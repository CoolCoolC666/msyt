# -*- coding: utf-8 -*-
"""check_list_changes.py —— 查列表变更留痕表的内容（真实采集之后）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== 留痕表总条数 ===")
    n = c.execute("SELECT COUNT(*) FROM list_changes").fetchone()[0]
    print("  ", n)
    print("\n=== 按 kind 分 ===")
    for r in c.execute("SELECT kind,COUNT(*) n FROM list_changes GROUP BY kind "
                       "ORDER BY n DESC"):
        print(f"  {r['kind']:<10} {r['n']:>6}")
    print("\n=== 按源分 ===")
    for r in c.execute("SELECT source,COUNT(*) n FROM list_changes GROUP BY source "
                       "ORDER BY n DESC LIMIT 8"):
        print(f"  {r['source']:<14} {r['n']:>6}")
    print("\n=== 最近 10 条（受理状态/回复数/标题变化优先看）===")
    for r in c.execute("SELECT * FROM list_changes "
                       "WHERE kind IN ('state','replies','title') "
                       "ORDER BY id DESC LIMIT 10"):
        print(f"  {r['ts']} {r['source']:<12} tid={r['tid']:<9} {r['kind']:<8} "
              f"{str(r['old_val'])[:20]} → {str(r['new_val'])[:20]}")
    print("\n=== fetch_log 里 phase='list' 的最近记录 ===")
    for r in c.execute("SELECT ts,target,kind,msg FROM fetch_log "
                       "WHERE phase='list' ORDER BY id DESC LIMIT 8"):
        print(f"  {r['ts']} {r['target']:<12} {r['kind']:<16} {str(r['msg'])[:60]}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
