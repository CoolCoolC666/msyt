# -*- coding: utf-8 -*-
"""
并发执行：把一批**独立任务**分给 N 个线程。

★ 为什么并发单位是「一个帖」而不是「一个请求」
  站方的 token 有效期 **< 30 秒**（实测：30/60/120/300 秒全部失效），
  详情页取到 token 后，**紧接着的那次 ajax 请求中间不得插入任何 sleep**。
  → 同一帖的两个请求必须**贴在一起**；
  → 能并发的最小单位是「一帖」（详情页 → ajax → 下一页 …），
    把两个请求拆到不同线程会必然踩 token 过期。

★ 为什么默认仍是 1（串行）
  串行是**已验证**的行为。并发是「先探测、再放开」的：
  并发度超过站方承受力会得到 403/429/超时，反而更慢。
  所以：默认 1（行为与改造前逐字节一致，出问题立刻退回），
  由配置/命令行按实测结论显式打开。

★ 线程安全的前提（调用方必须保证）
  · 每个线程**自己的** HTTP 客户端（requests.Session 不是线程安全的，
    而且限速状态是实例级的 → 每线程独立才能各自限速）
  · 每个线程**自己的**数据库连接
    （本项目 db.DB 的写锁是**进程级共享**的，
      所以「多连接 + 共享写锁」是安全的，见 db.DB 类注释）
  · 结果汇总要**线程安全**（本模块用一把锁汇总）
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple


def run_parallel(
    items: Sequence[Any],
    fn: Callable[[Any], Any],
    workers: int = 1,
    should_stop: Optional[Callable[[], bool]] = None,
    on_result: Optional[Callable[[Any, Any], None]] = None,
) -> Tuple[List[Any], List[Tuple[Any, BaseException]]]:
    """并发跑 `fn` 处理 `items`。

    返回 `(结果列表, 错误列表)`。
    结果列表**保持 items 的顺序**（与输入一一对应），
    错误列表是 `(item, 异常)` 对 —— **一个任务失败不影响其它任务**。

    ★ `workers <= 1` 时**直接串行**，不走线程池：
      这样默认路径与改造前完全一致（连异常传播方式都一样）。
    """
    n = max(1, int(workers or 1))
    if n == 1:
        out: List[Any] = []
        errs: List[Tuple[Any, BaseException]] = []
        for it in items:
            if should_stop is not None and should_stop():
                break
            try:
                r = fn(it)
                out.append(r)
                if on_result is not None:
                    on_result(it, r)
            except BaseException as e:                   # noqa: BLE001
                errs.append((it, e))
        return out, errs

    results: List[Any] = [None] * len(items)
    errors: List[Tuple[Any, BaseException]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def _one(idx_item):
        idx, item = idx_item
        if stop.is_set() or (should_stop is not None and should_stop()):
            return
        try:
            r = fn(item)
        except BaseException as e:                       # noqa: BLE001
            with lock:
                errors.append((item, e))
            return
        with lock:
            results[idx] = r
            if on_result is not None:
                try:
                    on_result(item, r)
                except Exception:                        # noqa: BLE001
                    pass

    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="msyt-par") as ex:
        futures = [ex.submit(_one, (i, it)) for i, it in enumerate(items)]
        for fu in futures:
            fu.result()
    return [r for r in results if r is not None], errors


def split_evenly(items: Sequence[Any], parts: int) -> List[List[Any]]:
    """把 items 尽量均分成 parts 份（保持原顺序）。"""
    n = max(1, int(parts or 1))
    if n == 1:
        return [list(items)]
    buckets: List[List[Any]] = [[] for _ in range(n)]
    for i, it in enumerate(items):
        buckets[i % n].append(it)
    return [b for b in buckets if b]
