# -*- coding: utf-8 -*-
"""check_task_state.py —— 看协作任务与各池的最终状态（核对撤销/恢复的净效果）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== 任务状态 ===")
    for r in c.execute("SELECT code,state,done,total FROM assist_tasks ORDER BY id"):
        print(f"  {r['code']:<4} state={r['state']:<11} done={r['done']}/{r['total']}")
    print("\n=== 各池的活 ===")
    for r in c.execute("SELECT task_code,state,COUNT(*) n FROM assist_picks "
                       "GROUP BY task_code,state ORDER BY task_code,state"):
        print(f"  {r['task_code']:<4} / {r['state']:<11} {r['n']:>6}")
    print("\n=== 帖子的详情状态分布 ===")
    for r in c.execute("SELECT detail_state,COUNT(*) n FROM threads "
                       "GROUP BY detail_state ORDER BY n DESC"):
        print(f"  {str(r['detail_state'] or '(空)'):<12} {r['n']:>8}")
    print("\n=== 正文楼层数 ===")
    print("  真实楼层(p>0)：", c.execute(
        "SELECT COUNT(*) FROM posts WHERE pid>0").fetchone()[0])
    print("  标记行(p<0)：", c.execute(
        "SELECT COUNT(*) FROM posts WHERE pid<0").fetchone()[0])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
