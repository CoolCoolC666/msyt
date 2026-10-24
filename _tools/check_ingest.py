# -*- coding: utf-8 -*-
"""check_ingest.py —— 核对「本机回传的列表元数据」是否真的进了权威库、
且**水位线没有被推**（推了会让管理员的续采漏页）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "dept:555"
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    n = c.execute("SELECT COUNT(*) FROM thread_sources WHERE source=?",
                  (src,)).fetchone()[0]
    print(f"来源 {src} 记到的帖数：{n}")
    wm = c.execute("SELECT * FROM list_watermark WHERE source=?", (src,)).fetchone()
    if wm:
        print(f"  水位线：last_page={wm['last_page']} status={wm['status']} "
              f"rows_total={wm['rows_total']}")
    else:
        print("  （该源没有水位线）")
    print("\n最近 5 条该来源的帖：")
    for r in c.execute(
            "SELECT t.tid, t.title, t.dept_name, t.pub_time FROM thread_sources s "
            "JOIN threads t ON t.tid=s.tid WHERE s.source=? "
            "ORDER BY s.first_seen DESC LIMIT 5", (src,)):
        print(f"  tid={r['tid']:<10} {str(r['title'])[:34]:<36} "
              f"{r['dept_name']} {r['pub_time']}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
