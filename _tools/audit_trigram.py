# -*- coding: utf-8 -*-
"""取证：trigram 分词器对 2 字中文词是否失效（对比 LIKE 与 FTS 命中数）。"""
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()

TERMS = ["地铁", "公交", "换乘", "晚点", "拥堵", "噪声", "漏水", "停电", "垃圾",
         "无障碍", "停车场", "红绿灯", "电动车", "燃气", "电梯"]
print("%-8s %-6s %10s %10s %8s" % ("词", "字数", "LIKE命中", "FTS命中", "FTS占比"))
tot_like = tot_fts = 0
for t in TERMS:
    like = cur.execute(
        "select count(*) from posts where pid>0 and content like ?", ("%" + t + "%",)).fetchone()[0]
    fts = cur.execute("select count(*) from posts_fts where posts_fts match ?", (t,)).fetchone()[0]
    tot_like += like
    tot_fts += fts
    print("%-8s %-6d %10d %10d %8s" % (
        t, len(t), like, fts, ("%.1f%%" % (100.0 * fts / like)) if like else "-"))
print("合计: LIKE=%d  FTS=%d  覆盖率=%.1f%%" % (tot_like, tot_fts, 100.0 * tot_fts / max(1, tot_like)))

print()
print("--- 2 字词用 trigram 是否必然 0：构造性验证 ---")
for t in ("地铁", "abc"):
    n = cur.execute("select count(*) from posts_fts where posts_fts match ?", (t,)).fetchone()[0]
    print("  MATCH %-6s -> %d" % (t, n))
print("--- 3 字窗口版（把 2 字词补成 3 字）---")
for t in ("地铁站", "公交车", "换乘站"):
    n = cur.execute("select count(*) from posts_fts where posts_fts match ?", (t,)).fetchone()[0]
    print("  MATCH %-6s -> %d" % (t, n))

print()
print("--- db.search() 实际走的是 fts 还是 like？（看 mode）---")
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序"))
from msyt.db import DB
db = DB(DBP=DB) if False else DB(DB)
for q in ("地铁", "无障碍", "换乘"):
    r = db.search(q, limit=5)
    print("  q=%-6s total=%-7s mode=%-5s took=%.0fms" % (
        q, r.get("total"), r.get("mode"), r.get("took_ms") or 0))
