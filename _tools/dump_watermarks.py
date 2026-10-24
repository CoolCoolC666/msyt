# -*- coding: utf-8 -*-
"""从 list_watermark 还原「上次列表采集」用了哪些源、还剩多少。

用途：Web 端的任务表在**内存**里，服务一重启就没了；
但水位表在库里，于是可以从水位反推出源清单，重发同一个任务续采。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.config import Config     # noqa: E402

cfg = Config.load()
import sqlite3                    # noqa: E402

conn = sqlite3.connect(cfg.db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT source,dim,code,name,last_page,max_page,rows_total,status "
    "FROM list_watermark ORDER BY source").fetchall()
print(f"共 {len(rows)} 个采集源的水位")
print(f"{'source':<22}{'dim':<10}{'last':>6}{'max':>6}{'rows':>9}  status")
todo = []
for r in rows:
    print(f"{r['source']:<22}{r['dim'] or '':<10}{r['last_page']:>6}"
          f"{r['max_page']:>6}{r['rows_total']:>9}  {r['status']}")
    if r["status"] != "done":
        todo.append(r["source"])

depts = [r["source"].split(":", 1)[1] for r in rows
         if (r["dim"] == "dept") and r["source"].startswith("dept:")]
print()
print("dept 源数:", len(depts))
print("dept_ids =", ",".join(depts))
print("未采完的源:", len(todo))
conn.close()
