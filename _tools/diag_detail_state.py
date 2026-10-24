# -*- coding: utf-8 -*-
"""diag_detail_state.py —— 本地库里「详情采到了多少」的真相。

为什么要它
----------
站方对**历史帖子**只给「提示信息」页（"历史帖子仅限作者本人查看"），
所以不是所有帖子都能采到正文。协作池如果装满了这种帖子，
成员采一百条全是空 → 进度永远为 0 → 看着像程序坏了。

先看清本地现状：多少帖采到了、采到的是哪些时期的帖子。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== threads.detail_state 分布 ===")
    for r in c.execute("SELECT detail_state, COUNT(*) n FROM threads "
                       "GROUP BY detail_state ORDER BY n DESC"):
        print(f"  {str(r['detail_state'] or '(空)'):<12} {r['n']:>8}")
    print("\n=== posts 表实际行数 ===")
    print("  ", c.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
    print("\n=== 采到正文的帖子：tid 范围与时间 ===")
    for r in c.execute(
            "SELECT MIN(tid) mn, MAX(tid) mx, COUNT(*) n FROM threads "
            "WHERE detail_state='done'"):
        print(f"  done 帖数 {r['n']}  tid {r['mn']} ~ {r['mx']}")
    print("\n=== 抽 5 条 done 帖看内容长度 ===")
    for r in c.execute(
            "SELECT t.tid, t.title, COUNT(p.pid) np, SUM(p.chars) ch "
            "FROM threads t LEFT JOIN posts p ON p.tid=t.tid "
            "WHERE t.detail_state='done' GROUP BY t.tid LIMIT 5"):
        print(f"  tid={r['tid']} 楼层={r['np']} 字数={r['ch']} 标题={str(r['title'])[:40]}")
    print("\n=== 抽 5 条非 done 帖（最大 tid）===")
    for r in c.execute(
            "SELECT tid, detail_state, posts_n, chars FROM threads "
            "WHERE detail_state!='done' ORDER BY tid DESC LIMIT 5"):
        print(f"  {dict(r)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
