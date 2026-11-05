# -*- coding: utf-8 -*-
import os
import re
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()

print("--- 匹配 '%ai%' 的列（确认是不是误命中）---")
print([t[1] for t in cur.execute("pragma table_info('threads')")
       if "ai" in t[1].lower()])
print("--- threads 全部列里有没有 摘要/初筛/复核/规则/关键词 ---")
cols = [t[1] for t in cur.execute("pragma table_info('threads')")]
print(cols)
print("--- posts 全部列 ---")
print([t[1] for t in cur.execute("pragma table_info('posts')")])

print()
print("--- 导出接口的字段（正式样本导出长什么样）---")
wp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "msyt", "webapp.py")
txt = open(wp, encoding="utf-8").read()
i = txt.find('"/api/export"')
if i < 0:
    i = txt.find("'/api/export'")
seg = txt[i:i + 3000]
# 抓 FIELDS / SELECT 列表
for m in re.finditer(r"(FIELDS|COLS|SELECT)\s*=\s*\[(.*?)\]", seg, re.S):
    print("----", m.group(1), "----")
    print(m.group(2)[:1200])
m = re.search(r"select\s+(.*?)\s+from\s+threads", seg, re.S | re.I)
if m:
    print("---- SQL SELECT ----")
    print(m.group(1)[:800])
m = re.search(r"from posts", seg, re.I)
print("---- 片段里提到 posts 吗 ----", bool(m))
print(seg[:1500])
