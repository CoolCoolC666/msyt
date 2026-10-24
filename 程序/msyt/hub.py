# -*- coding: utf-8 -*-
"""
hub.py —— 实时日志总线（供 SSE 推送）
================================================================
Beta 里"双方并行显示采集日志"靠它：
  · 管理员节点（admin）与成员节点（member:xxx）各写各的日志
  · 日志带 **任务编号**（协作任务 code，如 A1），双方看的是同一条流
  · 订阅端可以按节点过滤，也可以看全部

用轮询而不是 asyncio 事件量，是因为这是个**本机单进程**工具，
0.4 秒的推送延迟完全够用，而轮询实现简单、不会踩线程/事件循环的坑。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional


class Hub:
    def __init__(self, maxlen: int = 4000):
        self._logs: deque = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 写
    def publish(self, node: str, msg: str, task: str = "",
                level: str = "info") -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            rec = {
                "seq": self._seq,
                "t": time.time(),
                "node": node or "admin",
                "task": task or "",
                "level": level,
                "m": str(msg),
            }
            self._logs.append(rec)
            return rec

    def info(self, node, msg, task=""):
        return self.publish(node, msg, task, "info")

    def ok(self, node, msg, task=""):
        return self.publish(node, msg, task, "ok")

    def warn(self, node, msg, task=""):
        return self.publish(node, msg, task, "warn")

    def err(self, node, msg, task=""):
        return self.publish(node, msg, task, "err")

    # ---------------------------------------------------------------- 读
    def since(self, seq: int = 0, limit: int = 300,
              node: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [r for r in self._logs if r["seq"] > seq]
        if node:
            items = [r for r in items if r["node"] == node]
        return items[-limit:]

    def latest(self, limit: int = 200, node: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._logs)
        if node:
            items = [r for r in items if r["node"] == node]
        return items[-limit:]

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    # ---------------------------------------------------------------- SSE
    async def stream(self, seq: int = 0, node: Optional[str] = None,
                     interval: float = 0.4, heartbeat: float = 15.0):
        """异步生成器，产出 SSE 帧。断线由客户端自动重连（带 Last-Event-ID）。"""
        last = seq
        idle = 0.0
        while True:
            items = self.since(last, limit=200, node=node)
            if items:
                idle = 0.0
                for r in items:
                    last = r["seq"]
                    yield (f"id: {r['seq']}\n"
                           f"data: {_json(r)}\n\n")
            else:
                idle += interval
                if idle >= heartbeat:
                    idle = 0.0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(interval)


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
