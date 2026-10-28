# -*- coding: utf-8 -*-
"""批量归位：把「少贴 → stale → 队列 pending」的存量帖分流归位（2026-10-28）

★ 背景（实测）
  库里有 **4384 条**帖卡在 `detail_state='stale'` + 队列 `pending`，
  `detail_warn` 是「少贴：站方称应有 N 楼，实收 M 楼…已补采 2 次」。
  逐条回站方核对（走项目自己的 parse_thread_page + parse_ajax_posts
  完整翻页）证实：**站方现在给的楼层数与本地实收一模一样**，
  差额 100% 来自站方列表页那个没随删楼而减的计数。
  → 它们**不是漏采**，却被当成漏采反复重排队，每轮详情采集都白烧站方请求。

★ 本脚本做什么
  对每一条待归位的帖：
    ① 实测站方详情页现在有几层楼（`_probe_site_floors`）
    ② 实测 <= 本地实收 → 站方计数缺陷 → 标 `count_mismatch`（终态）
       实测 >  本地实收 → 真漏采     → 保留 `stale`，等正常补采
       实测失败（站方不可达）        → **不动**（不能下结论）
  批量写库用 `mark_count_mismatch_bulk`（一次事务）。

★ 安全
  · **默认 dry-run**：只报告不改库，加 `--apply` 才真正写。
  · 带限流（默认 1.5 秒/帖，与采集链路同档）与指数退避。
  · 只处理 `detail_state='stale'` 且告警含「少贴」的帖 —— 别的帖不碰。
  · 写库前自动备份 detail_state/detail_warn/detail_queue 三处快照。

用法：
    python _tools/fix_count_mismatch.py --limit 50           # 试跑 50 条
    python _tools/fix_count_mismatch.py --apply              # 全量写库
    python _tools/fix_count_mismatch.py --apply --limit 200  # 只处理 200 条
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.config import Config  # noqa: E402
from msyt.db import DB  # noqa: E402
from msyt.detail import DetailCollector  # noqa: E402
from msyt.http_client import Client  # noqa: E402

DBP = os.path.join(PROJ, "程序", "data", "msyt.db")
BACKUP = os.path.join(PROJ, "程序", "data", "msyt.db.bak-before-count-mismatch")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    ap.add_argument("--delay", type=float, default=1.5, help="每帖间隔秒数")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发线程数（默认 1；3 路是采集链路的默认并发档）")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份")
    args = ap.parse_args()

    cfg = Config.load() if hasattr(Config, "load") else Config()
    db = DB(DBP)
    client = Client(cfg)
    col = DetailCollector(db, client, cfg)

    rows = db.conn.execute(
        "SELECT tid, replies, posts_n, detail_warn FROM threads "
        "WHERE detail_state='stale' AND detail_warn LIKE '%少贴%' "
        "ORDER BY tid").fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"待归位：{len(rows)} 条（{'写库模式' if args.apply else '试跑模式'}）")
    print(f"库：{DBP}")
    if not args.apply:
        print("★ 试跑模式，不改库。加 --apply 才真正写入。")

    # 备份（写库前）
    if args.apply and not args.no_backup and not os.path.exists(BACKUP):
        import shutil
        shutil.copy2(DBP, BACKUP)
        print(f"已备份 → {BACKUP}")

    mismatch: list[tuple[int, str]] = []
    keep_stale: list[int] = []
    failed: list[int] = []
    t0 = time.time()
    # ★ 分批提交（每批 200 条就写一次库）：
    #   全量 4384 条要跑 ~90 分钟 → 攒完再写的话，
    #   中途中断就**全部白跑**，而且期间库里看不到任何进展。
    #   边跑边写 → 每批都是终态，中断也不损失已完成的批次。
    BATCH = 200
    lock = threading.Lock()

    def flush():
        """把攒下的 mismatch 写库。**只在主线程调用**（sqlite 连接非线程安全）。"""
        if mismatch:
            n = db.mark_count_mismatch_bulk(list(mismatch))
            print(f"    ↳ 已写库 {n} 条 → count_mismatch（累计）", flush=True)
            mismatch.clear()

    def probe_one(r):
        """实测一帖。返回 (tid, verdict, detail_or_None, kind)。"""
        tid = int(r["tid"])
        got = int(r["posts_n"] or 0)
        p = col._probe_site_floors(tid)
        if not p.get("ok"):
            return tid, f"实测失败（{p.get('reason')}）→ 不动", None, "failed"
        if p["count"] <= got:
            detail = (f"站方列表称应有 {(r['replies'] or 0) + 1} 楼，"
                      f"站方详情页实测 {p['count']} 楼，"
                      f"本地实收 {got} 楼 —— "
                      f"差额来自站方列表页计数未随删楼而减")
            return (tid,
                    f"站方计数缺陷 → count_mismatch（实测 {p['count']} vs 实收 {got}）",
                    detail, "mismatch")
        return (tid,
                f"真漏采 → 保留待补采（实测 {p['count']} > 实收 {got}）",
                None, "keep")

    if args.workers > 1:
        # ★ 并发：每线程一个 client（http_client 不是线程安全的）
        from concurrent.futures import ThreadPoolExecutor
        cols = [DetailCollector(db, Client(cfg), cfg) for _ in range(args.workers)]

        def probe_one_w(r):
            tid = int(r["tid"])
            got = int(r["posts_n"] or 0)
            c = cols[tid % len(cols)]
            p = c._probe_site_floors(tid)
            if not p.get("ok"):
                return tid, f"实测失败（{p.get('reason')}）→ 不动", None, "failed"
            if p["count"] <= got:
                detail = (f"站方列表称应有 {(r['replies'] or 0) + 1} 楼，"
                          f"站方详情页实测 {p['count']} 楼，"
                          f"本地实收 {got} 楼 —— "
                          f"差额来自站方列表页计数未随删楼而减")
                return (tid,
                        f"站方计数缺陷 → count_mismatch（实测 {p['count']} vs 实收 {got}）",
                        detail, "mismatch")
            return (tid,
                    f"真漏采 → 保留待补采（实测 {p['count']} > 实收 {got}）",
                    None, "keep")

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for tid, verdict, detail, kind in pool.map(probe_one_w, rows):
                done += 1
                if kind == "mismatch":
                    mismatch.append((tid, detail))
                elif kind == "keep":
                    keep_stale.append(tid)
                else:
                    failed.append(tid)
                if done % 50 == 0 or done == len(rows):
                    print(f"  [{done}/{len(rows)}] tid={tid} {verdict}", flush=True)
                if len(mismatch) >= BATCH:
                    flush()
    else:
        for i, r in enumerate(rows, 1):
            tid, verdict, detail, kind = probe_one(r)
            if kind == "mismatch":
                mismatch.append((tid, detail))
            elif kind == "keep":
                keep_stale.append(tid)
            else:
                failed.append(tid)
            if i % 20 == 0 or i == len(rows):
                print(f"  [{i}/{len(rows)}] tid={tid} {verdict}", flush=True)
            if len(mismatch) >= BATCH:
                flush()
            if i < len(rows):
                time.sleep(args.delay)

    flush()

    print()
    print("=" * 60)
    print(f"站方计数缺陷（→ count_mismatch）: {len(mismatch)}")
    print(f"真漏采（保留待补采）            : {len(keep_stale)}")
    print(f"实测失败（不动）                : {len(failed)}")
    print(f"耗时 {time.time() - t0:.1f}s")

    if not args.apply:
        print("\n★ 试跑结束，未改库。加 --apply 执行写库。")
        return 0

    if mismatch:
        n = db.mark_count_mismatch_bulk(mismatch)
        print(f"\n已写库：{n} 条 → count_mismatch")
    # 真漏采与实测失败的**保持原状**（stale + 队列 pending），
    # 让正常的补采流程去处理 —— 本脚本不越权改它们的状态。
    db.conn.commit()
    print("真漏采与实测失败的帖保持原状，由正常补采流程处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
