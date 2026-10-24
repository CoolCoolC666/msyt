# -*- coding: utf-8 -*-
"""check_state_and_logs.py —— 查两件事：
① 受理状态（threads.state）的真实分布，含空值与异常取值
② 列表采集阶段现在留了什么痕（fetch_log 里 phase='list' 的记录）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def main() -> int:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    print("=== threads.state（受理状态）分布 ===")
    for r in c.execute("SELECT COALESCE(state,'(空)') s, COUNT(*) n FROM threads "
                       "GROUP BY COALESCE(state,'(空)') ORDER BY n DESC"):
        print(f"  {r['s']:<12} {r['n']:>8}")
    print("\n=== state 为空的那批长什么样（取 5 条） ===")
    for r in c.execute("SELECT tid,title,category,mailbox_id,light_code,replies,"
                       "pub_time,detail_state FROM threads "
                       "WHERE COALESCE(state,'')='' LIMIT 5"):
        print(f"  tid={r['tid']:<10} 类别={r['category']!s:<6} 回帖数={r['replies']} "
              f"发帖={r['pub_time']} 详情={r['detail_state']}")
        print(f"     标题：{str(r['title'])[:44]}")
    print("\n=== state 为空 + 已有正文的（可能本该有状态） ===")
    n = c.execute("SELECT COUNT(*) FROM threads WHERE COALESCE(state,'')='' "
                  "AND detail_state='done'").fetchone()[0]
    print("  ", n)

    print("\n=== fetch_log 里 phase='list' 的留痕（最近 12 条） ===")
    for r in c.execute("SELECT ts,target,kind,ok,msg FROM fetch_log "
                       "WHERE phase='list' ORDER BY id DESC LIMIT 12"):
        print(f"  {r['ts']} {r['target']:<14} {r['kind']:<10} ok={r['ok']} "
              f"{str(r['msg'])[:66]}")
    print("\n=== fetch_log 里 phase='list' 的 kind 分布 ===")
    for r in c.execute("SELECT kind,COUNT(*) n FROM fetch_log WHERE phase='list' "
                       "GROUP BY kind ORDER BY n DESC"):
        print(f"  {r['kind']:<12} {r['n']:>6}")
    print("\n=== 库里有没有「列表变更留痕」这类表 ===")
    print("  ", [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
