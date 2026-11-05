# -*- coding: utf-8 -*-
"""回填**楼层发帖时间**（2026-11-05 · 对应优化报告 P0-1）。

★ 为什么单独写这个工具，而不是整帖重采
  · 整帖重采 = 每页 2 次请求（详情页 + ajax），而且会重写正文
  · 这里只要**楼层时间**这一个字段 → **每页 1 次请求**，且**不碰正文**
  · 全库 190,116 页里 187,257 页属于单页帖 → 一帖一次请求就能补全

★ 原理
  站方详情页的楼层锚点里有 `<em id="authorposton{pid}">发表于 2025-5-23 19:55</em>`
  → 直接给出 **pid → 发帖时间** 的映射。
  按 **pid** 写回（pid 是内容的稳定标识，楼层号会变、pid 不会）。

★ 站方重复渲染 / 删楼
  · 重复渲染：同一 pid 在多页出现 → 取到的时间一样，写回结果一致，无害
  · 删楼：站方锚点里没有的 pid → 我们也不该有它的时间 → 不写，留空

★ 并发纪律（沿用既有约定）
  · `requests.Session` 不是线程安全的 → **每线程独立 Client**
  · sqlite 连接不能跨线程 → **DB 写入回主线程**（worker 只抓网络，结果进 queue）

★ 断点续跑
  已完成的 tid 记在 `_备份/post_time_backfill.done`。
  ★ **先提交、后记进度** —— 顺序反了的话，中途被 kill 时
    那些 tid 会被当成「已完成」永久跳过（2026-11-05 踩过：
    进度文件写了 300 个 tid，库里一条都没写）。

用法：
    python _tools/backfill_post_time.py                      # 试跑（只报不写）
    python _tools/backfill_post_time.py --apply              # 真写库
    python _tools/backfill_post_time.py --apply --limit 5000
    python _tools/backfill_post_time.py --apply --loop       # 一直跑到全库补完
"""
from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.config import Config, THREAD_URL          # noqa: E402
from msyt.http_client import Client                 # noqa: E402
from msyt.parsers import parse_thread_page         # noqa: E402

DBP = os.path.join(PROJ, "程序", "data", "msyt.db")
PROGRESS = os.path.join(PROJ, "_备份", "post_time_backfill.done")
PROGRESS_INTERVAL = 5.0          # 进度行节流（与采集链路一致：5 秒一行）


def load_done() -> set:
    if not os.path.exists(PROGRESS):
        return set()
    out = set()
    with open(PROGRESS, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.add(int(line))
    return out


def one_round(args, cfg) -> int:
    """跑一轮。返回 0 = 全库补完，1 = 还有剩余。"""
    conn = sqlite3.connect(DBP)
    conn.row_factory = sqlite3.Row
    done = load_done()
    if args.tids:
        tids = list(args.tids)
    else:
        tids = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT tid FROM posts WHERE pid>0 "
            "AND (post_time IS NULL OR post_time='') ORDER BY tid")]
        tids = [t for t in tids if t not in done]
    if args.limit:
        tids = tids[:args.limit]
    print(f"\n===== 本轮待处理 {len(tids)} 帖 =====", flush=True)
    if not tids:
        return 0

    # 每帖要翻哪些页（按已有的 page_no，不额外试探）
    pages_of: dict[int, list[int]] = {}
    for tid in tids:
        rows = conn.execute(
            "SELECT DISTINCT page_no FROM posts WHERE tid=? AND pid>0 "
            "AND (post_time IS NULL OR post_time='')", (tid,)).fetchall()
        pages_of[tid] = [int(r[0]) for r in rows]
    total_pages = sum(len(v) for v in pages_of.values())
    print(f"待翻页数 {total_pages}", flush=True)

    q: queue.Queue = queue.Queue(maxsize=200)
    res_q: queue.Queue = queue.Queue()
    stop = threading.Event()
    lock = threading.Lock()
    stats = {"pages": 0, "pids": 0, "err": 0}
    pending: dict[int, list] = {}          # tid -> [(pid, time, ts), ...]
    seen_pages: dict[int, set] = {}        # tid -> 已回来的页
    prog_buf: list[int] = []               # 待写入进度文件的 tid
    os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
    fh = open(PROGRESS, "a", encoding="utf-8")

    def write_tid(tid: int, items: list) -> None:
        """把一个帖的结果写库，**提交之后才**记进度。

        ★ 为什么**每帖一提交**（2026-11-05 改，原为每 100 帖一提交）
          Python 的 sqlite3 默认开隐式事务：不 commit 就一直占着**写锁**。
          每 100 帖才提交 → 写锁被占住几十秒 → 服务端任何写操作
          （词表扫描落库、导出、核验台账）都会等到 busy_timeout 超时
          → 实测「database is locked」**持续 30 秒也等不到**。
          每帖一提交：写锁只占几毫秒，别人的写随时能插进来；
          代价是 commit 次数变多（WAL 下每次几毫秒，总代价可忽略）。
        """
        if items:
            conn.executemany(
                "UPDATE posts SET post_time=?, post_ts=? "
                "WHERE pid=? AND (post_time IS NULL OR post_time='')",
                [(t, ts, pid) for pid, t, ts in items])
        conn.commit()                             # ★ 先提交
        prog_buf.append(tid)
        if len(prog_buf) >= 1:
            fh.write("".join("%d\n" % t for t in prog_buf))
            fh.flush()                            # ★ 再记进度
            prog_buf.clear()

    def worker() -> None:
        cli = Client(cfg, verbose=False)        # 每线程独立 client（Session 非线程安全）
        while not stop.is_set():
            try:
                # ★ 必须**阻塞等**，不能 get_nowait：
                #   队列空就 return 的话，worker 会在主线程入队前就退出
                #   （2026-11-05 踩过：一页没翻，主循环还在等）。
                tid, page_no = q.get(timeout=1.0)
            except queue.Empty:
                continue                          # 队列空 → 继续等（不是退出）
            try:
                url = THREAD_URL.format(tid=tid, page=page_no)
                html = cli.get(url, referer=url).text
                tp = parse_thread_page(html, tid=tid, page_no=page_no)
                items = [(pid, fm.post_time, fm.post_ts)
                         for pid, fm in tp.floors.items() if fm.post_time]
                with lock:
                    stats["pages"] += 1
                    stats["pids"] += len(items)
                # ★ 结果连同「哪一页」一起回主线程 —— 主线程要**等一个帖的
                #   所有页都回来了**才写库、才记进度。
                res_q.put((tid, page_no, items))
            except Exception as e:                          # noqa: BLE001
                with lock:
                    stats["err"] += 1
                if stats["err"] <= 5:
                    print(f"  [warn] tid={tid} page={page_no} "
                          f"{type(e).__name__}: {e}", flush=True)
                res_q.put((tid, page_no, None))     # 失败也算这一页「回来了」
            finally:
                q.task_done()

    # ---- 独立心跳：进度行每 5 秒一行（与采集链路一致）----
    t0 = time.time()
    n_done = 0

    def ticker() -> None:
        while not stop.is_set():
            time.sleep(PROGRESS_INTERVAL)
            el = time.time() - t0
            rate = stats["pages"] / max(el, .001)
            left = (total_pages - stats["pages"]) / max(rate, .001)
            print(f"[回填] 已翻 {stats['pages']}/{total_pages} 页｜"
                  f"补到 {stats['pids']} 条时间｜已写 {n_done} 帖｜"
                  f"用时 {el/60:.1f} 分｜预计还剩 {left/60:.0f} 分", flush=True)

    threads = [threading.Thread(target=worker, name=f"bf-{i+1}", daemon=True)
               for i in range(max(1, args.workers))]
    for th in threads:
        th.start()
    tick = threading.Thread(target=ticker, name="bf-ticker", daemon=True)
    tick.start()

    try:
        # ---- 流水线：持续入队（q 满了就等）+ 持续收结果 ----
        todo = list(tids)
        while todo or not q.empty():
            while todo and not q.full():
                tid = todo.pop(0)
                for p in pages_of[tid]:
                    q.put((tid, p))
            try:
                t2, p2, items = res_q.get(timeout=0.2)
            except queue.Empty:
                continue
            seen_pages.setdefault(t2, set()).add(p2)
            if items:
                pending.setdefault(t2, []).extend(items)
            need2 = len(pages_of.get(t2, ()))
            if need2 and len(seen_pages[t2]) >= need2:
                write_tid(t2, pending.pop(t2, []))
                n_done += 1
        q.join()
        # 收尾：把还在路上的结果写掉
        while True:
            try:
                t2, p2, items = res_q.get(timeout=5)
            except queue.Empty:
                break
            seen_pages.setdefault(t2, set()).add(p2)
            if items:
                pending.setdefault(t2, []).extend(items)
            need2 = len(pages_of.get(t2, ()))
            if need2 and len(seen_pages[t2]) >= need2:
                write_tid(t2, pending.pop(t2, []))
                n_done += 1
        conn.commit()                                # ★ 提交
        if prog_buf:                                 # ★ 再记进度
            fh.write("".join("%d\n" % t for t in prog_buf))
            fh.flush()
            prog_buf.clear()
    finally:
        stop.set()
        fh.close()

    el = time.time() - t0
    left = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE pid>0 AND (post_time IS NULL OR post_time='')"
    ).fetchone()[0]
    print(f"\n本轮完成：翻 {stats['pages']} 页，补 {stats['pids']} 条楼层时间，"
          f"错误 {stats['err']}，用时 {el/60:.1f} 分钟")
    print(f"全库仍无发帖时间的楼层：{left}")
    return 0 if left == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认试跑）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少帖（0=不限）")
    ap.add_argument("--workers", type=int, default=3, help="并发路数（站方最优点是 3）")
    ap.add_argument("--tids", type=int, nargs="*", default=None, help="只处理这几条")
    ap.add_argument("--loop", action="store_true",
                    help="一轮跑完自动接着跑下一轮（直到全库补完）")
    args = ap.parse_args()

    cfg = Config.load()
    done = load_done()
    print(f"已完成 {len(done)} 帖（断点续跑）｜"
          f"{'真写库' if args.apply else '只报告'}｜并发 {args.workers} 路")

    if not args.apply:
        conn = sqlite3.connect(DBP)
        n = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE pid>0 "
            "AND (post_time IS NULL OR post_time='')").fetchone()[0]
        tids = conn.execute(
            "SELECT COUNT(DISTINCT tid) FROM posts WHERE pid>0 "
            "AND (post_time IS NULL OR post_time='')").fetchone()[0]
        print(f"待补楼层 {n} 条，涉及 {tids} 帖")
        print("★ 试跑结束，未改库。加 --apply 执行写库。")
        return 0

    if args.loop:
        while True:
            if one_round(args, cfg) == 0:
                print("★ 全库已补完，退出。", flush=True)
                return 0
            time.sleep(2)
    return one_round(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
