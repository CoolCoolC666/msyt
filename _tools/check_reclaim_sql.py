# -*- coding: utf-8 -*-
"""check_reclaim_sql.py —— 直接拆开跑回收那条 SQL 的每一步（定位为何没命中）。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PKG := PROG))

from msyt.db import DB        # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="msyt_sql_")
    try:
        db = DB(os.path.join(tmp, "t.db"))
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        with db.lock:
            db.conn.executemany(
                "INSERT INTO members(id,email,pw_hash,role,status,node_id,last_seen,"
                "created_at) VALUES(?,?,?,'member','active',?,?,?)",
                [(1, "m1@example.com", "x", "member:live", now, now),
                 (2, "m2@example.com", "x", "member:dead", now, old)])
            db.conn.execute(
                "INSERT INTO assist_picks(task_code,tid,state,node,taken_at,done_at) "
                "VALUES('P1',1,'taken','member:dead',?,NULL)", (now,))
            db.conn.commit()
        print("members:")
        for r in db.conn.execute("SELECT id,email,node_id,last_seen FROM members"):
            print("  ", dict(r))
        off_cut = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(time.time() - 120))
        print("now =", now)
        print("off_cut =", off_cut)
        print("\n子查询单独跑：")
        rows = db.conn.execute(
            "SELECT COALESCE(node_id,'') FROM members "
            "WHERE last_seen IS NULL OR last_seen < ?", (off_cut,)).fetchall()
        print("  命中的节点:", [r[0] for r in rows])
        print("\n完整条件单独跑：")
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - 1800))
        where = ("((taken_at IS NULL OR taken_at < ?) OR "
                 "(node IN (SELECT COALESCE(node_id,'') FROM members "
                 "WHERE last_seen IS NULL OR last_seen < ?)))")
        n = db.conn.execute(
            f"SELECT COUNT(*) FROM assist_picks WHERE state='taken' AND {where}",
            (cutoff, off_cut)).fetchone()[0]
        print("  命中条数:", n)
        print("\nUPDATE 单独跑：")
        cur = db.conn.execute(
            "UPDATE assist_picks SET state='pending', node=NULL, taken_at=NULL "
            f"WHERE state='taken' AND {where}", (cutoff, off_cut))
        print("  改动条数:", cur.rowcount)
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
