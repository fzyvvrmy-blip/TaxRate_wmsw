# 商务部外贸实务查询平台 — HS编码关税爬虫

自动查询商务部外贸实务查询平台 (wmsw.mofcom.gov.cn) 的 HS 编码关税数据。

## 功能

- 读取 CSV 中的 HS 编码列表
- 自动选择原产地=美国、目的地=中国
- EasyOCR 识别验证码（区分大小写）
- 自动提取税则号、进口关税、增值税、消费税
- 保存为 JSON，按税则号去重
- 断点续爬，随时中断可恢复

## 用法

```powershell
python wmsw_crawler.py "CSV文件路径"
```

示例：
```powershell
python "C:/Users/Administrator/Desktop/国别/关税/关税抓取/影刀改/wmsw_crawler.py" "C:/Users/Administrator/Desktop/国别/关税/关税抓取/HTScode/wmsw_results.csv"
```

## 查看进度

```powershell
cd "输出目录"
python -c "f=open('output/progress.txt'); print(f'已处理: {len(f.readlines())} 条'); f.close()"
python -c "import json; d=json.load(open('output/result.json',encoding='utf-8')); print(f'已抓取: {len(d)} 条')"
```

## 依赖

```bash
pip install easyocr playwright beautifulsoup4
python -m playwright install chromium
```

## 输出

```
output/
├── result.json      # 解析结果（按税则号去重）
├── progress.txt     # 断点进度（想从头跑删除此文件）
└── crawler.log      # 运行日志
```

## 断点续爬

随时 `Ctrl+C` 中断，下次重跑同一命令自动跳过已处理的编码。
