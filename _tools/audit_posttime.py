# -*- coding: utf-8 -*-
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()

print("--- post_time 空但 post_ts 有值？（说明是格式化/解析问题，不是站方没给）---")
print(cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='') "
    "and post_ts>0").fetchone()[0], "条：post_time 空但 post_ts>0")
print(cur.execute(
    "select count(*) from posts where pid>0 and (post_time is null or post_time='') "
    "and (post_ts is null or post_ts=0)").fetchone()[0], "条：post_time 空且 post_ts 也空")
print("抽样 5 条 post_time 空的：")
for r in cur.execute(
        "select pid,floor,post_time,post_ts,author,substr(content,1,40) from posts "
        "where pid>0 and (post_time is null or post_time='') limit 5"):
    print("   ", r)
print("对照：post_time 非空的 3 条：")
for r in cur.execute(
        "select pid,floor,post_time,post_ts,author from posts "
        "where pid>0 and post_time<>'' limit 3"):
    print("   ", r)

print()
print("--- assist_conflicts.resolved 的取值分布 ---")
print([list(r) for r in cur.execute(
    "select resolved,count(*) from assist_conflicts group by resolved")])
print("kind 分布:", [list(r) for r in cur.execute(
    "select kind,count(*) from assist_conflicts group by kind order by 2 desc limit 8")])

print()
print("--- 楼层级还有哪些字段缺失（图片/作者）---")
print("author 为空的楼层:", cur.execute(
    "select count(*) from posts where pid>0 and (author is null or author='')").fetchone()[0])
