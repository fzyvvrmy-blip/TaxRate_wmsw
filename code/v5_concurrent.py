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
import ssl
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# 自定义 SSL 适配器：降低安全级别适配商务部老网站
class LowSecurityHTTPAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

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
            existing[i] = new_entry
            tmp = SAVE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SAVE_FILE)  # 原子操作，不会写坏
            log.info(f"更新税则号 {tariff_no}（已有记录被覆盖）")
            return
    existing.append(new_entry)
    tmp = SAVE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SAVE_FILE)
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
    """创建带 cookie 的 session（使用自定义 SSL）"""
    s = requests.Session()
    adapter = LowSecurityHTTPAdapter()
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    # 先访问首页拿 JSESSIONID
    s.get(SEARCH_URL, timeout=15)
    return s


def get_captcha_image(session: requests.Session) -> bytes:
    """下载验证码图片"""
    resp = session.get(f"{CAPTCHA_URL}?t={int(time.time()*1000)}", timeout=10, verify=False)
    return resp.content


# 德勤内网 OCR 服务
DELOITTE_OCR_URL = "https://ibondtest.deloitte.com.cn/ocr_file?character=1"


def preprocess_captcha(img_bytes: bytes):
    """只放大3倍（实测最佳: 3x>6x>1x，不加对比度锐化）"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    return img.resize((w * 3, h * 3), Image.LANCZOS)
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
        resp = session.post(PRE_VALIDATE_URL, data={"imgcode": code}, timeout=10, verify=False,
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
        }, timeout=15, verify=False, headers={
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

        img_bytes = get_captcha_image(session)
        if not img_bytes:
            continue

        # 德勤OCR + preValidate 快速筛选
        captcha_ok = False
        captcha_text = None
        empty_streak = 0
        for captcha_attempt in range(1, CAPTCHA_MAX_RETRIES + 1):
            captcha_text = ocr_captcha(img_bytes)
            if not captcha_text:
                empty_streak += 1
                if empty_streak >= 3:
                    session.cookies.clear()
                    session.get(SEARCH_URL, timeout=15, verify=False)
                    empty_streak = 0
                img_bytes = get_captcha_image(session)
                continue
            empty_streak = 0

            if validate_captcha(session, captcha_text):
                captcha_ok = True
                break
            else:
                img_bytes = get_captcha_image(session)

        if not captcha_ok or not captcha_text:
            log.error("验证码校验失败")
            continue

        # 提交查询
        html = submit_search(session, code, captcha_text)
        if html:
            result = parse_results_page(html, code)
            if result:
                log.info(f"解析成功: 税则号={result['税则号']}, 进口关税={result['进口关税'][:50]}...")
                return result

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
    missing_result = []
    incomplete_retry = []
    for c in codes:
        if c not in processed:
            remaining.append(c)  # 没处理过的 → 要做
        elif c not in complete_codes:
            missing_result.append(c)  # 处理过但完全没结果 → 重做
        elif not complete_codes[c]:
            incomplete_retry.append(c)  # 处理过但信息不完整 → 重做

    log.info(f"总计 {len(codes)} 条")
    log.info(f"  已完成（信息完整）: {len(processed) - len(incomplete_retry) - len(missing_result)} 条")
    log.info(f"  待重做（无结果）: {len(missing_result)} 条")
    log.info(f"  待重做（不完整）: {len(incomplete_retry)} 条")
    log.info(f"  未处理: {len(remaining)} 条")

    remaining = missing_result + incomplete_retry + remaining

    if not remaining:
        log.info("所有编码已处理完毕且信息完整")
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    write_lock = threading.Lock()
    success = fail = 0
    streak_fail = 0
    log.info("OCR 使用德勤内网服务: ibondtest.deloitte.com.cn")
    log.info(f"并发模式: 10 线程")

    def do_one(code, index):
        nonlocal success, fail, streak_fail
        s = create_session()
        try:
            result = process_single_code(s, code, index)
            with write_lock:
                if result:
                    save_results(result)
                    save_progress(code)
                    success += 1
                    streak_fail = 0
                else:
                    save_progress(code)
                    fail += 1
                    streak_fail += 1
                log.info(f"进度: {success} 成功 / {fail} 失败 / {len(remaining) - success - fail} 剩余")
            return result is not None
        finally:
            s.close()

    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(do_one, code, len(processed) + i): code
                       for i, code in enumerate(remaining, start=1)}
            for future in as_completed(futures):
                if MAX_STREAK_FAILURES > 0 and streak_fail >= MAX_STREAK_FAILURES:
                    log.error(f"连续失败 {streak_fail} 次，自动停止")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
    except KeyboardInterrupt:
        log.warning("用户中断，进度已保存")

    log.info(f"完成！成功 {success} 条，失败 {fail} 条")
    log.info(f"结果文件: {SAVE_FILE}")


if __name__ == "__main__":
    main()
