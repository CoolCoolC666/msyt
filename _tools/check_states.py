# -*- coding: utf-8 -*-
"""check_states.py —— 核对 threads.detail_state 的分布与明细（诊断用）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== restricted 帖 ===")
    for r in c.execute("SELECT tid,posts_n,chars,detail_warn FROM threads "
                       "WHERE detail_state='restricted' ORDER BY tid"):
        print(f"  tid={r['tid']:<10} posts_n={r['posts_n']} chars={r['chars']} "
              f"warn={str(r['detail_warn'])[:40]}")
    print("\n=== restricted 帖里 floor>0 的真实楼层（应为 0）===")
    for r in c.execute("SELECT tid,COUNT(*) n FROM posts "
                       "WHERE tid IN (SELECT tid FROM threads "
                       "WHERE detail_state='restricted') AND floor>0 GROUP BY tid"):
        print(f"  tid={r['tid']} 真实楼层 {r['n']}")
    print("\n=== done 帖 ===")
    for r in c.execute("SELECT tid,posts_n,chars FROM threads "
                       "WHERE detail_state='done' ORDER BY tid"):
        print(f"  tid={r['tid']:<10} posts_n={r['posts_n']} chars={r['chars']}")
    print("\n=== 标记行（pid<0）条数 ===")
    print("  ", c.execute("SELECT COUNT(*) FROM posts WHERE pid<0").fetchone()[0])
    print("\n=== 帖子总数 / 状态分布 ===")
    for r in c.execute("SELECT detail_state,COUNT(*) n FROM threads "
                       "GROUP BY detail_state ORDER BY n DESC"):
        print(f"  {str(r['detail_state'] or '(空)'):<12} {r['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
