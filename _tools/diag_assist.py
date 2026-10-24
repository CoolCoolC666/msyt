# -*- coding: utf-8 -*-
"""diag_assist.py —— 看协作池子的真实状态（picks 明细 + 失败原因）。

为什么需要它
------------
界面上「进度显示不出来」的时候，光看 API 的汇总数不够 ——
得能一眼看到**每个 tid 处于什么状态、失败在哪个环节**。
agent 侧的失败原因不会回传（assist_picks 表里没有 error 字段），
所以这里把服务端能看到的都摊开。
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="", help="只看某个节点")
    ap.add_argument("--task", default="", help="只看某个任务")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row

    print("=== 按 任务×状态 统计 ===")
    for r in c.execute("SELECT task_code,state,COUNT(*) n,SUM(posts) posts "
                       "FROM assist_picks GROUP BY task_code,state "
                       "ORDER BY task_code,state"):
        print(f"  {r['task_code']:>4} / {r['state']:<8} "
              f"条数 {r['n']:>6}  正文合计 {r['posts'] or 0:>6}")

    print("\n=== 任务定义 ===")
    for r in c.execute("SELECT * FROM assist_tasks"):
        d = dict(r)
        print("  " + "  ".join(f"{k}={str(v)[:40]}" for k, v in d.items()))

    q = "SELECT id,task_code,tid,node,state,posts,chars,taken_at,done_at " \
        "FROM assist_picks"
    cond, args2 = [], []
    if args.node:
        cond.append("node LIKE ?")
        args2.append(f"%{args.node}%")
    if args.task:
        cond.append("task_code=?")
        args2.append(args.task)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY id DESC LIMIT ?"
    args2.append(args.limit)
    print(f"\n=== 最近 {args.limit} 条 picks ===")
    for r in c.execute(q, args2):
        print("  " + "  ".join(f"{k}={str(v)[:26]}" for k, v in dict(r).items()))

    print("\n=== 冲突/留痕表（最近 10 条）===")
    try:
        for r in c.execute("SELECT * FROM assist_conflicts ORDER BY id DESC LIMIT 10"):
            print("  " + "  ".join(f"{k}={str(v)[:50]}" for k, v in dict(r).items()))
    except Exception as e:  # noqa: BLE001
        print(f"  （读不到：{e}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
