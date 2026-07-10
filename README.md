# HS编码关税爬虫 — 商务部外贸实务查询平台

自动查询 wmsw.mofcom.gov.cn 的 HS 编码关税数据（原产地美国→目的地中国）。

## 版本演进

| 版本 | 文件 | 模式 | OCR | 速度 | 说明 |
|------|------|------|-----|------|------|
| v1 | `code/v1_browser.py` | Playwright 浏览器 | EasyOCR | 慢 | 最早版本 |
| v2 | `code/v2_api.py` | 纯 API | EasyOCR | 中 | 去掉浏览器 |
| v3 | `code/v3_deloitte.py` | 纯 API | 德勤内网 OCR | 15条/分 | 生产可用 |
| v4 | `code/v4_claude.py` | 纯 API | Claude Opus | 慢 | 效果不好 |
| **v5** | **`code/v5_concurrent.py`** | **纯 API 并发** | **德勤内网 OCR** | **31条/分** | **推荐** |

## 用法

```powershell
python code/v5_concurrent.py "CSV文件路径"
```

## 导出

```powershell
python code/export.py CN          # 出口国默认 US
python code/export.py CN JP       # 指定出口国
```

## 依赖

```bash
pip install requests beautifulsoup4 pillow
```

## 目录结构

```
code/     — Python 脚本 (v1~v5 + export)
output/   — 结果数据 (result.json, export_US_CN.csv)
```
