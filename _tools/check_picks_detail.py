# -*- coding: utf-8 -*-
"""check_picks_detail.py —— 把 assist_picks 按（任务 × 状态 × 完成时间）摊开看。

为什么需要它
------------
「A4 的完成数从 466 掉到 56」这类怪事，光看汇总数看不出是谁改的。
把 done_at 的分布列出来，一眼就能看出「是哪一批、什么时候被重写的」。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    code = sys.argv[1] if len(sys.argv) > 1 else ""
    print("=== 各任务 picks：状态 × done_at 分布 ===")
    q = ("SELECT task_code, state, substr(COALESCE(done_at,'(无)'),1,16) d, COUNT(*) n "
         "FROM assist_picks")
    args = []
    if code:
        q += " WHERE task_code=?"
        args.append(code)
    q += " GROUP BY task_code, state, d ORDER BY task_code, state, d"
    for r in c.execute(q, args):
        print(f"  {r['task_code']:>4} / {r['state']:<11} {r['d']:<18} {r['n']:>5}")

    print("\n=== 最近的合并/冲突痕迹 ===")
    for r in c.execute("SELECT id,task_code,tid,node,kind,created_at FROM assist_conflicts "
                       "ORDER BY id DESC LIMIT 12"):
        print(f"  {dict(r)}")

    print("\n=== 任务表 ===")
    for r in c.execute("SELECT code,total,done,state,created_at,updated_at FROM assist_tasks"):
        print("  ", dict(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
