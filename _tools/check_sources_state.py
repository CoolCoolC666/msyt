# -*- coding: utf-8 -*-
"""check_sources_state.py —— 列出各采集源的水位线状态，找「还没到底」的源。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== 各源水位线 ===")
    for r in c.execute(
            "SELECT source,last_page,status,rows_total FROM list_watermark "
            "ORDER BY last_page DESC LIMIT 20"):
        print(f"  {r['source']:<20} last_page={r['last_page']:<5} "
              f"status={r['status']:<12} rows={r['rows_total']}")
    print("\n=== 部门源里 last_page 最小（最可能没到底）的 ===")
    for r in c.execute(
            "SELECT source,last_page,status,rows_total FROM list_watermark "
            "WHERE source LIKE 'dept:%' AND status<>'done' "
            "ORDER BY last_page ASC LIMIT 12"):
        print(f"  {r['source']:<20} last_page={r['last_page']:<5} "
              f"status={r['status']:<12} rows={r['rows_total']}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
