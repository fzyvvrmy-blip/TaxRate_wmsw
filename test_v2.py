"""V2 快速测试 — 验证 API 是否通"""
import io, requests, numpy as np
from PIL import Image
import easyocr

BASE_URL = "https://wmsw.mofcom.gov.cn/wmsw"

# 1. 建会话拿 cookie
s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0"})
r = s.get(f"{BASE_URL}/sfcxSearch", timeout=15)
print(f"1. 会话建立: JSESSIONID={s.cookies.get('JSESSIONID','?')[:20]}...")

# 2. 下载验证码
img_bytes = s.get(f"{BASE_URL}/imgcode?t=1", timeout=10).content
print(f"2. 验证码图片: {len(img_bytes)} bytes")

# 3. 保存验证码，放大处理
img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
print(f"3. 原始验证码: {img.size}")
img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
img.save("captcha_test.png")
print(f"   已放大到 {img.size}，存为 captcha_test.png")

# 4. OCR
reader = easyocr.Reader(['en'], gpu=False)
arr = np.array(img)

r1 = reader.readtext(arr, detail=0, text_threshold=0.3, low_text=0.3, width_ths=2.0)
code = "".join(r1).strip()
code = "".join(c for c in code if c.isalnum())
print(f"4. OCR 识别: '{code}' ({len(code)}位)")

# 4. 预校验
vr = s.post(f"{BASE_URL}/imgcode/preValidate", data={"imgcode": code},
            headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://wmsw.mofcom.gov.cn"}, timeout=10)
print(f"5. 预校验: {vr.json()}")

# 5. 提交查询
sr = s.post(f"{BASE_URL}/searchMore",
            data={"ec": "US", "oc": "0101210010", "ic": "CN", "hc": "0101210010", "imgcode": code},
            headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://wmsw.mofcom.gov.cn",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}, timeout=15)
print(f"6. 提交查询: status={sr.status_code}, 包含aNoClass={'aNoClass' in sr.text}, 长度={len(sr.text)}")
