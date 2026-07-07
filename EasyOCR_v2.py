"""
商务部外贸实务查询平台 — HS编码关税爬虫 V2 (纯API模式)
======================================================
用法:
    python EasyOCR_v2.py "CSV文件路径"

与V1区别: 不启动浏览器，纯 HTTP 请求，每条 <1秒，不占内存不崩

依赖:
    pip install easyocr requests beautifulsoup4
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

import requests
import numpy as np
from PIL import Image
from bs4 import BeautifulSoup
import easyocr

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://wmsw.mofcom.gov.cn/wmsw"
SEARCH_URL = f"{BASE_URL}/sfcxSearch"
CAPTCHA_URL = f"{BASE_URL}/imgcode"
PRE_VALIDATE_URL = f"{BASE_URL}/imgcode/preValidate"
SEARCH_MORE_URL = f"{BASE_URL}/searchMore"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output_v2")
SAVE_FILE = os.path.join(OUTPUT_DIR, "result.json")
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.txt")

CAPTCHA_MAX_RETRIES = 10
QUERY_MAX_RETRIES = 3

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

# User-Agent 池（防止被抓包）
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


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
    for item in existing:
        if item.get("税则号") == tariff_no and tariff_no:
            return
    existing.append(new_entry)
    with open(SAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    log.info(f"已保存税则号 {tariff_no}，共 {len(existing)} 条记录")


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


def ocr_captcha(img_bytes: bytes, reader) -> str | None:
    """EasyOCR 识别验证码"""
    try:
        image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_array = np.array(image)
        results = reader.readtext(img_array, detail=0, text_threshold=0.3, low_text=0.3, link_threshold=0.3, width_ths=2.0)
        result = "".join(results).strip()
        result = "".join(c for c in result if c.isalnum())
        return result if len(result) >= 3 else None
    except Exception as e:
        log.error(f"OCR失败: {e}")
        return None


def validate_captcha(session: requests.Session, code: str) -> bool:
    """预校验验证码是否正确（不提交表单）"""
    try:
        resp = session.post(PRE_VALIDATE_URL, data={"imgcode": code}, timeout=10,
                            headers={"X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL, "Referer": SEARCH_URL})
        return resp.json().get("success") == True
    except Exception:
        return False


def submit_search(session: requests.Session, hs_code: str, captcha: str) -> str | None:
    """提交查询表单，返回HTML"""
    try:
        resp = session.post(SEARCH_MORE_URL, data={
            "ec": "US",
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

    return result if result["税则号"] else None


# ============================================================
# 单条处理
# ============================================================

def process_single_code(session: requests.Session, reader, code: str, index: int) -> dict | None:
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

        # OCR 识别 + 预校验
        captcha_ok = False
        captcha_text = None
        for captcha_attempt in range(1, CAPTCHA_MAX_RETRIES + 1):
            captcha_text = ocr_captcha(img_bytes, reader)
            if not captcha_text:
                log.warning(f"OCR未识别到文字，刷新验证码...")
                img_bytes = get_captcha_image(session)
                continue

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

    processed = load_progress()
    remaining = [c for c in codes if c not in processed]
    log.info(f"总计 {len(codes)} 条，已处理 {len(processed)} 条，剩余 {len(remaining)} 条")

    if not remaining:
        log.info("所有编码已处理完毕")
        return

    log.info("加载 EasyOCR...")
    reader = easyocr.Reader(['en'], gpu=False)
    log.info("EasyOCR 加载完成")

    success = fail = 0
    session = create_session()
    log.info(f"会话已建立，JSESSIONID={session.cookies.get('JSESSIONID', '?')[:20]}...")

    try:
        for i, code in enumerate(remaining, start=1):
            global_index = len(processed) + i
            result = process_single_code(session, reader, code, global_index)

            if result:
                save_results(result)
                save_progress(code)
                success += 1
            else:
                save_progress(code)
                fail += 1

            log.info(f"进度: {success} 成功 / {fail} 失败 / {len(remaining)-i} 剩余")

            # 随机休眠防抓包（2~6秒）
            delay = random.uniform(2, 6)
            time.sleep(delay)

    except KeyboardInterrupt:
        log.warning("用户中断，进度已保存")
    finally:
        session.close()

    log.info(f"完成！成功 {success} 条，失败 {fail} 条")
    log.info(f"结果文件: {SAVE_FILE}")


if __name__ == "__main__":
    main()
