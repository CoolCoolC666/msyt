# -*- coding: utf-8 -*-
"""check_pick_rows.py —— 看指定的 picks 行的每一个字段（定位谁改的状态）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    code = sys.argv[1] if len(sys.argv) > 1 else "A4"
    state = sys.argv[2] if len(sys.argv) > 2 else "pending"
    print(f"=== {code} / {state} 的前 12 行 ===")
    for r in c.execute("SELECT * FROM assist_picks WHERE task_code=? AND state=? "
                       "ORDER BY id LIMIT 12", (code, state)):
        print("  ", dict(r))
    print(f"\n=== {code} / {state} 里 node 非空的条数 ===")
    n = c.execute("SELECT COUNT(*) FROM assist_picks WHERE task_code=? AND state=? "
                  "AND node IS NOT NULL", (code, state)).fetchone()[0]
    print("  ", n)
    print(f"\n=== {code} / {state} 里 done_at 非空的条数 ===")
    n2 = c.execute("SELECT COUNT(*) FROM assist_picks WHERE task_code=? AND state=? "
                   "AND done_at IS NOT NULL", (code, state)).fetchone()[0]
    print("  ", n2)
    print("\n=== 这些 tid 在 threads 里的状态（采到了吗）===")
    rows = c.execute("SELECT tid FROM assist_picks WHERE task_code=? AND state=? "
                     "LIMIT 6", (code, state)).fetchall()
    for r in rows:
        t = c.execute("SELECT tid,detail_state,posts_n,chars FROM threads WHERE tid=?",
                      (r["tid"],)).fetchone()
        print("  ", dict(t) if t else f"tid={r['tid']} (不在库里)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
