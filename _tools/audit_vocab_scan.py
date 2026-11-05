# -*- coding: utf-8 -*-
"""取证：词表一次扫描 vs N 次查询。"""
import os
import sqlite3
import time

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()

TERMS = ["地铁", "公交", "换乘", "晚点", "拥堵", "噪声", "漏水", "停电", "垃圾", "电梯",
         "燃气", "施工", "积水", "违停", "油烟", "扰民", "道路", "停车", "照明", "井盖"]

print("--- 方案A：N 次 LIKE 查询（现状 search() 的兜底方式）---")
t0 = time.time()
cnt_a = {t: 0 for t in TERMS}
for t in TERMS:
    cnt_a[t] = cur.execute(
        "select count(*) from posts where pid>0 and content like ?", ("%" + t + "%",)).fetchone()[0]
el_a = time.time() - t0
print("  耗时 %.1fs" % el_a)

print("--- 方案B：一次全表扫描 + Python 端多词判定 ---")
t0 = time.time()
cnt_b = {t: 0 for t in TERMS}
for (content,) in cur.execute("select content from posts where pid>0"):
    if not content:
        continue
    for t in TERMS:
        if t in content:
            cnt_b[t] += 1
el_b = time.time() - t0
print("  耗时 %.1fs" % el_b)

print("--- 方案C：一次 SQL 扫描（CASE 判定 20 词）---")
cases = " ".join("WHEN content LIKE ? THEN 1 ELSE 0 END" for _ in TERMS)
t0 = time.time()
sql = ("select " + ", ".join(
    "sum(case %s end)" % cases.replace("WHEN content LIKE ? THEN 1 ELSE 0 END",
                                       "WHEN content LIKE ? THEN 1 ELSE 0 END")
    for _ in [0]) + " from posts where pid>0")
# 上面构造易错，改为逐词 COUNT 的单次扫描写法：
sql = "select " + ", ".join("sum(case when content like ? then 1 else 0 end)" for _ in TERMS) \
      + " from posts where pid>0"
row = cur.execute(sql, ["%" + t + "%" for t in TERMS]).fetchone()
el_c = time.time() - t0
print("  耗时 %.1fs" % el_c)

print()
print("%-8s %10s %10s %10s" % ("词", "A:N次查询", "B:Python扫", "C:SQL扫"))
for i, t in enumerate(TERMS):
    print("%-8s %10d %10d %10d" % (t, cnt_a[t], cnt_b[t], row[i]))
print("加速比：B 比 A 快 %.1f 倍；C 比 A 快 %.1f 倍" % (el_a / max(el_b, .001), el_a / max(el_c, .001)))
