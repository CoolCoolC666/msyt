# -*- coding: utf-8 -*-
"""pid 集合闭环核验：站方实测 pid 集合 vs 本地库 pid 集合。

★ 口径（2026-10-29 实测确立）
  内容是否采全**只看 pid 集合**。楼层号是站方渲染的产物：
  会跨页重复渲染、会整体后移、置顶帖会被渲染成 floor=0，
  所以「楼层号连续性」不能当判据。

用法：
    python _tools/verify_pid_closure.py 8236013 8294530
    python _tools/verify_pid_closure.py --all-done      # 抽查所有 done 的帖（慢）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.detail import DetailCollector, DETAIL_PAGE_HARD_CAP  # noqa: E402
from msyt.db import DB                                            # noqa: E402
from msyt.config import Config                                    # noqa: E402
from msyt.http_client import Client                               # noqa: E402

DBP = os.path.join(PROJ, "程序", "data", "msyt.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tids", nargs="*", type=int)
    ap.add_argument("--max-pages", type=int, default=DETAIL_PAGE_HARD_CAP)
    args = ap.parse_args()

    tids = args.tids
    if not tids:
        print("请给 tid，或用 --all-done")
        return 2

    db = DB(DBP)
    cfg = Config.load()
    client = Client(cfg)
    col = DetailCollector(db, client, cfg)

    print("站方实测 pid 集合 vs 本地库 pid 集合（max_pages=%d）\n" % args.max_pages)
    for tid in tids:
        t0 = time.time()
        probe = col._probe_site_floors(tid, max_pages=args.max_pages)
        local = set(int(p) for p in col._unique_pids(tid))
        site = set(int(p) for p in (probe.get("pids") or set()))
        missing = site - local
        extra = local - site
        st = db.conn.execute(
            "SELECT detail_state, posts_n, pages_total FROM threads WHERE tid=?",
            (tid,)).fetchone()
        print("tid=%d  本地状态=%s 本地篇数=%s 库标页数=%s"
              % (tid, st[0] if st else "?", st[1] if st else "?",
                 st[2] if st else "?"))
        print("  站方实测：ok=%s 页数=%s 篇数=%d 楼层集合=%d restricted=%s reason=%s"
              % (probe.get("ok"), probe.get("pages"), len(site),
                 len(probe.get("floors") or ()), probe.get("restricted"),
                 probe.get("reason")))
        print("  本地 pid：%d   站方 pid：%d" % (len(local), len(site)))
        if missing:
            print("  ★ 真漏采 %d 篇，缺 pid: %s"
                  % (len(missing), sorted(missing)[:20]))
        else:
            print("  ✔ 站方 pid 集合已全部覆盖")
        if extra:
            print("  （本地多出 %d 个 pid，可能是站方已删/转私密）" % len(extra))
        print("  耗时 %.1fs\n" % (time.time() - t0))

    return 0


if __name__ == "__main__":
    sys.exit(main())
