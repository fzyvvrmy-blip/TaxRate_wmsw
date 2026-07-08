"""
商务部外贸实务查询平台 — HS编码关税爬虫 (德勤内网OCR版)
========================================================
用法:
    python Deloitte_OCR.py "CSV文件路径"

使用德勤内网 OCR 服务 (ibondtest.deloitte.com.cn) 识别验证码，需连接公司 VPN。

依赖:
    pip install requests beautifulsoup4 pillow
"""

import csv
import io
import json
import os
import re
import sys
import time
import logging
import random
import urllib3

import requests
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# EasyOCR 降级（德勤API挂了时用）
_easyocr_reader = None
_deloitte_dead = False

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://wmsw.mofcom.gov.cn/wmsw"
SEARCH_URL = f"{BASE_URL}/sfcxSearch"
CAPTCHA_URL = f"{BASE_URL}/imgcode"
PRE_VALIDATE_URL = f"{BASE_URL}/imgcode/preValidate"
SEARCH_MORE_URL = f"{BASE_URL}/searchMore"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output_deloitte")
SAVE_FILE = os.path.join(OUTPUT_DIR, "result.json")
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.txt")

CAPTCHA_MAX_RETRIES = 10
QUERY_MAX_RETRIES = 3
MAX_STREAK_FAILURES = 20  # 连续失败N次自动停止（0=不限）

# ============================================================
# 日志
# ============================================================
os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(OUTPUT_DIR, "crawler.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# User-Agent 池 + Accept-Language 池（防检测）
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
]
LANG_LIST = ["zh-CN,zh;q=0.9", "en-US,en;q=0.9", "zh-CN,zh;q=0.9,en;q=0.8"]

# 请求计数器 + 轮换间隔
REQUEST_COUNT = 0
HEADER_ROTATE_INTERVAL = 50   # 每50条换 header
SESSION_REFRESH_INTERVAL = 100  # 每100条重建 session


# ============================================================
# 工具函数
# ============================================================

def read_csv_codes(file_path: str) -> list[str]:
    """读取CSV第二列（跳过表头）"""
    if not os.path.exists(file_path):
        log.error(f"CSV文件不存在: {file_path}")
        sys.exit(1)
    codes = []
    with open(file_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2:
                codes.append(row[1].strip())
    log.info(f"从CSV读取到 {len(codes)} 条编码")
    return codes


def load_progress() -> set[str]:
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_progress(code: str):
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(code + "\n")


def load_existing_results() -> list[dict]:
    if not os.path.exists(SAVE_FILE):
        return []
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_results(new_entry: dict):
    existing = load_existing_results()
    tariff_no = new_entry.get("税则号", "")
    for i, item in enumerate(existing):
        if item.get("税则号") == tariff_no and tariff_no:
            # 已有记录 → 更新（补全不完整数据）
            existing[i] = new_entry
            with open(SAVE_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            log.info(f"更新税则号 {tariff_no}（已有记录被覆盖）")
            return
    existing.append(new_entry)
    with open(SAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    log.info(f"已保存税则号 {tariff_no}，共 {len(existing)} 条记录")


def is_result_complete(entry: dict) -> bool:
    """判断一条结果是否信息完整"""
    return bool(
        entry.get("税则号") and
        entry.get("进口关税") and
        entry.get("其他税费") is not None
    )


def get_complete_codes() -> dict[str, bool]:
    """
    读取 result.json，返回 {原始编码: 是否完整}
    只有 税则号 + 进口关税 + 其他税费 都有的才算完整
    """
    results = load_existing_results()
    code_map = {}
    for entry in results:
        orig = entry.get("原始编码", "")
        if orig:
            # 如果已有完整记录，保留 True；不完整的用 False
            if orig not in code_map:
                code_map[orig] = is_result_complete(entry)
            elif not code_map[orig]:
                code_map[orig] = is_result_complete(entry)
    return code_map


def random_ua() -> str:
    return random.choice(UA_LIST)


# ============================================================
# 核心: API 请求
# ============================================================

def create_session() -> requests.Session:
    """创建带 cookie 的 session"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    # 先访问首页拿 JSESSIONID
    s.get(SEARCH_URL, timeout=15)
    return s


def get_captcha_image(session: requests.Session) -> bytes:
    """下载验证码图片"""
    resp = session.get(f"{CAPTCHA_URL}?t={int(time.time()*1000)}", timeout=10)
    return resp.content


# 德勤内网 OCR 服务
DELOITTE_OCR_URL = "https://ibondtest.deloitte.com.cn/ocr_file?character=1"


def preprocess_captcha(img_bytes: bytes):
    """预处理: 增强对比度→略微锐化→放大6倍"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    # 增强对比度（1.5倍）
    img = ImageEnhance.Contrast(img).enhance(1.5)
    # 锐化
    img = img.filter(ImageFilter.SHARPEN)
    # 放大6倍
    w, h = img.size
    img = img.resize((w * 6, h * 6), Image.LANCZOS)
    return img


def ocr_captcha(img_bytes: bytes, _=None) -> str | None:
    """预处理 + 德勤内网OCR识别验证码"""
    try:
        img = preprocess_captcha(img_bytes).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        resp = requests.post(DELOITTE_OCR_URL, data=buf.getvalue(), timeout=30, verify=False)
        if resp.status_code != 200:
            log.error(f"德勤OCR返回非200: {resp.status_code}")
            return None
        data = resp.json()
        if data.get("code") != 200 or "result" not in data:
            log.error(f"德勤OCR失败: {data.get('message', '?')}")
            return None

        texts = []
        for table in data["result"].get("tables", []):
            for line in table.get("lines", []):
                if isinstance(line, dict) and line.get("text"):
                    texts.append(line["text"])
        for line in data["result"].get("lines", []):
            if isinstance(line, dict) and line.get("text"):
                texts.append(line["text"])
        result = "".join(texts)
        # 只去掉空白和特殊符号，保留中文英文数字
        result = re.sub(r"[\s\n\r\t]", "", result)
        log.info(f"德勤OCR: '{result}' ({len(result)}位)")
        # 验证码一定4位（数字/英文/中文），不足4位刷新重试
        return result if len(result) == 4 else None
    except Exception as e:
        log.error(f"德勤OCR异常: {e}")
        return None


def validate_captcha(session: requests.Session, code: str) -> bool:
    """预校验验证码是否正确（不提交表单）"""
    try:
        resp = session.post(PRE_VALIDATE_URL, data={"imgcode": code}, timeout=10,
                            headers={"X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL, "Referer": SEARCH_URL})
        return resp.json().get("validateResult") == True
    except Exception:
        return False


def submit_search(session: requests.Session, hs_code: str, captcha: str) -> str | None:
    """提交查询表单，返回HTML"""
    try:
        resp = session.post(SEARCH_MORE_URL, data={
            "ec": "US",
            "oc": hs_code,
            "ic": "CN",
            "hc": hs_code,
            "imgcode": captcha,
        }, timeout=15, headers={
            "Origin": BASE_URL,
            "Referer": SEARCH_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        })
        if "#aNoClass" in resp.text or "taxrateRadio" in resp.text:
            return resp.text
        return None
    except Exception as e:
        log.error(f"提交查询失败: {e}")
        return None


# ============================================================
# 解析
# ============================================================

def parse_results_page(html: str, original_code: str) -> dict | None:
    """解析查询结果页HTML"""
    soup = BeautifulSoup(html, "html.parser")
    result = {"税则号": "", "进口关税": "", "其他税费": []}

    ano = soup.select_one("#aNoClass")
    if ano:
        text = ano.get_text(strip=True)
        digits = "".join(re.findall(r"\d+", text))
        result["税则号"] = digits if digits else original_code.replace(".", "").replace(" ", "")
    else:
        result["税则号"] = original_code.replace(".", "").replace(" ", "")

    checked = soup.select_one('input[name="taxrateRadio"]:checked')
    if checked:
        parent = checked.find_parent("p") or checked.find_parent("label")
        if parent:
            result["进口关税"] = " ".join(parent.get_text(" ", strip=True).split())

    sel_box = soup.select_one("#divLeft div.selBox") or soup.select_one(".selBox")
    if sel_box:
        for h6 in sel_box.find_all("h6"):
            tax_name = h6.get_text(strip=True)
            if "其他税费" in tax_name and not h6.find_next_sibling("p"):
                continue
            p = h6.find_next_sibling("p")
            if p:
                tax_rate = p.get_text(strip=True)
                if tax_rate:
                    result["其他税费"].append({"税种": tax_name, "税率": tax_rate})

    result["原始编码"] = original_code
    return result if result["税则号"] else None


# ============================================================
# 单条处理
# ============================================================

def process_single_code(session: requests.Session, code: str, index: int) -> dict | None:
    """处理单条HS编码"""
    log.info(f"{'='*50}")
    log.info(f"[{index}] 开始处理编码: {code}")
    log.info(f"{'='*50}")

    for query_attempt in range(1, QUERY_MAX_RETRIES + 1):
        log.info(f"查询尝试 {query_attempt}/{QUERY_MAX_RETRIES}")

        # 获取验证码图片
        img_bytes = get_captcha_image(session)
        if not img_bytes:
            log.warning("获取验证码图片失败")
            continue

        # 德勤OCR 识别 + 预校验
        captcha_ok = False
        captcha_text = None
        empty_streak = 0
        for captcha_attempt in range(1, CAPTCHA_MAX_RETRIES + 1):
            captcha_text = ocr_captcha(img_bytes)
            if not captcha_text:
                empty_streak += 1
                log.warning(f"OCR返回空({empty_streak}连空)，刷新验证码...")
                # 连续3次为空 → 可能是session或API挂了，重建session
                if empty_streak >= 3:
                    log.warning("OCR连续空，重建会话...")
                    session.cookies.clear()
                    session.get(SEARCH_URL, timeout=15)
                    empty_streak = 0
                img_bytes = get_captcha_image(session)
                continue
            empty_streak = 0

            log.info(f"OCR: '{captcha_text}'，预校验中...")

            if validate_captcha(session, captcha_text):
                log.info("验证码正确！")
                captcha_ok = True
                break
            else:
                log.warning(f"验证码 '{captcha_text}' 错误，刷新重试...")
                img_bytes = get_captcha_image(session)

        if not captcha_ok or not captcha_text:
            log.error("验证码识别/校验失败（10次重试用尽）")
            continue

        # 提交查询
        html = submit_search(session, code, captcha_text)
        if html:
            result = parse_results_page(html, code)
            if result:
                log.info(f"解析成功: 税则号={result['税则号']}, 进口关税={result['进口关税'][:50]}...")
                return result
            else:
                log.warning("HTML解析失败")
        else:
            log.warning("查询返回无结果页标记")

    log.error(f"编码 {code} 查询失败")
    return None


# ============================================================
# 主流程
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    csv_path = sys.argv[1]
    codes = read_csv_codes(csv_path)
    if not codes:
        log.error("CSV中没有编码数据")
        sys.exit(1)

    # 续跑逻辑：检查 progress.txt 和 result.json，信息不完整的也要重做
    processed = load_progress()
    complete_codes = get_complete_codes()  # {原始编码: 是否完整}

    remaining = []
    incomplete_retry = []
    for c in codes:
        if c not in processed:
            remaining.append(c)  # 没处理过的 → 要做
        elif c in complete_codes and not complete_codes[c]:
            incomplete_retry.append(c)  # 处理过但信息不完整 → 重做

    log.info(f"总计 {len(codes)} 条")
    log.info(f"  已完成（信息完整）: {len(processed) - len(incomplete_retry)} 条")
    log.info(f"  待重做（信息不完整）: {len(incomplete_retry)} 条")
    log.info(f"  未处理: {len(remaining)} 条")

    remaining = incomplete_retry + remaining  # 先补漏，再做新的

    if not remaining:
        log.info("所有编码已处理完毕且信息完整")
        return

    success = fail = streak_fail = 0
    session = create_session()
    log.info(f"会话已建立，JSESSIONID={session.cookies.get('JSESSIONID', '?')[:20]}...")
    log.info("OCR 使用德勤内网服务: ibondtest.deloitte.com.cn")

    try:
        for i, code in enumerate(remaining, start=1):
            global_index = len(processed) + i
            result = process_single_code(session, code, global_index)

            if result:
                save_results(result)
                save_progress(code)
                success += 1
                streak_fail = 0
            else:
                save_progress(code)
                fail += 1
                streak_fail += 1
                if MAX_STREAK_FAILURES > 0 and streak_fail >= MAX_STREAK_FAILURES:
                    log.error(f"连续失败 {streak_fail} 次，自动停止")
                    break

            log.info(f"进度: {success} 成功 / {fail} 失败 / {len(remaining)-i} 剩余")

            # 每50条换 header，每100条重建 session
            global REQUEST_COUNT
            REQUEST_COUNT += 1
            if REQUEST_COUNT % HEADER_ROTATE_INTERVAL == 0:
                session.headers.update({"User-Agent": random.choice(UA_LIST), "Accept-Language": random.choice(LANG_LIST)})
                log.info(f"已更换 Header（第{REQUEST_COUNT}条）")
            if REQUEST_COUNT % SESSION_REFRESH_INTERVAL == 0:
                session.cookies.clear()
                session.get(SEARCH_URL, timeout=15)
                log.info(f"已重建 Session（第{REQUEST_COUNT}条）")

            # 极短间隔（OCR调用本身已有耗时，无需额外等待）
            time.sleep(random.uniform(0.1, 0.3))

    except KeyboardInterrupt:
        log.warning("用户中断，进度已保存")
    finally:
        session.close()

    log.info(f"完成！成功 {success} 条，失败 {fail} 条")
    log.info(f"结果文件: {SAVE_FILE}")


if __name__ == "__main__":
    main()
