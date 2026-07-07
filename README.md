# HS编码关税爬虫 — 商务部外贸实务查询平台

自动查询 wmsw.mofcom.gov.cn 的 HS 编码关税数据，支持断点续爬。

## 版本

| 文件 | 模式 | OCR | 需要VPN |
|------|------|-----|---------|
| `v1_browser.py` | Playwright 模拟浏览器 | EasyOCR 本地 | 否 |
| `v2_api.py` | 纯 HTTP 请求 | EasyOCR 本地 | 否 |
| `v3_deloitte.py` | 纯 HTTP 请求 | 德勤内网 OCR | 是 |

推荐用 **v3_deloitte.py**（连 VPN 时）或 **v2_api.py**（离线时）。

## 用法

```powershell
python v3_deloitte.py "CSV文件路径"
```

## 依赖

```bash
# v1 / v2
pip install easyocr playwright beautifulsoup4 requests pillow
python -m playwright install chromium

# v3
pip install requests beautifulsoup4 pillow
```

## 输出

```
output_v2/ 或 output_deloitte/
├── result.json      # 解析结果（按税则号去重）
├── progress.txt     # 断点进度
└── crawler.log      # 运行日志
```
