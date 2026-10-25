# -*- coding: utf-8 -*-
"""清掉「孤儿活」：threads 已终态、但 detail_queue 还停在 pending/running 的行。

★ 什么是孤儿活
  `threads.detail_state`（帖子的采集状态）与 `detail_queue.state`（队列行状态）
  是**两张独立的表**，改前者**不会**自动改后者。
  所以只要某条路径只改了前者，队列里就会挂着一行「注定白跑的活」：
  · 领活是**按 pub_ts 升序**的 → 它们排在队首
  · worker 一上来就撞上它们 → 每帖白跑一趟请求
  · 产能被占满，日志看着像「没在干活」

★ 两个来源（本项目都真实发生过）
  ① 2026-10-23：`mark_restricted()` 只写 threads、没碰 detail_queue
     → 14,636 条（受限却排队）
  ② 2026-10-25：**组员回传**（`assist.submit`）只写 `threads.detail_state='done']`
     → 231 条（已采完却排队）

★ 现在两个源头都修好了（`mark_restricted` 会摘队列、`assist.submit` 会调
  `db.finish_queue`），但**修之前积下的存量**要手动清一次 —— 就是这个脚本。

★ 只动本地库，不碰站方、不打任何 HTTP 接口。

用法（三段式，默认不写库）：
    python _tools\\clean_queue_orphans.py            # dry-run：只清点
    python _tools\\clean_queue_orphans.py --apply    # 真清
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time

ROOT = (r"【用户桌面】\14-网络舆情分析与监测-2.0"
        r"\2-作业\02 民生e网通")
DB = os.path.join(ROOT, "程序", "data", "msyt.db")

ap = argparse.ArgumentParser(description="清理队列孤儿活（threads 已终态、队列还 pending）")
ap.add_argument("--apply", action="store_true", help="真写库（默认只清点）")
args = ap.parse_args()

conn = sqlite3.connect(DB, timeout=60)
conn.row_factory = sqlite3.Row
now = time.strftime("%Y-%m-%d %H:%M:%S")

# ★ 一次把孤儿活全取出来，内存里统计
#   （原来写成 3 个 COUNT + EXISTS 子查询，每次都是 60 万行 threads 全表扫 →
#    实测要 3 分钟以上。孤儿活是「一次性运维工具」，不能慢成那样。）
_orph = conn.execute(
    "SELECT q.tid, t.detail_state FROM detail_queue q "
    "JOIN threads t ON t.tid=q.tid "
    "WHERE q.state IN ('pending','running') "
    "AND t.detail_state IN ('restricted','done')").fetchall()
_orph_tids = [r["tid"] for r in _orph]
_n_r = sum(1 for r in _orph if r["detail_state"] == "restricted")
_n_d = sum(1 for r in _orph if r["detail_state"] == "done")

print("### 清点")
print(f"  孤儿活（受限却排队）：{_n_r:,}")
print(f"  孤儿活（已采完却排队）：{_n_d:,}")
print(f"  合计：{len(_orph):,}")

# 归因：这些孤儿活来自哪条路径？（assist_picks 命中 = 组员回传）
_n_pick = 0
if _orph_tids:
    # 分批 IN 查询，避免 SQLite 的变量数上限（默认 999）
    _B = 500
    for _i in range(0, len(_orph_tids), _B):
        _chunk = _orph_tids[_i:_i + _B]
        _q = ",".join("?" * len(_chunk))
        _n_pick += conn.execute(
            f"SELECT COUNT(DISTINCT tid) FROM assist_picks WHERE tid IN ({_q})",
            _chunk).fetchone()[0]
if _orph:
    print(f"  其中在 assist_picks 里（= 组员回传造成）：{_n_pick:,} / {len(_orph):,}")
    print("  ★ 队列行 attempts=0 且 updated_at 停在入队那一刻 = "
          "这行从没被采集路径领过 → 只能被「只改主表的那条路径」解释")

if not args.apply:
    print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
    conn.close()
    raise SystemExit(0)

print("\n### 清理：受限的 → 队列置 restricted")
cur = conn.execute(
    "UPDATE detail_queue SET state='restricted', last_error=?, updated_at=? "
    "WHERE state IN ('pending','running') "
    "AND tid IN (SELECT t.tid FROM threads t WHERE t.detail_state='restricted')",
    ("历史遗留：站方限制（正文不公开），已终态化", now))
conn.commit()
print(f"  更新 {cur.rowcount:,} 行")

print("\n### 清理：已采完的 → 队列置 done")
cur = conn.execute(
    "UPDATE detail_queue SET state='done', updated_at=? "
    "WHERE state='pending' "
    "AND tid IN (SELECT t.tid FROM threads t WHERE t.detail_state='done')", (now,))
conn.commit()
print(f"  更新 {cur.rowcount:,} 行")

print("\n### 清点（清完）")
for r in conn.execute("SELECT state, COUNT(*) n FROM detail_queue GROUP BY state "
                       "ORDER BY n DESC"):
    print(f"  {r['state']:<12} {r['n']:>9,}")
left = conn.execute(
    "SELECT COUNT(*) FROM detail_queue q JOIN threads t ON t.tid=q.tid "
    "WHERE q.state IN ('pending','running') "
    "AND t.detail_state IN ('restricted','done')").fetchone()[0]
print(f"  剩下的孤儿活：{left}  ← 应为 0")

print("\n### 队首现在是哪些（worker 下一个会领到的）")
for r in conn.execute(
        """SELECT q.tid, t.detail_state, t.pub_time, t.pub_ts
            FROM detail_queue q JOIN threads t ON t.tid=q.tid
            WHERE q.state='pending'
            ORDER BY COALESCE(t.pub_ts, 0) ASC, q.tid ASC LIMIT 8"""):
    d = (time.strftime("%Y-%m-%d", time.localtime(r["pub_ts"]))
         if r["pub_ts"] else "（pub_ts 为空）")
    print(f"  tid={r['tid']} {d} 状态={r['detail_state']}")
conn.close()
