# -*- coding: utf-8 -*-
"""check_log.py —— 查采集日志与详情队列，定位「状态被谁改成 pending」。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"
TIDS = [8332684, 8337073, 8339870, 8340121, 8340283]


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("=== 表清单 ===")
    print("  ", tables)
    log_tables = [t for t in tables if "log" in t.lower()]
    for lt in log_tables:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({lt})")]
        print(f"\n=== {lt} 的列 === {cols}")
        if "tid" in cols:
            for t in TIDS:
                rows = c.execute(
                    f"SELECT * FROM {lt} WHERE tid=? ORDER BY rowid DESC LIMIT 5",
                    (t,)).fetchall()
                print(f"  --- tid={t} ---")
                for r in rows:
                    print("   ", dict(r))
    print("\n=== 这 5 帖的 detail_queue ===")
    for t in TIDS:
        r = c.execute("SELECT * FROM detail_queue WHERE tid=?", (t,)).fetchone()
        print(f"  tid={t}: {dict(r) if r else '(不在队列)'}")
    print("\n=== 队列状态分布 ===")
    for r in c.execute("SELECT state, COUNT(*) n FROM detail_queue GROUP BY state"):
        print(f"  {r['state']:<10} {r['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
