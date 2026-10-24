# -*- coding: utf-8 -*-
"""check_one_thread.py —— 看某一帖在库里的状态与规模（诊断 agent 卡在哪）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    tids = [int(x) for x in sys.argv[1:]] or [8343703]
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    for t in tids:
        r = c.execute("SELECT tid,title,replies,views,detail_state,posts_n,chars,"
                      "pub_time FROM threads WHERE tid=?", (t,)).fetchone()
        print(f"tid={t}: {dict(r) if r else '(不在库里)'}")
        for p in c.execute("SELECT * FROM assist_picks WHERE tid=?", (t,)):
            print("   pick:", dict(p))
        n = c.execute("SELECT COUNT(*) FROM posts WHERE tid=?", (t,)).fetchone()[0]
        print(f"   已入库楼层 {n}")
    print("\n=== 最近 10 条 fetch_log ===")
    for r in c.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT 10"):
        print("  ", dict(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
