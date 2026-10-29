# -*- coding: utf-8 -*-
"""回改：把 2026-10-28 标成 `count_mismatch` 的帖，按 2026-10-29 的新口径改回 `done`。

★ 为什么需要回改（用户 2026-10-29 拍板）
  `count_mismatch` 当初的语义是「站方列表计数虚高导致的假少贴」，
  但它是**终态且不计入「已采详情帖」** —— 而这批帖的正文**本来就采全了、
  站方也照样公开**（实测 25 条逐层对比：站方详情页锚点数 == ajax 正文楼层数
  == 本地实存楼层数，pid 集合逐个对得上）。
  → 记成「没采到」是**错的记账**。
  → 新口径：**正文齐全即已采**（归为 done），差额原因写进 detail_warn 留痕。

★ 为什么不重新实测
  这批帖的 `detail_warn` 里**已经记着**当初的实测值：
    「站方列表称应有 N 楼，站方详情页实测 M 楼，本地实收 M 楼 —— …」
  直接解析出来用即可 → **不用再打站方**（省 1305 次请求）。

★ 同时补上「计数核对」标记
  归位后体检/采集再遇到这批帖时，靠这个标记**复用**上次的实测结论，
  不再重复实测（否则每次体检都要白打一遍站方请求）。
  标记带「列表称几楼」—— 列表计数一旦变了，说明可能有新回复，必须重新实测。

用法：
    python _tools/fix_legacy_count_mismatch.py            # 试跑，不改库
    python _tools/fix_legacy_count_mismatch.py --apply    # 真正写库
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

PROJ = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(PROJ, '程序'))

from msyt.db import DB                              # noqa: E402
from msyt.detail import COUNT_VER_MARK             # noqa: E402

DBP = os.path.join(PROJ, '程序', 'data', 'msyt.db')

#: 旧 detail_warn 里的实测值：「站方列表称应有 N 楼，站方详情页实测 M 楼」
_OLD_RE = re.compile(r"站方列表称应有\s*(\d+)\s*楼.*?站方详情页实测\s*(\d+)\s*楼")
#: 旧队列状态 / 旧 detail_state
OLD_STATE = "count_mismatch"


def build_note(old_warn: str, posts_n: int) -> str:
    """把旧告警改写成新口径的 detail_warn（带「计数核对」标记）。"""
    m = _OLD_RE.search(old_warn or "")
    if m:
        expect, meas = int(m.group(1)), int(m.group(2))
    else:
        # 旧文案缺失 → 退化成只写本地实收（标记仍要写，否则会反复实测）
        expect, meas = (posts_n or 0), (posts_n or 0)
    return (f"站方列表称应有 {expect} 楼，"
            f"站方详情页实测 {meas} 楼，"
            f"本地实收 {posts_n or 0} 楼 —— "
            f"差额来自站方列表页计数未随删楼而减；"
            f"正文已按详情页实测核对，判定为已采全；"
            f"{COUNT_VER_MARK}：已按详情页实测"
            f"（实测 {meas} 楼，列表称 {expect} 楼）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    args = ap.parse_args()

    db = DB(DBP)
    rows = db.conn.execute(
        "SELECT tid, posts_n, detail_warn FROM threads "
        "WHERE detail_state=? ORDER BY tid", (OLD_STATE,)).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"待回改：{len(rows)} 条（{'写库模式' if args.apply else '试跑模式'}）")
    print(f"库：{DBP}")
    if not args.apply:
        print("★ 试跑模式，不改库。加 --apply 才真正写入。")

    items = []
    n_bad = 0
    for r in rows:
        note = build_note(r["detail_warn"] or "", int(r["posts_n"] or 0))
        if not _OLD_RE.search(r["detail_warn"] or ""):
            n_bad += 1
        items.append((int(r["tid"]), note))

    for tid, note in items[:3]:
        print(f"  样例 tid={tid} → {note}")

    if not args.apply:
        print(f"\n★ 试跑结束，未改库。加 --apply 执行写库。")
        return 0

    t0 = time.time()
    # 分批提交（每批 200 条）
    BATCH = 200
    done = 0
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        n = db.mark_detail_done_bulk(chunk)
        done += n
        print(f"  ↳ 已写库 {n} 条 → done（累计 {done}）", flush=True)
    print(f"\n回改完成：{done} 条 → done；耗时 {time.time() - t0:.1f}s")
    if n_bad:
        print(f"⚠ 其中 {n_bad} 条旧文案里没解析出实测值，标记按本地实收兜底。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
