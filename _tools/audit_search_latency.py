# -*- coding: utf-8 -*-
"""取证：2 字词走 LIKE 兜底的实际耗时（决定词表批量查询是否可行）。"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "程序"))
from msyt.db import DB  # noqa: E402

db = DB(os.path.join(os.path.dirname(HERE), "程序", "data", "msyt.db"))

print("%-8s %-6s %10s %8s %10s" % ("词", "scope", "命中", "mode", "耗时ms"))
for q in ("地铁", "换乘", "无障碍", "垃圾"):
    for scope in ("all", "post"):
        t0 = time.time()
        r = db.search(q, scope=scope, limit=20)
        print("%-8s %-6s %10s %8s %10.0f" % (
            q, scope, r.get("total"), r.get("mode"), (time.time() - t0) * 1000))

print()
print("--- 模拟词表批量：20 个词 × scope=all ---")
terms = ["地铁", "公交", "换乘", "晚点", "拥堵", "噪声", "漏水", "停电", "垃圾", "电梯",
         "燃气", "施工", "积水", "违停", "油烟", "扰民", "道路", "停车", "照明", "井盖"]
t0 = time.time()
tot = 0
for t in terms:
    tot += db.search(t, scope="all", limit=1).get("total") or 0
el = time.time() - t0
print("  20 词耗时 %.1fs，平均 %.2fs/词，合计命中 %d" % (el, el / len(terms), tot))
