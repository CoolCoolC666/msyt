# -*- coding: utf-8 -*-
"""一次性归位：把 27 条卡死的 pending 帖按 **pid 集合**判定归位。

★ 为什么用 pid 集合，不用楼层集合
  实测 tid=8214148：站方把同一批回复**跨页重复渲染**，
  第 3、4 页给的 pid 全是第 1、2 页那批（楼层号整体后移到 31-48），
  置顶回复 pid=113197168 更在 3 页里各出现一次（floor=0/22/41）。
  → 站方 pid 并集 29、楼层并集 47；本地 pid 29 个**一个不缺**。
  → 用楼层集合判会误报「漏了 18 楼」；用 pid 集合判才是对的。

四分类：
  done      —— 站方 pid 集合 ⊆ 本地 pid 集合（正文已采全）→ 归 done + 备注留痕
  missing   —— 站方有本地没有的 pid（真漏采）→ 保持 pending，走正常补采
  restricted—— 站方限制页 → 不动（本批实测 0 条）
  failed    —— 实测失败 → 不下结论，不动

用法：
    python _tools/fix_pending_by_pid.py            # 试跑
    python _tools/fix_pending_by_pid.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.detail import DetailCollector          # noqa: E402
from msyt.db import DB                            # noqa: E402
from msyt.config import Config                    # noqa: E402
from msyt.http_client import Client               # noqa: E402

DBP = os.path.join(PROJ, "程序", "data", "msyt.db")
MARK = "计数核对"
MARK_RE = ("计数核对：已按详情页实测"
           "（实测 {n} 篇，列表称 {r} 楼）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认试跑）")
    args = ap.parse_args()

    db = DB(DBP)
    cfg = Config.load()
    client = Client(cfg)
    col = DetailCollector(db, client, cfg)

    conn = sqlite3.connect(DBP)
    conn.row_factory = sqlite3.Row
    tids = [int(r["tid"]) for r in conn.execute(
        "SELECT tid FROM detail_queue WHERE state NOT IN ('done','restricted') "
        "ORDER BY tid")]
    print(f"待归位 {len(tids)} 条（{'写库模式' if args.apply else '试跑模式'}）\n")

    done_items: list[tuple[int, str]] = []
    missing_items: list[tuple[int, int, int]] = []      # tid, 站方pid数, 缺数
    restricted: list[int] = []
    failed: list[tuple[int, str]] = []
    t0 = time.time()

    for tid in tids:
        row = conn.execute(
            "SELECT replies, detail_warn FROM threads WHERE tid=?", (tid,)).fetchone()
        local_pids = {int(r[0]) for r in conn.execute(
            "SELECT DISTINCT pid FROM posts WHERE tid=?", (tid,))}
        expect = (int(row["replies"]) + 1) if row and row["replies"] else 0

        res = col._probe_site_floors(tid, max_pages=120)
        if res.get("restricted"):
            restricted.append(tid)
            print(f"tid={tid} → restricted（站方限制）：{res.get('notice','')[:40]}")
            continue
        if not res.get("ok"):
            failed.append((tid, res.get("reason", "")))
            print(f"tid={tid} → failed（实测失败，不下结论）：{res.get('reason')}")
            continue

        site_pids = res.get("pids") or set()
        miss = site_pids - local_pids
        if miss:
            missing_items.append((tid, len(site_pids), len(miss)))
            print(f"tid={tid} → missing：站方 {len(site_pids)} 篇，"
                  f"本地 {len(local_pids)} 篇，缺 {len(miss)} 篇")
            continue

        note = (f"站方列表称应有 {expect} 楼，站方详情页实测 {len(site_pids)} 篇，"
                f"本地实收 {len(local_pids)} 篇 —— "
                f"正文已按详情页实测 pid 集合核对，判定为已采全；"
                f"{MARK_RE.format(n=len(site_pids), r=expect)}")
        done_items.append((tid, note))
        print(f"tid={tid} → done：站方 pid {len(site_pids)} 篇全部已入库")

    print()
    print("=" * 60)
    print(f"归位 done : {len(done_items)}")
    print(f"真漏采    : {len(missing_items)}（需补采）")
    print(f"站方限制  : {len(restricted)}")
    print(f"实测失败  : {len(failed)}")
    print(f"耗时 {time.time() - t0:.1f}s")

    if not args.apply:
        print("\n★ 试跑结束，未改库。加 --apply 执行写库。")
        return 0

    if done_items:
        n = db.mark_detail_done_bulk(list(done_items))
        print(f"\n已写库 {n} 条 → done")
    for tid, site_n, miss_n in missing_items:
        db.set_detail_state(tid, "pending",
                            f"真漏采：站方实测 {site_n} 篇，缺 {miss_n} 篇，待补采")
    print(f"已回置 {len(missing_items)} 条 → pending（等正常采集补）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
