# -*- coding: utf-8 -*-
"""fix_pool.py —— 修协作池（2026-10-23 站方可见性策略调整后的一次性修复）。

背景
----
站方对**历史帖子**只给「提示信息」页（"历史帖子仅限作者本人查看"），
详情页正文不公开。原先建池按 `pub_ts ASC`（先旧后新）选帖，
池子里全是这种采不到正文的帖 —— 成员领到的活全是空回传，
于是「完成 0 / 失败 500」，界面看着像程序坏了。

本脚本做四件事：
  ① 把历史上「回传为空」的失败活**改判**为「站方限制」，
     并给对应帖打标记 + 把提示写进正文（这才是它真正的性质）
  ② 回收超时未回传的活（taken → pending）
  ③ 关掉装满死链的旧池子
  ④ 用**先新后旧**的规则重建一个新池子

用法：
    python _tools\\fix_pool.py --reclassify --reclaim --close A1,A2,A3 --new A4 --total 500
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PKG))

from msyt.assist import AssistService      # noqa: E402
from msyt.db import DB                      # noqa: E402
from msyt.hub import Hub                    # noqa: E402
from msyt.config import Config              # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reclassify", action="store_true",
                    help="把「回传为空」的失败活改判为站方限制")
    ap.add_argument("--reclaim", action="store_true", help="回收超时未回传的活")
    ap.add_argument("--close", default="", help="关掉这些任务编号（逗号分隔）")
    ap.add_argument("--new", default="", help="新建任务编号前缀（实际编号由服务端分配）")
    ap.add_argument("--title", default="", help="新任务标题")
    ap.add_argument("--total", type=int, default=500)
    ap.add_argument("--depts", default="733,728,731,558,732,735,727,736,726,730")
    ap.add_argument("--fix-done", action="store_true",
                    help="把「正文其实已采到、但 picks 状态被误改回 pending」的活改回 done")
    ap.add_argument("--fix-restricted", action="store_true",
                    help="把「帖被判站方限制、但 pick 状态被误改」的活改回 restricted")
    args = ap.parse_args()

    cfg = Config.load()
    db = DB(str(cfg.db_path))
    hub = Hub()
    svc = AssistService(db, hub)

    # ---- ⓪ 修复被误改的完成状态 ----
    if args.fix_done:
        # 判据：pick 状态是 pending、但 done_at 非空（说明曾经完成过），
        #      且 threads.detail_state='done'（正文确实在库里）→ 状态被误改。
        rows = db.conn.execute(
            "SELECT p.task_code, p.tid FROM assist_picks p "
            "JOIN threads t ON t.tid=p.tid "
            "WHERE p.state='pending' AND p.done_at IS NOT NULL "
            "AND t.detail_state='done'").fetchall()
        n = 0
        with db.lock:
            for r in rows:
                db.conn.execute(
                    "UPDATE assist_picks SET state='done' "
                    "WHERE task_code=? AND tid=?", (r["task_code"], r["tid"]))
                n += 1
            db.conn.commit()
        print(f"[0] 已把 {n} 条「正文已采到但状态被误改」的活改回 done")

    # ---- ⓪b 修复被误改的「站方限制」状态 ----
    if args.fix_restricted:
        # 判据：帖本身被判为 restricted（站方不公开正文），
        #      但它的 pick 状态不是 restricted → 状态被误改。
        rows = db.conn.execute(
            "SELECT p.task_code, p.tid FROM assist_picks p "
            "JOIN threads t ON t.tid=p.tid "
            "WHERE t.detail_state='restricted' AND p.state<>'restricted'").fetchall()
        n = 0
        with db.lock:
            for r in rows:
                db.conn.execute(
                    "UPDATE assist_picks SET state='restricted' "
                    "WHERE task_code=? AND tid=?", (r["task_code"], r["tid"]))
                n += 1
            db.conn.commit()
        print(f"[0b] 已把 {n} 条「帖已判站方限制但 pick 状态被误改」的活改回 restricted")

    # ---- ① 改判「回传为空」 ----
    if args.reclassify:
        rows = db.conn.execute(
            "SELECT DISTINCT p.task_code, p.tid FROM assist_picks p "
            "JOIN assist_conflicts c ON c.task_code=p.task_code AND c.tid=p.tid "
            "WHERE p.state='failed' AND c.kind='empty'").fetchall()
        n = 0
        for r in rows:
            reason = "站方限制：历史帖子仅限作者本人查看"
            db.mark_restricted(int(r["tid"]), reason)
            with db.lock:
                db.conn.execute(
                    "UPDATE assist_picks SET state='restricted' "
                    "WHERE task_code=? AND tid=?", (r["task_code"], r["tid"]))
                db.conn.commit()
            n += 1
        print(f"[1] 已把 {n} 条「回传为空」的失败活改判为「站方限制」"
              f"（并给对应帖打了标记、写入正文）")

    # ---- ② 回收超时活 ----
    if args.reclaim:
        n = svc.reclaim_stale()
        print(f"[2] 已回收 {n} 条超时未回传的活")

    # ---- ③ 关掉旧池 ----
    if args.close:
        codes = [c.strip() for c in args.close.split(",") if c.strip()]
        with db.lock:
            for c in codes:
                db.conn.execute(
                    "UPDATE assist_tasks SET state='closed', updated_at="
                    "datetime('now','localtime') WHERE code=?", (c,))
            db.conn.commit()
        print(f"[3] 已关掉任务 {codes}")

    # ---- ④ 重建新池 ----
    if args.new:
        ids = [int(x) for x in args.depts.split(",") if x.strip()]
        t = svc.create(args.title or "协作采集（新池·先新后旧）", ids,
                       total=args.total, created_by="admin")
        print(f"[4] 已建新任务 {t['code']}：{t['total']} 帖，状态 {t['state']}")
        rows = db.conn.execute(
            "SELECT MIN(tid) mn, MAX(tid) mx FROM assist_picks WHERE task_code=?",
            (t["code"],)).fetchone()
        print(f"    tid 范围 {rows['mn']} ~ {rows['mx']}")

    # ---- 汇总 ----
    print("\n=== 各池现状 ===")
    for r in db.conn.execute(
            "SELECT task_code, state, COUNT(*) n FROM assist_picks "
            "GROUP BY task_code, state ORDER BY task_code, state"):
        print(f"  {r['task_code']:>4} / {r['state']:<11} {r['n']:>6}")
    print("\n=== 帖子的详情状态分布 ===")
    for r in db.conn.execute(
            "SELECT detail_state, COUNT(*) n FROM threads GROUP BY detail_state "
            "ORDER BY n DESC"):
        print(f"  {str(r['detail_state'] or '(空)'):<12} {r['n']:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
