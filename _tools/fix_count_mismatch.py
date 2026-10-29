# -*- coding: utf-8 -*-
"""批量归位：把「少贴 → stale → 队列 pending」的存量帖分流归位

★ 背景（实测）
  库里有数千条帖卡在 `detail_state='stale'` + 队列 `pending`，
  `detail_warn` 是「少贴：站方称应有 N 楼，实收 M 楼…补采次数：2」。
  逐条回站方核对（走项目自己的 parse_thread_page + parse_ajax_posts
  完整翻页）证实：**站方现在给的楼层数与本地实收一模一样**，
  差额 100% 来自站方列表页那个没随删楼而减的计数。
  → 它们**不是漏采**，却被当成漏采反复重排队，每轮详情采集都白烧站方请求。

★ 本脚本做什么（2026-10-29 用户拍板后改口径）
  对每一条待归位的帖，实测站方详情页现在有几层楼，分四路：
    ① 实测 <= 本地实收 → **正文齐全 = 已采** → 归为 `done`
       （差额原因写进 detail_warn 留痕；**不**另立「站方计数缺陷」状态）
    ② 实测 >  本地实收 → 真漏采   → 保留 `stale`，等正常补采
    ③ 站方提示信息页   → 站方限制 → 归为 `restricted`（终态）
    ④ 实测失败（站方不可达）→ **不动**（不能下结论）
  写库用 `mark_detail_done_bulk` / `mark_restricted_bulk`（各一次事务）。

★ 为什么 ① 是 `done` 而不是另立状态
  2026-10-28 的做法是标 `count_mismatch`（终态、**不计入**「已采详情帖」）。
  但实测 25 条逐层对比证明：这批帖的**正文本来就采全了、站方也照样公开**
  （站方详情页锚点数 == ajax 正文楼层数 == 本地实存楼层数，pid 逐个对得上）。
  → 把它们记成「不计入已采详情帖」= **账面上说它们没采到**，
    而正文明明在库里 —— 这就是「详情采集采不到这些篇目」的真正来源。
  → 正确处置：**正文齐全即已采**。

★ 安全
  · **默认 dry-run**：只报告不改库，加 `--apply` 才真正写。
  · 带限流（默认 1.5 秒/帖，与采集链路同档）与指数退避。
  · 只处理 `detail_state='stale'` 且告警含「少贴」的帖 —— 别的帖不碰。
  · 写库前自动备份 detail_state/detail_warn/detail_queue 三处快照。
  · **分批提交**（每批 200 条）：中途被打断也不白跑。

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
from msyt.detail import DetailCollector, COUNT_VER_MARK  # noqa: E402
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

    done_items: list[tuple[int, str]] = []
    restricted_items: list[int] = []
    keep_stale: list[int] = []
    skipped: list[int] = []
    failed: list[int] = []
    t0 = time.time()
    # ★ 分批提交（每批 200 条就写一次库）：
    #   全量 4384 条要跑 ~90 分钟 → 攒完再写的话，
    #   中途中断就**全部白跑**，而且期间库里看不到任何进展。
    #   边跑边写 → 每批都是终态，中断也不损失已完成的批次。
    BATCH = 200
    lock = threading.Lock()
    #: ★ 已写库条数要**独立于待写列表**：
    #   flush() 会把 done_items 清空（分批提交），
    #   若最后直接 len(done_items) 统计，会显示 0 —— 输出误导。
    done_written = 0

    def flush():
        """把攒下的 done_items 写库。**只在主线程调用**（sqlite 连接非线程安全）。"""
        nonlocal done_written
        if done_items:
            n = db.mark_detail_done_bulk(list(done_items))
            done_written += n
            print(f"    ↳ 已写库 {n} 条 → done（正文齐全，累计）", flush=True)
            done_items.clear()

    def probe_one(r):
        """实测一帖。返回 (tid, verdict, detail_or_None, kind)。"""
        tid = int(r["tid"])
        got = int(r["posts_n"] or 0)
        expect = (r["replies"] or 0) + 1
        p = col._probe_site_floors(tid)
        if p.get("restricted"):
            notice = db._clean_notice(p.get("notice") or "")
            # ★ 防丢数据：本地**已经有正文**的帖，不许标 restricted ——
            #   restricted 的既有语义是「正文不公开 → posts_n 清零 + 写提示正文行」，
            #   而这批帖的正文是我们**真采到过**的（站方后来才隐藏），
            #   清零 = 把已经采到的正文扔掉。
            #   → 保持原状、单独报出来，等人决定怎么处理。
            if got > 0:
                return (tid,
                        f"站方限制但本地已有 {got} 楼正文 → 不动（避免清零丢数据）",
                        None, "skip")
            return tid, f"站方限制 → restricted（{notice[:24]}）", notice, "restricted"
        if not p.get("ok"):
            return tid, f"实测失败（{p.get('reason')}）→ 不动", None, "failed"
        if p["count"] <= got:
            # ★ 正文齐全 = 已采（2026-10-29 用户拍板）
            detail = (f"站方列表称应有 {expect} 楼，"
                      f"站方详情页实测 {p['count']} 楼，"
                      f"本地实收 {got} 楼 —— "
                      f"差额来自站方列表页计数未随删楼而减；"
                      f"正文已按详情页实测核对，判定为已采全；"
                      f"{COUNT_VER_MARK}：已按详情页实测"
                      f"（实测 {p['count']} 楼，列表称 {expect} 楼）")
            return (tid,
                    f"正文齐全 → done（实测 {p['count']} vs 实收 {got}）",
                    detail, "done")
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
            expect = (r["replies"] or 0) + 1
            c = cols[tid % len(cols)]
            p = c._probe_site_floors(tid)
            if p.get("restricted"):
                notice = db._clean_notice(p.get("notice") or "")
                # ★ 防丢数据：本地已有正文的帖不标 restricted（见 probe_one 注释）
                if got > 0:
                    return (tid,
                            f"站方限制但本地已有 {got} 楼正文 → 不动（避免清零丢数据）",
                            None, "skip")
                return (tid, f"站方限制 → restricted（{notice[:24]}）",
                        notice, "restricted")
            if not p.get("ok"):
                return tid, f"实测失败（{p.get('reason')}）→ 不动", None, "failed"
            if p["count"] <= got:
                detail = (f"站方列表称应有 {expect} 楼，"
                          f"站方详情页实测 {p['count']} 楼，"
                          f"本地实收 {got} 楼 —— "
                          f"差额来自站方列表页计数未随删楼而减；"
                          f"正文已按详情页实测核对，判定为已采全；"
                          f"{COUNT_VER_MARK}：已按详情页实测"
                          f"（实测 {p['count']} 楼，列表称 {expect} 楼）")
                return (tid,
                        f"正文齐全 → done（实测 {p['count']} vs 实收 {got}）",
                        detail, "done")
            return (tid,
                    f"真漏采 → 保留待补采（实测 {p['count']} > 实收 {got}）",
                    None, "keep")

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for tid, verdict, detail, kind in pool.map(probe_one_w, rows):
                done += 1
                if kind == "done":
                    done_items.append((tid, detail))
                elif kind == "restricted":
                    restricted_items.append(tid)
                elif kind == "keep":
                    keep_stale.append(tid)
                elif kind == "skip":
                    skipped.append(tid)
                else:
                    failed.append(tid)
                if done % 50 == 0 or done == len(rows):
                    print(f"  [{done}/{len(rows)}] tid={tid} {verdict}", flush=True)
                if len(done_items) >= BATCH:
                    flush()
    else:
        for i, r in enumerate(rows, 1):
            tid, verdict, detail, kind = probe_one(r)
            if kind == "done":
                done_items.append((tid, detail))
            elif kind == "restricted":
                restricted_items.append(tid)
            elif kind == "keep":
                keep_stale.append(tid)
            elif kind == "skip":
                skipped.append(tid)
            else:
                failed.append(tid)
            if i % 20 == 0 or i == len(rows):
                print(f"  [{i}/{len(rows)}] tid={tid} {verdict}", flush=True)
            if len(done_items) >= BATCH:
                flush()
            if i < len(rows):
                time.sleep(args.delay)

    flush()

    print()
    print("=" * 60)
    print(f"正文齐全（→ done，正文齐全即已采）: {done_written}")
    print(f"站方限制（→ restricted）            : {len(restricted_items)}")
    print(f"站方限制但本地有正文（保持原状）    : {len(skipped)}")
    print(f"真漏采（保留待补采）                : {len(keep_stale)}")
    print(f"实测失败（不动）                    : {len(failed)}")
    print(f"耗时 {time.time() - t0:.1f}s")

    if not args.apply:
        print("\n★ 试跑结束，未改库。加 --apply 执行写库。")
        return 0

    if restricted_items:
        n = db.mark_restricted_bulk(restricted_items, "站方限制：历史帖子仅限作者本人查看")
        print(f"\n已写库：{n} 条 → restricted")
    # 真漏采与实测失败的**保持原状**（stale + 队列 pending），
    # 让正常的补采流程去处理 —— 本脚本不越权改它们的状态。
    db.conn.commit()
    print("真漏采与实测失败的帖保持原状，由正常补采流程处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
