# -*- coding: utf-8 -*-
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()

print("--- post_time 空的楼层，floor 分布 ---")
print("floor<=5 :", cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='') and floor<=5").fetchone()[0])
print("floor<=10:", cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='') and floor<=10").fetchone()[0])
print("floor>10 :", cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='') and floor>10").fetchone()[0])
print("floor<=0 :", cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='') and floor<=0").fetchone()[0])

print()
print("--- 同一个帖里：有时间的楼层 vs 没时间的楼层（取 3 个帖看）---")
for tid in (8333204, 8236013, 8294530):
    a = cur.execute(
        "select count(*) from posts where tid=? and pid>0 and post_time<>''", (tid,)).fetchone()[0]
    b = cur.execute(
        "select count(*) from posts where tid=? and pid>0 and (post_time is null or post_time='')", (tid,)).fetchone()[0]
    print("  tid=%d 有时间 %d / 无时间 %d" % (tid, a, b))

print()
print("--- pid 区间分布（无时间的更集中在早期？）---")
for lo, hi, label in ((0, 80000000, "pid<8e7"), (80000000, 100000000, "8e7~1e8"),
                      (100000000, 113000000, "1e8~1.13e8"), (113000000, 999999999, "pid>1.13e8")):
    tot = cur.execute("select count(*) from posts where pid>0 and pid>=? and pid<?", (lo, hi)).fetchone()[0]
    miss = cur.execute("select count(*) from posts where pid>0 and pid>=? and pid<? "
                        "and (post_time is null or post_time='')", (lo, hi)).fetchone()[0]
    print("  %-14s 共 %7d  无时间 %7d  (%.1f%%)" % (label, tot, miss, 100.0 * miss / max(1, tot)))
