# -*- coding: utf-8 -*-
"""check_orphan_posts.py —— 找出「有楼层但状态不是 done」的帖（诊断状态被谁改过）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== 有真实楼层、但 detail_state 不是 done 的帖 ===")
    for r in c.execute(
            "SELECT t.tid, t.detail_state, t.posts_n, t.chars, "
            "  (SELECT COUNT(*) FROM posts p WHERE p.tid=t.tid AND p.floor>0) floors "
            "FROM threads t WHERE EXISTS (SELECT 1 FROM posts p "
            "  WHERE p.tid=t.tid AND p.floor>0) "
            "AND COALESCE(t.detail_state,'pending')<>'done' ORDER BY t.tid"):
        print(f"  tid={r['tid']:<10} state={r['detail_state']:<10} "
              f"posts_n={r['posts_n']} chars={r['chars']} 真实楼层={r['floors']}")
    print("\n=== posts 表里 floor>0 的总条数 / 涉及帖数 ===")
    print("  ", c.execute("SELECT COUNT(*) FROM posts WHERE floor>0").fetchone()[0],
          c.execute("SELECT COUNT(DISTINCT tid) FROM posts WHERE floor>0").fetchone()[0])
    print("\n=== threads.posts_n 与真实楼层数对不上的帖 ===")
    for r in c.execute(
            "SELECT t.tid, t.detail_state, t.posts_n, "
            "  (SELECT COUNT(*) FROM posts p WHERE p.tid=t.tid AND p.floor>0) floors "
            "FROM threads t WHERE COALESCE(t.posts_n,-1) <> "
            "  (SELECT COUNT(*) FROM posts p WHERE p.tid=t.tid AND p.floor>0) "
            "AND t.detail_state='done' ORDER BY t.tid LIMIT 20"):
        print(f"  tid={r['tid']:<10} state={r['detail_state']} "
              f"posts_n={r['posts_n']} 真实楼层={r['floors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
