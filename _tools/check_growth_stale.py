# -*- coding: utf-8 -*-
"""check_growth_stale.py —— 核对「受理状态变化」被标成待重采（stale）的帖。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT l.tid, l.kind, l.old_val, l.new_val, l.ts, "
        "       t.detail_state, t.state, t.replies, t.posts_n, "
        "       q.state qstate, q.last_error "
        "FROM list_changes l "
        "LEFT JOIN threads t ON t.tid=l.tid "
        "LEFT JOIN detail_queue q ON q.tid=l.tid "
        "WHERE l.kind IN ('state','replies') "
        "ORDER BY l.id DESC LIMIT 12").fetchall()
    print("=== 状态/回复数变化的帖，现在是什么状态 ===")
    for r in rows:
        print(f"  tid={r['tid']:<10} {r['kind']:<8} {str(r['old_val'])[:8]}→"
              f"{str(r['new_val'])[:8]}  详情={r['detail_state']}  "
              f"队列={r['qstate']}  受理={r['state']}  回帖={r['replies']}  "
              f"楼={r['posts_n']}")
        if r["last_error"]:
            print(f"      队列说明：{str(r['last_error'])[:70]}")
    print("\n=== detail_state 分布 ===")
    for r in c.execute("SELECT detail_state,COUNT(*) n FROM threads "
                       "GROUP BY detail_state ORDER BY n DESC"):
        print(f"  {str(r['detail_state'] or '(空)'):<12} {r['n']:>8}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
