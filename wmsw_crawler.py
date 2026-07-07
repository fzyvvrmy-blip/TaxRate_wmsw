"""
商务部外贸实务查询平台 — HS编码关税爬虫（有断点续爬）
============================================
用法:
terminal里面python "C:/Users/Administrator/Desktop/国别/关税/关税抓取/影刀改/wmsw_crawler.py" "C:/Users/Administrator/Desktop/国别/关税/关税抓取/HTScode/wmsw_results.csv"

输出:
    output/
    ├── result.json      ← 解析结果（按税则号去重）
    ├── progress.txt     ← 断点进度（每处理完一条就写入）（想从头跑的话，删除该文件即可）
    └── crawler.log      ← 运行日志

依赖:
    pip install easyocr playwright beautifulsoup4
    python -m playwright install chromium
"""

import csv
import json
import os
import re
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import easyocr
import io
from PIL import Image
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============================================================
# 配置
# ============================================================

# 商务部外贸实务查询平台
TARGET_URL = "https://wmsw.mofcom.gov.cn/wmsw/sfcxSearch"

# 脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 结果输出目录
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
SAVE_FILE = os.path.join(OUTPUT_DIR, "result.json")

# 进度文件（记录已处理的编码，用于断点续爬）
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.txt")

# 验证码最大重试次数
CAPTCHA_MAX_RETRIES = 5

# 查询最大重试次数（不含验证码）
QUERY_MAX_RETRIES = 3

# 页面加载等待时间（秒）
PAGE_WAIT = 2

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
        next(reader, None)  # 跳过表头
        for row in reader:
            if len(row) >= 2:
                codes.append(row[1].strip())
    log.info(f"从CSV读取到 {len(codes)} 条编码")
    return codes


def load_progress() -> set[str]:
    """加载已处理的编码集合"""
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_progress(code: str):
    """记录一条编码已处理"""
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(code + "\n")


def load_existing_results() -> list[dict]:
    """加载已有结果文件"""
    if not os.path.exists(SAVE_FILE):
        return []
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_results(new_entry: dict):
    """追加一条结果到JSON（按税则号去重）"""
    existing = load_existing_results()

    # 去重
    tariff_no = new_entry.get("税则号", "")
    for item in existing:
        if item.get("税则号") == tariff_no and tariff_no:
            log.info(f"税则号 {tariff_no} 已存在，跳过保存")
            return

    existing.append(new_entry)
    with open(SAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    log.info(f"已保存税则号 {tariff_no}，共 {len(existing)} 条记录")


def parse_results_page(html: str, original_code: str) -> dict | None:
    """
    解析查询结果页HTML
    返回: {'税则号': ..., '进口关税': ..., '其他税费': [...]} 或 None
    """
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "税则号": "",
        "进口关税": "",
        "其他税费": [],
    }

    # 1. 解析税则号 - 从 #aNoClass 提取完整数字
    ano = soup.select_one("#aNoClass")
    if ano:
        text = ano.get_text(strip=True)
        # 提取所有数字，拼接成完整税则号
        digits = "".join(re.findall(r"\d+", text))
        if digits:
            result["税则号"] = digits
        else:
            # 如果没有数字，使用原始输入的编码
            result["税则号"] = original_code.replace(".", "").replace(" ", "")
    else:
        log.warning("未找到 #aNoClass 元素，使用输入编码")
        result["税则号"] = original_code.replace(".", "").replace(" ", "")

    # 2. 解析进口关税 - 选中的 radio
    checked = soup.select_one('input[name="taxrateRadio"]:checked')
    if checked:
        parent = checked.find_parent("p") or checked.find_parent("label")
        if parent:
            result["进口关税"] = " ".join(parent.get_text(" ", strip=True).split())

    # 3. 解析其他税费（增值税、消费税等）
    sel_box = soup.select_one("#divLeft div.selBox") or soup.select_one(".selBox")
    if sel_box:
        h6_tags = sel_box.find_all("h6")
        for h6 in h6_tags:
            tax_name = h6.get_text(strip=True)
            # 跳过"其他税费"标题本身
            if "其他税费" in tax_name and not h6.find_next_sibling("p"):
                continue
            # 查找紧邻的 p 标签
            p = h6.find_next_sibling("p")
            if p:
                tax_rate = p.get_text(strip=True)
                if tax_rate:
                    result["其他税费"].append({"税种": tax_name, "税率": tax_rate})

    if not result["税则号"]:
        log.warning("未能解析到税则号")
        return None

    return result


def is_captcha_present(page) -> bool:
    """检查页面是否存在验证码"""
    try:
        captcha_input = page.locator('input[name="imgcode"]')
        return captcha_input.count() > 0 and captcha_input.is_visible()
    except Exception:
        return False


def is_result_page(page) -> bool:
    """检查当前是否在结果页"""
    try:
        page.wait_for_selector("#aNoClass", timeout=3000)
        return True
    except PWTimeout:
        return False


# ============================================================
# 核心逻辑
# ============================================================

def click_blank(page):
    """点击'税费查询'标题空白处，消除下拉框"""
    page.locator('h3:has-text("税费查询")').click()
    time.sleep(0.3)


def select_origin_destination(page):
    """选择原产地=美国，目的地=中国"""
    # ========== 原产地 ==========
    page.locator('input[value="请选择原产地"]').click()
    time.sleep(0.6)

    # 选中"中北美"tab → 选择"美国"
    page.locator('li:has-text("中北美"):visible').first.click()
    time.sleep(0.5)
    page.locator('li[data="US"]:visible').first.click()
    log.info("已选择原产地: 美国")
    time.sleep(0.3)
    click_blank(page)

    # ========== 目的地 ==========
    page.locator('input[value="请选择目的地"]').click()
    time.sleep(0.6)

    # 直接选择"中国"（在可见面板中）
    page.locator('li[data="CN"]:visible').first.click()
    log.info("已选择目的地: 中国")
    time.sleep(0.3)
    click_blank(page)


def fill_hs_code(page, code: str, index: int):
    """填入HS编码"""
    log.info(f"[{index}] 正在填入HS编码: {code}")
    input_box = page.locator('input[name="hc"]')
    input_box.click(timeout=3000)
    input_box.fill("", timeout=3000)  # 先清空
    input_box.fill(str(code), timeout=3000)
    time.sleep(0.3)
    click_blank(page)  # 消除可能的弹窗遮挡


def recognize_captcha(page, reader) -> str | None:
    """
    截图验证码图片 → EasyOCR识别 → 返回识别文本（区分大小写）
    """
    captcha_img = page.locator("img#img_code")
    if captcha_img.count() == 0:
        log.warning("未找到验证码图片元素")
        return None

    try:
        img_bytes = captcha_img.screenshot(timeout=5000)
        image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        import numpy as np
        img_array = np.array(image)

        # 检测后用宽松参数合并框，避免丢末位
        results = reader.readtext(
            img_array, detail=0,
            text_threshold=0.3, low_text=0.3, link_threshold=0.3,
            width_ths=2.0,  # 横向拉宽，把分散字符并在一起
        )
        result = "".join(results).strip()
        result = "".join(c for c in result if c.isalnum())

        log.info(f"EasyOCR 识别结果: '{result}'")
        return result if result and len(result) >= 3 else None
    except Exception as e:
        log.error(f"验证码截图/识别失败: {e}")
        return None


def fill_captcha(page, text: str):
    """将识别的验证码填入输入框"""
    captcha_input = page.locator('input[name="imgcode"]')
    if captcha_input.count() > 0:
        captcha_input.click(timeout=3000)
        captcha_input.fill("", timeout=3000)
        captcha_input.fill(text, timeout=3000)
        time.sleep(0.3)


def refresh_captcha(page):
    """点击验证码图片刷新"""
    captcha_img = page.locator("img#img_code")
    if captcha_img.count() > 0:
        captcha_img.click()
        log.info("已点击验证码图片刷新")
        time.sleep(1)


def click_query(page) -> bool:
    """点击查询按钮"""
    query_btn = page.locator('input.qBtn_01')
    if query_btn.count() == 0:
        query_btn = page.locator("xpath=//input[@type='button'][contains(@class,'qBtn')]")
    if query_btn.count() == 0:
        query_btn = page.locator("xpath=//input[@type='button'][@value='查询']")

    if query_btn.count() > 0:
        query_btn.scroll_into_view_if_needed()
        query_btn.click(timeout=5000, force=True)
        log.info("已点击查询按钮")
        return True
    else:
        log.error("未找到查询按钮")
        return False


def handle_captcha_flow(page, reader) -> bool:
    """
    EasyOCR 识别验证码（区分大小写）
    """
    for attempt in range(1, CAPTCHA_MAX_RETRIES + 1):
        if not is_captcha_present(page):
            log.info("页面无验证码，跳过验证码处理")
            return True

        log.info(f"验证码识别第 {attempt}/{CAPTCHA_MAX_RETRIES} 次...")

        captcha_text = recognize_captcha(page, reader)
        if captcha_text and len(captcha_text) >= 3:
            fill_captcha(page, captcha_text)
            return True

        # 识别失败或结果太短，刷新验证码
        log.warning(f"验证码识别结果不理想 ({captcha_text})，刷新重试")
        refresh_captcha(page)
        time.sleep(1)

    log.error(f"验证码识别失败，已重试 {CAPTCHA_MAX_RETRIES} 次")
    return False


def process_single_code(page, reader, code: str, index: int) -> dict | None:
    """处理单条HS编码的完整流程"""
    log.info(f"{'='*50}")
    log.info(f"[{index}] 开始处理编码: {code}")
    log.info(f"{'='*50}")

    # 1. 确保在查询页面
    if "sfcxSearch" not in page.url:
        log.info("当前不在查询页，正在跳转...")
        page.goto(TARGET_URL)
        time.sleep(PAGE_WAIT)

    # 2. 选择原产地和目的地
    select_origin_destination(page)

    # 3. 填入HS编码
    fill_hs_code(page, code, index)

    # 4. 处理验证码 + 查询 + 等待结果
    for query_attempt in range(1, QUERY_MAX_RETRIES + 1):
        log.info(f"查询尝试 {query_attempt}/{QUERY_MAX_RETRIES}")

        if not handle_captcha_flow(page, reader):
            log.warning("验证码处理失败，刷新页面重试")
            page.goto(TARGET_URL)
            time.sleep(PAGE_WAIT)
            select_origin_destination(page)
            fill_hs_code(page, code, index)
            continue

        # 点击查询（force click 防止被遮挡）
        if not click_query(page):
            log.error("未找到查询按钮")
            return None

        # 等待结果页加载（先快速检查验证码是否还在，还在说明输了）
        time.sleep(2)
        if is_captcha_present(page):
            log.warning("验证码错误（2秒内验证码仍在），刷新重试...")
            refresh_captcha(page)
            continue
        try:
            page.wait_for_selector("#aNoClass", timeout=6000)
            log.info("查询成功，进入结果页")
            break
        except PWTimeout:
            log.warning("未进入结果页，刷新验证码重试...")
            refresh_captcha(page)
            continue
    else:
        log.error(f"编码 {code} 查询失败（{QUERY_MAX_RETRIES}次重试用尽）")
        return None

    # 5. 解析结果（页面HTML已包含全部信息，无需再查验证码）
    html = page.content()
    result = parse_results_page(html, code)

    if result:
        log.info(f"解析成功: 税则号={result['税则号']}, 进口关税={result['进口关税'][:50]}...")

    # 6. 返回查询页，准备下一条
    page.goto(TARGET_URL)
    time.sleep(PAGE_WAIT)

    return result


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("用法: python wmsw_crawler.py <csv文件路径>")
        print("示例: python wmsw_crawler.py C:/Users/Administrator/Desktop/国别/关税/HS编码.csv")
        sys.exit(1)

    csv_path = sys.argv[1]
    codes = read_csv_codes(csv_path)

    if not codes:
        log.error("CSV中没有编码数据")
        sys.exit(1)

    # 断点续爬：过滤已处理的编码
    processed = load_progress()
    remaining = [c for c in codes if c not in processed]
    skipped = len(codes) - len(remaining)

    log.info(f"总计 {len(codes)} 条编码，已处理 {skipped} 条，剩余 {len(remaining)} 条")
    if skipped > 0:
        log.info(f"已处理的编码: {sorted(processed)}")

    if not remaining:
        log.info("所有编码已处理完毕，无需继续")
        return

    # 初始化 EasyOCR
    log.info("加载 EasyOCR（区分大小写）...")
    reader = easyocr.Reader(['en'], gpu=False)
    log.info("EasyOCR 加载完成")

    # 启动浏览器
    log.info("启动浏览器...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # 需要看到验证码，不能无头模式
            args=["--start-maximized"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(TARGET_URL)
        time.sleep(PAGE_WAIT + 1)
        log.info(f"浏览器已启动，当前页面: {page.url}")

        success_count = 0
        fail_count = 0

        try:
            for i, code in enumerate(remaining, start=1):
                global_index = skipped + i
                result = process_single_code(page, reader, code, global_index)

                if result:
                    save_results(result)
                    save_progress(code)
                    success_count += 1
                else:
                    log.error(f"编码 {code} 处理失败")
                    # 仍然记录进度避免死循环
                    save_progress(code)
                    fail_count += 1

                log.info(f"进度: {success_count} 成功 / {fail_count} 失败 / {len(remaining) - i} 剩余")
        except KeyboardInterrupt:
            log.warning("用户中断，进度已保存，可随时恢复")
        finally:
            browser.close()

    log.info(f"完成！成功 {success_count} 条，失败 {fail_count} 条")
    log.info(f"结果文件: {SAVE_FILE}")


if __name__ == "__main__":
    main()
