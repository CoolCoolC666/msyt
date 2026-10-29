# -*- coding: utf-8 -*-
"""一次性清理：按 posts 表**重建** thread_pages 的坏记录。

★ 为什么必须清
  全库体检（2026-10-29）：thread_pages 19.1 万行 status='ok'，其中
    ① 7.73 万行 pids 字段**写坏**（存成 "1,,,1,,,3,,,4,,,…" —— 明显串了别的数据）
    ② 27 行 记录说采过但 posts 里该页**一行都没有**
    ③ 560 行 pid 集合与 posts 对不上
  合计影响 **7.76 万个 tid**。

★ 清理方式：**从 posts 表现算重建**，不是删库
  · 内容（正文）在 posts 表里是完好的，坏的是 thread_pages 的**元数据**
  · 重建后 thread_pages 与 posts 一致 → 任何读它的代码都拿到真值
  · 已经改过的生产代码（detail/assist/webapp）现在都从 posts 表现算了，
    重建主要是**把历史脏数据抹平**，避免以后谁再读它踩坑

★ 实现要点（2026-10-29 改写）
  旧版逐行回查 posts（19.1 万次查询）→ 慢到跑不动。
  现在改成**单遍扫描**：一次性把 posts 的 (tid,page_no)→pid 集合读进内存，
  再与 thread_pages 逐行比对。内存量 ≈ 56 万条 (tid,page,pid)，可控。

用法：
    python _tools/rebuild_thread_pages.py            # 试跑
    python _tools/rebuild_thread_pages.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DBP = os.path.join(PROJ, "程序", "data", "msyt.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（默认试跑）")
    args = ap.parse_args()

    conn = sqlite3.connect(DBP)
    conn.row_factory = sqlite3.Row
    t0 = time.time()

    # ---- ① 单遍读出 posts 的真值：(tid,page_no) -> pid 集合 ----
    expect: dict[tuple[int, int], set[int]] = {}
    for tid, page_no, pid in conn.execute(
            "SELECT tid, page_no, pid FROM posts WHERE pid > 0"):
        expect.setdefault((int(tid), int(page_no or 0)), set()).add(int(pid))
    print(f"posts 真值 (tid,page) 组数 = {len(expect)}  耗时 {time.time() - t0:.1f}s")

    # ---- ② 与 thread_pages 逐行比对 ----
    rows = conn.execute(
        "SELECT tid, page_no, pids, n, status FROM thread_pages").fetchall()
    bad_field, bad_missing, bad_mismatch = [], [], []
    for r in rows:
        p = (r["pids"] or "")
        items = p.split(",") if p else []
        key = (int(r["tid"]), int(r["page_no"] or 0))
        real = expect.get(key)
        if (not p) or any(x.strip() == "" for x in items) or \
                any(not x.strip().isdigit() for x in items):
            bad_field.append(key)
            continue
        if not real:
            bad_missing.append(key)
            continue
        rec = {int(x) for x in items}
        if rec != real:
            bad_mismatch.append(key)

    bad_tids = sorted({t for t, _ in bad_field + bad_missing + bad_mismatch})
    print(f"thread_pages 总行数 = {len(rows)}")
    print(f"① pids 字段写坏  : {len(bad_field)}")
    print(f"② posts 里没这页 : {len(bad_missing)}")
    print(f"③ pid 集合对不上 : {len(bad_mismatch)}")
    print(f"受影响 tid 数     = {len(bad_tids)}")
    print(f"耗时 {time.time() - t0:.1f}s")

    if not args.apply:
        print("\n★ 试跑结束，未改库。加 --apply 执行写库。")
        return 0

    # ---- ③ 删掉坏行 ----
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _bad_tp "
                 "(tid INTEGER, page_no INTEGER, PRIMARY KEY(tid, page_no))")
    conn.execute("DELETE FROM _bad_tp")
    conn.executemany("INSERT OR IGNORE INTO _bad_tp VALUES (?,?)",
                     bad_field + bad_missing + bad_mismatch)
    n_del = conn.execute(
        "DELETE FROM thread_pages WHERE (tid, page_no) IN "
        "(SELECT tid, page_no FROM _bad_tp)").rowcount
    conn.execute("DROP TABLE IF EXISTS _bad_tp")
    print(f"\n已删除坏行 {n_del} 条")

    # ---- ④ 从 posts 表现算重建（pid 按升序拼接，与旧格式一致）----
    conn.execute("""
        INSERT OR REPLACE INTO thread_pages(tid,page_no,pids,n,status,fetched_at)
        SELECT tid, page_no,
               GROUP_CONCAT(pid, ','),
               COUNT(*),
               'ok',
               MAX(fetched_at)
        FROM (
            SELECT tid, page_no, pid, MAX(fetched_at) AS fetched_at
            FROM posts WHERE pid > 0
            GROUP BY tid, page_no, pid
            ORDER BY tid, page_no, pid
        )
        GROUP BY tid, page_no
    """)
    conn.commit()
    n_all = conn.execute(
        "SELECT COUNT(*) FROM thread_pages WHERE status='ok'").fetchone()[0]
    print(f"重建后 status='ok' 行数 = {n_all}")

    # ---- ⑤ 复核：再比一次，必须 0 坏行 ----
    expect2: dict[tuple[int, int], set[int]] = {}
    for tid, page_no, pid in conn.execute(
            "SELECT tid, page_no, pid FROM posts WHERE pid > 0"):
        expect2.setdefault((int(tid), int(page_no or 0)), set()).add(int(pid))
    bad2_field = bad2_missing = bad2_mismatch = 0
    for r in conn.execute("SELECT tid, page_no, pids FROM thread_pages"):
        p = (r["pids"] or "")
        items = p.split(",") if p else []
        key = (int(r["tid"]), int(r["page_no"] or 0))
        real = expect2.get(key)
        if (not p) or any(x.strip() == "" for x in items) or \
                any(not x.strip().isdigit() for x in items):
            bad2_field += 1
            continue
        if not real:
            bad2_missing += 1
            continue
        if {int(x) for x in items} != real:
            bad2_mismatch += 1
    print("复核：写坏 %d / 无对应 posts %d / 集合不符 %d"
          % (bad2_field, bad2_missing, bad2_mismatch))
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
