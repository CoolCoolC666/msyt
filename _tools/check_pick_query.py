# -*- coding: utf-8 -*-
"""check_pick_query.py —— 直接跑一遍 agent 领活那条 SQL，看它到底能选出什么。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== assist_tasks 状态 ===")
    for r in c.execute("SELECT code,title,total,done,state FROM assist_tasks"):
        print("  ", dict(r))
    print("\n=== 领活 SQL（agent 走的 JOIN 分支）===")
    rows = c.execute(
        "SELECT p.* FROM assist_picks p JOIN assist_tasks t "
        "ON t.code=p.task_code "
        "WHERE p.state='pending' AND t.state IN ('open','active') "
        "ORDER BY p.id LIMIT 5").fetchall()
    print(f"  选出 {len(rows)} 行")
    for r in rows:
        print("   ", dict(r))
    print("\n=== 各任务各状态的 picks 数 ===")
    for r in c.execute("SELECT task_code,state,COUNT(*) n FROM assist_picks "
                       "GROUP BY task_code,state ORDER BY task_code,state"):
        print(f"  {r['task_code']:>4} / {r['state']:<11} {r['n']:>6}")
    print("\n=== 被 node 占着但超时的活（taken 未回传）===")
    for r in c.execute("SELECT task_code,state,node,taken_at,COUNT(*) n "
                       "FROM assist_picks WHERE state='taken' GROUP BY task_code,state,node,taken_at "
                       "ORDER BY taken_at LIMIT 10"):
        print("  ", dict(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
