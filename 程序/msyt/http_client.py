# -*- coding: utf-8 -*-
"""
HTTP 客户端：会话 / 限速 / 重试 / 「取 token 即用」
================================================================
限速纪律（本项目最容易写错的地方）：
  · **限速加在「对象之间」**（帖与帖之间、列表页与列表页之间），
    **不能加在「同一次逻辑步骤之间」** —— 也就是详情页取到 token 之后，
    紧接着的那次 ajax 请求**中间不得插入任何 sleep**。
    token 实测有效期 < 30 秒，一旦在这里睡过去，
    服务端返回 code=0「数据不存在」，而且**不告诉你真实原因**。
  · 所以本模块提供两个入口：
      - `get()`       —— 会限速（用于列表页、详情页这类「取新对象」）
      - `get_nowait()` —— **不**限速（用于「取完 token 立刻用」）
"""

from __future__ import annotations

import sys
import time
from typing import Optional

import requests

from .config import Config


class HttpError(Exception):
    """带 kind 的失败，便于落进 fetch_log 时归因。"""

    def __init__(self, kind: str, msg: str = ""):
        super().__init__(f"{kind}: {msg}" if msg else kind)
        self.kind = kind
        self.msg = msg


class Client:
    def __init__(self, cfg: Config, verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json,*/*",
        })
        self._last_list = 0.0
        self._last_obj = 0.0
        self.n_req = 0
        self.n_err = 0

    # ------------------------------------------------------------------
    def _throttle(self, kind: str) -> None:
        """kind: 'list' 列表页之间 / 'obj' 对象（帖）之间"""
        if kind == "list":
            gap, last = self.cfg.list_interval, self._last_list
        else:
            gap, last = self.cfg.interval, self._last_obj
        dt = time.time() - last
        if dt < gap:
            time.sleep(gap - dt)
        if kind == "list":
            self._last_list = time.time()
        else:
            self._last_obj = time.time()

    def _do(self, method: str, url: str, *, throttle: str | None,
            **kw) -> requests.Response:
        """throttle=None 表示**不限速**（token 即用场景）。"""
        last_exc = None
        for i in range(self.cfg.retry):
            if throttle:
                self._throttle(throttle)
            try:
                self.n_req += 1
                r = self.session.request(method, url, timeout=self.cfg.timeout, **kw)
                if r.status_code == 200:
                    r.encoding = "utf-8"
                    return r
                last_exc = HttpError(f"http_{r.status_code}", f"{url}")
            except requests.RequestException as e:
                last_exc = HttpError("network", f"{type(e).__name__}: {e}")
            self.n_err += 1
            time.sleep(self.cfg.interval * (i + 1))
        raise last_exc or HttpError("unknown", url)

    # ------------------------------------------------------------------
    def get(self, url: str, *, referer: str = "", params: dict | None = None,
            throttle: str = "obj") -> requests.Response:
        """取一个**新对象**（列表页 / 详情页）—— 会限速。"""
        headers = {"Referer": referer} if referer else None
        return self._do("GET", url, throttle=throttle, headers=headers,
                        params=params)

    def get_nowait(self, url: str, *, referer: str = "",
                   params: dict | None = None) -> requests.Response:
        """紧接着上一个请求发出（token 即用）—— **绝不**限速、绝不重试。

        重试在这里是错的：重试会用**同一个过期 token**，必然还是失败。
        """
        headers = {"Referer": referer} if referer else None
        self.n_req += 1
        try:
            r = self.session.get(url, headers=headers, params=params,
                                 timeout=self.cfg.timeout)
        except requests.RequestException as e:
            raise HttpError("network", f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise HttpError(f"http_{r.status_code}", url)
        r.encoding = "utf-8"
        return r

    def log(self, msg: str) -> None:
        """打一行进度日志。

        ★ 打印失败**绝不能影响采集**：
          Windows 控制台默认 GBK，采集日志里的「⚠」「≠」「✓」编不了 →
          print 抛 UnicodeEncodeError → 采集线程直接被打挂（实测踩过：
          漂移警告里的「⚠」把整个列表采集任务干掉了）。
          所以这里吞掉打印异常，并做一次 replace 兜底。
        """
        if not self.verbose:
            return
        try:
            print(msg, flush=True)
        except Exception:                             # noqa: BLE001
            try:
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
            except Exception:                         # noqa: BLE001
                pass
