"""
用法: python export.py CN [US]
输出: output_deloitte/export_US_CN.csv
"""
import csv, json, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(BASE, "output_deloitte", "result.json")
CSV_DIR = os.path.join(BASE, "..", "HTScode")

importer = sys.argv[1].upper()
exporter = sys.argv[2].upper() if len(sys.argv) > 2 else "US"

# 加载关税数据: {编码: 关税文本}
tariffs = {}
for r in json.load(open(RESULT, encoding="utf-8")):
    code = r.get("原始编码", "").strip().replace(".", "").replace(" ", "")
    if not code:
        continue
    parts = [f"进口关税: {r.get('进口关税', '')}"]
    for t in r.get("其他税费", []):
        parts.append(f"{t['税种']}: {t['税率']}")
    tariffs[code] = "; ".join(parts)

# 拼接导出
csv_path = os.path.join(CSV_DIR, f"hts_{importer}.csv")
out_path = os.path.join(os.path.dirname(RESULT), f"export_{exporter}_{importer}.csv")

with open(csv_path, encoding="utf-8-sig") as f_in, \
     open(out_path, "w", encoding="utf-8-sig", newline="") as f_out:
    reader = csv.reader(f_in)
    next(reader)  # 跳表头
    writer = csv.writer(f_out)
    writer.writerow(["出口国", "进口国", "税则号", "商品名称", "关税"])
    for row in reader:
        code = row[1].strip().replace(".", "").replace(" ", "")
        writer.writerow([exporter, importer, code, row[2], tariffs.get(code, "")])

print(f"导出完成: {out_path}")
