# -*- coding: utf-8 -*-
"""一次性验证：在**真库**上真跑一次导出，看 29 列与内容。"""
import csv
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))
from msyt.db import DB  # noqa: E402

DBP = os.path.join(PROJ, "程序", "data", "msyt.db")
OUT = os.path.join(HERE, "_real_export.csv")

db = DB(DBP)
t0 = time.time()
n = db.export_csv(OUT)
el = time.time() - t0
size = os.path.getsize(OUT)
print("导出 %d 行，%.1f MB，用时 %.1f 秒" % (n, size / 1048576, el))

with open(OUT, "r", encoding="utf-8-sig", newline="") as f:
    rd = csv.reader(f)
    head = next(rd)
    print("列数 =", len(head))
    print("新列 =", head[-6:])
    rows = []
    for i, r in enumerate(rd):
        if i < 3:
            rows.append(r)
        if i > 200000:
            break

for r in rows[:2]:
    d = dict(zip(head, r))
    print("-" * 60)
    print("tid =", d["tid"], "|", d["title"][:30])
    print("  发现时间   :", d["发现时间"])
    print("  原文摘要   :", d["原文摘要"][:90])
    print("  核验结论   :", repr(d["核验结论"]))
    print("  候选池路径 :", d["候选池路径"])
    print("  系统诊断   :", d["系统诊断"][:60])
    print("  词表命中   :", d["词表命中"][:80])
