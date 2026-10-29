# -*- coding: utf-8 -*-
"""一次性补采：把「站方确实还有楼、我们真漏采」的帖补齐。

★ 判定口径（2026-10-29 实测确立）
  **内容是否采全，只看 pid 集合** —— 站方会跨页重复渲染同一批回复
  （实测 tid=8214148：第 3、4 页给的 pid 全是第 1、2 页那批），
  楼层号是站方渲染的产物、不可靠。

★ 为什么要真重跑而不是「只补缺的那几页」
  站方会**重复渲染**，页与页的对应关系不稳定；
  旧实现「本页没有新 pid 就停止翻页」（`if not new_pids and page_no > 1: break`）
  正是死循环根因 —— 已修（detail.py）。
  → 现在从第 1 页完整重跑，pid 已存在的走 upsert 不重复写，
    缺的自动补上，最后按 pid 集合判定。

用法：
    python _tools/fix_backfill_missing.py            # 试跑
    python _tools/fix_backfill_missing.py --apply    # 真采（会打站方）
"""
from __future__ import annotations

import argparse
import os
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

#: 待补采的 tid（由 fix_pending_by_pid.py 试跑判定为 missing）
TIDS = [8236013, 8294530, 8333106, 8333204]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真采（默认只报告）")
    ap.add_argument("--tids", type=int, nargs="*", default=None,
                    help="只处理这几条 tid（默认用 TIDS 全表）")
    args = ap.parse_args()

    db = DB(DBP)
    cfg = Config.load()
    client = Client(cfg)
    col = DetailCollector(db, client, cfg)

    tids = args.tids if args.tids else TIDS

    print(f"待补采 {len(tids)} 条（{'真采模式' if args.apply else '只报告模式'}）\n")
    for tid in tids:
        t0 = time.time()
        if not args.apply:
            n = db.conn.execute(
                "SELECT COUNT(*) FROM posts WHERE tid=?", (tid,)).fetchone()[0]
            print(f"tid={tid}：本地现有 {n} 篇（未采集）")
            continue
        print(f"--- tid={tid} 开始重采 ---", flush=True)
        # ★ 用 collect_one_full：采完做 pid 集合闭环校验，
        #   不全就从第 1 页再走一轮（站方重复渲染 → 循环可能提前收尾）
        res = col.collect_one_full(tid, max_recheck=2, pages_done=0,
                                   pages_total=0)
        n = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=?", (tid,)).fetchone()[0]
        print(f"    ok={res['ok']} pages={res['pages']} posts={res['posts']} "
              f"chars={res['chars']} 库内现有={n} 篇 "
              f"耗时 {time.time() - t0:.1f}s")
        if res.get("error"):
            print(f"    error={res['error']}")
        if res.get("warnings"):
            for w in res["warnings"]:
                print(f"    warn: {w}")
        print(flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
