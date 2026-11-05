# -*- coding: utf-8 -*-
"""统计楼层发帖时间回填的工作量。"""
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()

print("done 帖数        :", cur.execute(
    "select count(*) from threads where detail_state='done'").fetchone()[0])
print("空时间的真实楼层 :", cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='')").fetchone()[0])
print("需要翻的页数合计 :", cur.execute(
    "select count(*) from (select distinct tid,page_no from posts where pid>0)").fetchone()[0])
print("页数分布 top10   :", [list(r) for r in cur.execute(
    "select pages,count(*) from (select tid,count(distinct page_no) pages from posts "
    "where pid>0 group by tid) group by pages order by pages limit 10")])
print("总页数上限       :", cur.execute(
    "select max(c) from (select count(distinct page_no) c from posts where pid>0 group by tid)").fetchone()[0])
print()
print("已有时间的楼层   :", cur.execute(
    "select count(*) from posts where pid>0 and post_time<>''").fetchone()[0])
print("已空的楼层里 tid 覆盖 :", cur.execute(
    "select count(distinct tid) from posts where pid>0 and (post_time is null or post_time='')").fetchone()[0])
