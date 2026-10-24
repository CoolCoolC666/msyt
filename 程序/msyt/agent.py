# -*- coding: utf-8 -*-
"""
agent.py —— 成员端采集代理（Beta）
================================================================
小组成员在自己机器上跑的一个小程序：

    python -m msyt agent --server https://msyt.example.com --email you@example.com

它做四件事：
  ① 用邮箱登录控制面 → 拿到 JWT 与**自己的节点号**
  ② 轮询领活（领到的是**具体 tid**，按先旧后新）
  ③ 本地采集该帖详情
  ④ 回传结果，然后**立刻把内存里的东西清掉**

★ 数据落在哪（这是 Beta 的关键承诺）：
  本 agent **用内存 SQLite（`:memory:`）** 采集 ——
  **整条链路不写任何磁盘文件**：不落帖、不落正文、不落临时目录。
  回传成功后，内存库与正文对象立即释放（`del` + 重建库）。
  所以"传完即删"不是口号，是**物理上没有可删的东西**。

  另一个好处：走成员自己的 IP，站方看到的是**多个分散的普通用户**，
  而不是一台机器集中爬 —— 这也是控制面代理模式做不到的。

★ 与服务器失联 → **自动停止**（2026-10-22 新增）
  控制面是协作的**唯一出口**：领活要问它、回传要给它。
  一旦断了联系，agent 再继续采下去，采到的东西也送不回来 ——
  那是白采，也是白打站方。所以：

    · 后台**心跳线程**每 N 秒 ping 一次 `/api/agent/heartbeat`
    · 连接错误 / 超时连续达到阈值（默认 3 次）→ 判定「失联」
    · 失联后立刻：
        - 不再领活（避免占着活）
        - 正在采的那一帖**在下一个页的边界中止**（collect_one 支持 should_stop）
        - 退出主循环，并把内存里的正文清掉
    · 恢复连接也要**重新登录**（token 可能已过期）
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ★ 控制台编码兜底（Windows 默认 GBK，编不了「⚠」这类字符）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                 # noqa: BLE001
        pass

from .config import Config
from .db import DB
from .detail import DetailCollector
from .http_client import Client

# ★ 成员端**刻意不 import .auth**：
#   auth 依赖 PyJWT，而成员机器上只需要 requests + beautifulsoup4 两个包。
#   少一个依赖，成员机器上要装的包就少一个 —— 少一个坑。


class AgentError(Exception):
    pass


class LinkLost(AgentError):
    """与控制面的联系断了 —— agent 必须停下来。"""


#: agent 自身版本。★ 启动时打印出来 —— 成员拿的是**下载来的包**，
#: 万一包是旧的（比如今天服务端加了新功能），一眼就能从版本号看出来。
AGENT_VERSION = "1.2.0"


def _code_build() -> str:
    """**构建号** = 本文件的 mtime（改代码自动变，**永不骗人**）。

    ★ 为什么加它（2026-10-23 晚上发现）：
      `AGENT_VERSION` 从上线到现在一直是 "1.1.0"，一次都没改过 ——
      可这几天代码改了十几轮。组员报问题说「我这是 1.1.0」，
      我这边完全听不出他手上是新包还是旧包。
      构建号跟着源码走 → 组员对比一眼就知道要不要更新。
    """
    try:
        return str(int(os.path.getmtime(os.path.abspath(__file__))))
    except OSError:                                 # noqa: BLE001
        return "0"


#: 构建号（与 AGENT_VERSION 一起打印：msyt agent 1.2.0 (build 1792748xxx)）
AGENT_BUILD = _code_build()

# ======================================================================
# 网络参数（2026-10-23 按组员实测反馈调整）
# ----------------------------------------------------------------------
# 背景：组员在公网上跑 agent，遇到一次瞬时抖动（ReadTimeout），
#       agent 107 秒就判定「失联」并**自动停止采集** —— 活停在半路。
#       根因有三条，全是「太急」：
#         ① 单次请求 timeout 40 秒 —— 网络不通时每个请求白等 40 秒
#            （连「取身份」「退活」这种辅助请求也要等满）
#         ② 失败后 1~5 秒就重试 —— 网络还没恢复，连撞三次
#         ③ 连续 3 次失败就判死 —— 3×(40s+5s) ≈ 2 分钟的容忍太短
#
# 现在的取值：
#: 单次请求超时（秒）。
#:   实测正常延迟 1.4~2.3 秒（agentapi 公网），20 秒有近 10 倍余量；
#:   40 秒太长 —— 网络不通时纯白等，用户在窗口里干看。
HTTP_TIMEOUT = 45
#: 辅助请求（取身份 / 退活）的超时（秒）。
#:   这类请求失败也不影响主流程，不值得等那么久。
HTTP_TIMEOUT_AUX = 15
#: 失联重试的**间隔**（秒）。用户 2026-10-23 明确：
#:   「以网络连接，个人觉得可以 30s 以上一次，不然可能容易连接超时」。
#:   → 失败后至少等 30 秒再试，给网络（和 Cloudflare 隧道）恢复时间。
RETRY_BACKOFF = (30, 45, 60, 60, 60)
#: 连续失败多少次判定「失联」（配合上面的间隔 ≈ 3.5 分钟容忍）。
FAIL_LIMIT = 6
#: 心跳连续失败多少次单独判失联（心跳 12 秒一次 → 约 72 秒）。
#:   ★ 心跳与主请求**分开计数**（2026-10-23 修）：
#:     原来两者共用一个计数，主请求和心跳同时失败会让计数涨得飞快，
#:     「3 次」其实只有 1.5 轮 —— 误判失联。
HB_FAIL_LIMIT = 8
#: 回传一帖最多重试几次（2026-10-23 加）。
#:   用户：「回传后失联就断开，不重试不大合理」。
#:   回传走「合并覆盖」语义 → **幂等** → 重试安全。
SUBMIT_RETRY = 3
#: 连续多少帖回传都失败 → 才判定控制面真的不可用，停止采集。
#:   （单帖失败只放弃那一帖，**继续采下一帖**。）
SUBMIT_FAIL_LIMIT = 4
#: 选址探测的超时（秒）。走公网时要多给一点时间。
PROBE_TIMEOUT = 20


class Agent:
    def __init__(self, server: str, email: str = "", password: str = "",
                 node: str = "", limit: int = 0, interval: float = 4.0,
                 task: str = "", verbose: bool = True,
                 heartbeat: float = 12.0, fail_limit: int = 0,
                 token: str = "", ticket: str = "",
                 server_alt: str = "", proxy: str = ""):
        self.server = server.rstrip("/")
        #: ★ 备用控制面地址（2026-10-23 加）：
        #:   启动包由「**本地浏览器 + 服务器**」协同生成（见 bundle.py），
        #:   里面带**两个**候选地址：主用 + 备用。
        #:   启动时先探主地址，探不通就换备用 —— 包不必重新下载。
        self.server_alt = (server_alt or "").rstrip("/")
        #: ★ 手动指定的代理（""= 自动读系统代理，None=已试过且没有）
        self.proxy = proxy.strip() if proxy else ""
        self._proxy: Optional[str] = None if proxy else None
        self.email = email.strip().lower()
        self.password = password
        self.node = node or ""
        self.limit = limit
        self.interval = interval
        self.task = task
        self.verbose = verbose
        #: 心跳间隔（秒）。<=0 关闭心跳。
        self.heartbeat = heartbeat
        #: 连续失败多少次判定「失联」（0 = 用模块级默认值 FAIL_LIMIT）
        self.fail_limit = max(1, int(fail_limit or FAIL_LIMIT))
        #: 直接用已有令牌工作（**不给密码也能跑**）
        #: 网页端「一键启动本机 agent」走的就是这条路 ——
        #: 网页把当前登录的 JWT 交给本机助手，助手再交给 agent，
        #: 于是**密码从头到尾不落到成员机器的磁盘上**。
        self.token = token or ""
        #: 一次性启动票据（下载来的启动包里带的）→ 运行时换成 JWT。
        #: 这样**密码不必落到成员机器的磁盘上**，票还是限时限次的。
        self.ticket = ticket or ""
        self.member = None
        self._session = None

        # ---- 失联检测状态 ----
        self.link_lost = False
        self._fail_streak = 0
        #: ★ 心跳的失败计数**单独记**（2026-10-23 改）：
        #:   原来心跳和主请求共用一个计数 —— 两者同时失败时计数翻倍，
        #:   「连续 3 次」其实只有 1.5 轮，很容易误判失联。
        self._hb_fail_streak = 0
        #: 退避到第几次（决定等多久）
        self._backoff_i = 0
        self._last_ok = 0.0
        self._lock = threading.Lock()
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()
        #: ★ 当前状态（心跳带上，界面据此区分「在采 / 在等活 / 卡住了」）
        #:   collecting 正在采集 · idle 池子里没活可领 · starting 刚启动
        self.state = "starting"
        #: 最近一次「领不到活」的说明（给界面看）
        self.state_note = ""

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str) -> None:
        """打一行日志。

        ★ 打印**绝不能把 agent 打死**：
          Windows 控制台默认 GBK，「⚠」「✗」这些字符编不了 → print 抛
          UnicodeEncodeError，把正在跑的采集带崩。
          所以先换成 UTF-8 输出，兜底再用 replace 编一遍。
        """
        if not self.verbose:
            return
        try:
            print(f"[agent] {msg}", flush=True)
        except Exception:                             # noqa: BLE001
            try:
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                safe = msg.encode(enc, "replace").decode(enc, "replace")
                print(f"[agent] {safe}", flush=True)
            except Exception:                         # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 选址
    def _sys_proxy(self) -> str:
        """读 **Windows 系统代理**（Edge 用的就是这套）。

        ★ 为什么必须有（2026-10-23 晚上实测）：
          组员的现象是「Edge 能打开网站，Python 却 ConnectionError」。
          同一域名、同一台机器 —— 差别在：
            · Edge 读 Windows 注册表 `Internet Settings\\ProxyServer`
            · **requests 不读注册表**（只有 urllib.request 读）→ 直连 → 不通
          `urllib.request.getproxies()` 在 Windows 上**正好会读注册表** → 用它。

        返回代理 URL（如 `http://127.0.0.1:7890`），没有就返回空串。
        """
        if self._proxy is not None:
            return self._proxy
        try:
            import urllib.request
            proxies = urllib.request.getproxies()
        except Exception:                             # noqa: BLE001
            proxies = {}
        cand = ""
        for k in ("https", "http", "all"):
            if proxies.get(k):
                cand = str(proxies[k])
                break
        self._proxy = cand
        return cand

    def _mk_session(self, proxy: str = ""):
        """建一个 requests.Session（可指定代理）。"""
        import requests
        sess = requests.Session()
        sess.headers.update({"User-Agent": "msyt-agent/1.1"})
        if proxy:
            sess.proxies.update({"http": proxy, "https": proxy})
        return sess

    def _probe(self, base: str, proxy: str = "") -> bool:
        """探一次控制面；通就返回 True（401/403 也算通）。"""
        sess = self._mk_session(proxy)
        try:
            sess.get(base + "/api/agent/me", timeout=PROBE_TIMEOUT,
                     headers={"User-Agent": "msyt-agent/1.1"})
            return True
        except Exception:                             # noqa: BLE001
            return False

    def diagnose(self) -> List[str]:
        """**连通性诊断** —— 逐项测，告诉组员「到底哪一环不通」。

        ★ 为什么需要：
          组员只看到「ConnectionError」，完全不知道是 DNS、TCP、TLS、
          还是代理、还是服务端的问题 —— 只能一遍遍试。
          这里把每一环单独测一遍，给出**可执行的结论**。
        """
        import socket
        import urllib.parse
        out = []
        base = self.server
        u = urllib.parse.urlparse(base)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        out.append(f"控制面：{base}")

        # ① DNS
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            ips = sorted({i[4][0] for i in infos})
            out.append(f"  ① DNS 解析：{len(ips)} 个地址 → {', '.join(ips[:4])}")
        except Exception as e:                         # noqa: BLE001
            out.append(f"  ① DNS 解析：**失败**（{type(e).__name__}）"
                       f" —— 你的网络连这个域名都解析不了")
            return out

        # ② TCP 能不能连上（IPv4 / IPv6 分开测）
        for fam, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
            try:
                s = socket.socket(fam, socket.SOCK_STREAM)
                s.settimeout(6)
                s.connect((host, port))
                s.close()
                out.append(f"  ② TCP {label}：✓ 连得上")
            except Exception as e:                     # noqa: BLE001
                out.append(f"  ② TCP {label}：✗ {type(e).__name__}")

        # ③ 直连 HTTP
        if self._probe(base):
            out.append(f"  ③ 直连 HTTP：✓ 通")
        else:
            out.append(f"  ③ 直连 HTTP：✗ 不通")

        # ④ 经系统代理
        proxy = self._sys_proxy()
        if proxy:
            out.append(f"  ④ 系统代理：{proxy}")
            out.append(f"     经代理 HTTP：{'✓ 通' if self._probe(base, proxy) else '✗ 不通'}")
        else:
            out.append("  ④ 系统代理：未检测到"
                       "（若 Edge 能打开而这里不通，请手动指定代理："
                       "启动采集.cmd 里加 --proxy http://127.0.0.1:端口）")

        # ⑤ 本机助手
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=4) as r:
                out.append(f"  ⑤ 本机助手（127.0.0.1:8766）：✓ 在跑")
        except Exception:                             # noqa: BLE001
            out.append("  ⑤ 本机助手（127.0.0.1:8766）：✗ 没在跑"
                       "（双击「启动助手.cmd」）")
        return out

    def pick_server(self) -> str:
        """**探测**一下控制面地址，主地址不通就换备用的。

        ★ 为什么需要（2026-10-23）：
          启动包是「**本地浏览器 + 服务器**」协同生成的，
          里面带两个候选地址：
            · 主地址 = 按「下载者此刻访问的地址」定的
              （本地下载 → 本机；公网下载 → agentapi 子域名）
            · 备用地址 = 另一个
          实际情况会变：本地服务没开、隧道断了、域名换了……
          所以 agent 启动时**自己探一下**，别让组员为了换地址重下包。

        ★ 判据：GET /api/agent/me 能返回（哪怕 401 也算「通」）
          —— 说明网络到得了、路径存在。
          连不上/超时 = 不通。

        ★ 不改失联计数：选址阶段的失败**不该**算「运行中失联」。
        """
        cands = [s for s in (self.server, self.server_alt) if s]
        if len(cands) <= 1:
            return self.server
        import requests
        self.log(f"探测控制面地址（共 {len(cands)} 个候选）…")
        for i, base in enumerate(cands):
            try:
                r = requests.get(base + "/api/agent/me", timeout=PROBE_TIMEOUT,
                                 headers={"User-Agent": "msyt-agent/1.1"})
                # 401/403 也算「通」—— 那是「没带令牌」，不是「到不了」
                self.log(f"  ✓ {base} 可达（HTTP {r.status_code}）")
                if i > 0:
                    self.log(f"  → 主地址不通，改用备用：{base}")
                    self.server = base
                return self.server
            except Exception as e:                     # noqa: BLE001
                self.log(f"  ✗ {base} 不通（{type(e).__name__}）")
        # ★ 直连全不通 → 试**系统代理**（2026-10-23 晚上实测踩到）
        #   现象：同一台机器，Edge 能打开网站，Python 却 ConnectionError。
        #   原因：Edge 读 Windows 注册表的系统代理，requests 不读 → 直连不通。
        proxy = self._sys_proxy()
        if proxy:
            self.log(f"  ⚠ 直连都不通，试试系统代理：{proxy}")
            for i, base in enumerate(cands):
                if self._probe(base, proxy):
                    self.log(f"  ✓ 经代理可达：{base}")
                    if i > 0:
                        self.log(f"  → 改用备用地址：{base}")
                    self.server = base
                    self.proxy = proxy
                    self._session = self._mk_session(proxy)
                    return self.server
                self.log(f"  ✗ 经代理也不通：{base}")
        else:
            self.log("  ⚠ 没检测到系统代理（Edge 可能用了别的通道，"
                     "或你的网络需要手动指定代理）")

        # 都探不通：还是用主地址（后面有失联退避重试兜底）
        self.log(f"  ⚠ 所有候选地址都探不通，先用主地址 {self.server}"
                 f"（稍后会按退避重试）")
        return self.server

    # ---------------------------------------------------------------- HTTP
    def _req(self, method: str, path: str, body: Optional[dict] = None,
             auth: bool = True, count_fail: bool = True,
             timeout: Optional[float] = None) -> Any:
        """发一个请求。

        ★ 失败分两类，不能混：
          · **连接类**（连不上 / 超时 / DNS 失败）→ 算「可能失联」，累加失败计数
          · **HTTP 类**（4xx/5xx）→ 服务端在，只是拒绝了；5xx 算暂时性，4xx 不算
        连接类失败连续到阈值 → 抛 LinkLost，主循环据此停止。

        ★ timeout（2026-10-23 改）：
          原来硬编码 40 秒 —— 网络不通时每个请求白等 40 秒
          （连「取身份」「退活」这种辅助请求也要等满）。
          实测公网正常延迟 1.4~2.3 秒，20 秒够用；
          辅助请求用 10 秒，不值得为它占住 40 秒。
        """
        import requests
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": "msyt-agent/1.1"})
        url = self.server + path
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        to = timeout if timeout is not None else HTTP_TIMEOUT
        try:
            r = self._session.request(method, url, headers=headers,
                                      data=json.dumps(body) if body is not None else None,
                                      timeout=to)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            # ★ count_fail=False：这类请求（例如退出前退活）**不该**计入失联计数
            if count_fail:
                self._mark_fail(f"连接失败：{type(e).__name__}: {e}")
            raise LinkLost(f"与 {self.server} 的连接失败：{e}") from e
        except requests.exceptions.RequestException as e:
            if count_fail:
                self._mark_fail(f"请求异常：{type(e).__name__}: {e}")
            raise AgentError(f"请求异常：{e}") from e

        if r.status_code >= 400:
            # ★ 5xx = **服务端暂时不可用**（控制面重启、隧道闪断、网关抖动）
            #   → 当成「可能失联」处理：计入失败计数，抛 LinkLost，
            #     让主循环按「等待重试 → 达阈值自动停止」的路径走。
            #   旧实现把它当致命错误直接抛出去 → agent 一个 502 就死了，
            #   明明只是服务端重启了几秒。
            if r.status_code >= 500:
                if count_fail:
                    self._mark_fail(f"HTTP {r.status_code}")
                raise LinkLost(f"服务端暂时不可用（HTTP {r.status_code}）")
            # 4xx = 明确拒绝（401 登录过期等），这是致命的
            if r.status_code == 401:
                raise AgentError("登录已过期（401），请重新登录")
            raise AgentError(f"HTTP {r.status_code}: {r.text[:200]}")
        if count_fail:
            self._mark_ok()
        try:
            return r.json()
        except Exception as e:
            # ★★ 响应**不是 JSON** → 必须报错，不能当成成功。
            #   实测踩过：走公网时请求被 Cloudflare Access 挡下，
            #   返回的是 Access 登录页的 HTML（几万字节）。
            #   旧实现返回 {"ok": True, "text": <html>}，
            #   于是 agent 以为「登录成功」，但 token 是空的、角色是 None，
            #   而且**领活也永远领不到东西** —— 看起来像"在跑"，实际什么都没干。
            #   这里明确报错，把「网关拦截」这件事直接说出来。
            head = (r.text or "")[:120].replace("\n", " ")
            raise AgentError(
                f"服务端返回的不是 JSON（可能被网关/登录页拦截）：{head}")

    # ---------------------------------------------------------------- 失联
    def _mark_ok(self) -> None:
        with self._lock:
            self._fail_streak = 0
            self._hb_fail_streak = 0
            self._backoff_i = 0
            self._last_ok = time.time()

    def _mark_fail(self, why: str) -> None:
        with self._lock:
            self._fail_streak += 1
            n = self._fail_streak
            lost = self._fail_streak >= self.fail_limit
        self.log(f"⚠ {why}（连续第 {n} 次）")
        if lost and not self.link_lost:
            self.link_lost = True
            self.log(f"✗ 连续 {n} 次联系不上控制面 —— 判定为失联，自动停止采集。")

    def _mark_hb_fail(self, why: str) -> None:
        """心跳失败**单独计数**（不跟主请求混算）。

        ★ 为什么要分开（2026-10-23 修）：
          原来心跳和主请求共用 `_fail_streak`。
          agent 采一帖要几十秒，这期间主请求不发出、只有心跳在跑；
          两者一旦同时失败，计数就翻倍 ——
          「连续 3 次」其实只有 1.5 轮，很容易误判失联。
          现在：主请求按 `fail_limit` 判，心跳按 `HB_FAIL_LIMIT` 判，
          任何一方成功都会把另一方清零。
        """
        with self._lock:
            self._hb_fail_streak += 1
            n = self._hb_fail_streak
            lost = self._hb_fail_streak >= HB_FAIL_LIMIT
        self.log(f"⚠ 心跳失败：{why}（心跳连续第 {n} 次）")
        if lost and not self.link_lost:
            self.link_lost = True
            self.log(f"✗ 心跳连续 {n} 次失败 —— 判定为失联，自动停止采集。")

    def _backoff_wait(self) -> int:
        """失联后的等待：**至少 30 秒**（用户 2026-10-23 明确要求）。

        ★ 旧实现只等 1~5 秒 —— 网络还没恢复就连撞三次，
          107 秒就把 agent 判死了。Cloudflare 隧道闪断、
          家里 Wi-Fi 抖一下都会中招。
          现在按 RETRY_BACKOFF 退避：30 → 45 → 60 → 60 …
        """
        with self._lock:
            i = min(self._backoff_i, len(RETRY_BACKOFF) - 1)
            self._backoff_i += 1
        wait = RETRY_BACKOFF[i]
        self.log(f"… 联系不上控制面，等 {wait} 秒后重试"
                 f"（第 {i + 1} 次退避；网络/隧道恢复需要时间，别急着放弃）")
        time.sleep(wait)
        return wait

    def should_stop(self) -> bool:
        """给采集器用的停止回调。"""
        return self.link_lost

    def _set_state(self, state: str, note: str = "") -> None:
        """切换当前状态，并把状态**打进日志**。

        ★ 为什么要打成一行带标记的日志（2026-10-23 修）
          本机助手（msyt-helper.py）是从 agent 的 stdout 里读日志的，
          它据此判断「这个 agent 到底在干嘛」。
          没有这行标记，助手只能靠「多久没动静」去猜 ——
          于是「**在等活**」会被误报成「**卡住了**」（实测踩过：
          池子被别的节点占死，agent 领不到活却一声不吭）。
          标记格式：`[[state]] <状态>`，助手按前缀识别。
        """
        if state == self.state and note == self.state_note:
            return
        self.state = state
        self.state_note = note
        self.log(f"[[state]] {state}" + (f" · {note}" if note else ""))

    def _hb_loop(self) -> None:
        """后台心跳线程：定期证明「我还连着」。"""
        while not self._hb_stop.wait(self.heartbeat):
            try:
                # ★ 心跳带上**当前状态**：这样界面上能区分
                #   「正在采」/「在等活」/「卡住了」，
                #   而不必靠「多久没动静」去猜。
                # ★ count_fail=False：心跳走**自己的**计数（_mark_hb_fail），
                #   不跟主请求混算（否则「3 次」其实只有 1.5 轮）。
                self._req("POST", "/api/agent/heartbeat",
                          {"node": self.node, "state": self.state,
                           "note": self.state_note},
                          count_fail=False)
            except LinkLost as e:
                # ★ 还没到失联阈值 → **继续下一轮**，让失败计数继续累加；
                #   到了阈值（link_lost 已置位）→ 线程收工，等主循环收尾。
                self._mark_hb_fail(str(e))
                if self.link_lost:
                    return
            except Exception as e:              # noqa: BLE001
                self.log(f"心跳异常（不影响继续采）：{e}")

    def _start_heartbeat(self) -> None:
        if self.heartbeat <= 0 or self._hb_thread:
            return
        self._hb_thread = threading.Thread(target=self._hb_loop,
                                           name="msyt-heartbeat", daemon=True)
        self._hb_thread.start()

    def _stop_heartbeat(self) -> None:
        self._hb_stop.set()

    # ---------------------------------------------------------------- 登录
    def _load_ticket_file(self) -> str:
        """找同目录（或工作目录）下的 `agent-ticket.txt`。

        ★ 那是**网站登录后签发的一次性票据**：30 分钟内有效、用一次作废。
        有了它，agent 启动就能直接换成 JWT —— **组员不用再输密码**。
        放在文件里（不是命令行）是因为命令行参数在进程列表里是公开的。
        """
        if self.ticket:
            return self.ticket
        here = Path(__file__).resolve().parent
        for cand in (here.parent / "agent-ticket.txt", Path.cwd() / "agent-ticket.txt"):
            try:
                if not cand.exists():
                    continue
                t = cand.read_text(encoding="utf-8").splitlines()
                for line in t:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.ticket = line
                        self.log(f"已读取启动凭证（{cand.name}）")
                        return self.ticket
            except Exception:                         # noqa: BLE001
                continue
        return ""

    def login(self) -> Dict[str, Any]:
        """拿令牌。

        四条路，优先级从高到低：
          ① 已有 token（网页端「一键启动」传过来的）→ 直接用，不登录
          ② 有 ticket（**网站登录后签发的启动凭证**）→ 先换 JWT
          ③ 邮箱 + 密码 → 正常登录（交互式问，密码带 `*` 回显）

        ★ 登录前先**探测地址**（2026-10-23）：
          启动包由「本地浏览器 + 服务器」协同生成，带主/备两个地址，
          这里先探一下，别让组员为了换个地址重下包。
        """
        # ★ 先选址（主地址不通就换备用）
        self.pick_server()
        self.log(f"msyt agent {AGENT_VERSION} (build {AGENT_BUILD}) · 服务端 {self.server}")
        if self.token and not self.email:
            self.member = self._whoami()
            # ★ 身份取不到时的兜底：**从令牌里本地解**（不联网）
            #   否则节点号会留空 → 领活时用空节点去领 → 池子被记乱。
            if not (self.member or {}).get("node_id"):
                fb = self._identity_from_token()
                if fb.get("node_id"):
                    self.member = {**(self.member or {}), **fb}
                    self.log("（身份接口暂时不通，节点号改从令牌里取）")
            self.node = self.node or self.member.get("node_id") or ""
            self.link_lost = False
            with self._lock:
                self._fail_streak = 0
            self._log_login("使用已有令牌")
            self._start_heartbeat()
            return {"token": self.token, "member": self.member}
        ticket = self._load_ticket_file()
        if ticket and not self.token and not self.email:
            try:
                # ★ count_fail=False（2026-10-23 改）：
                #   票据是**一次性**的，兑换失败（过期/用过/网络不通）重试也没用，
                #   不该把它算进「失联计数」——
                #   否则一次票据问题就能把后面的网络判定搅浑。
                d = self._req("POST", "/api/agent/redeem",
                              {"ticket": ticket}, auth=False, count_fail=False)
            except Exception as e:                     # noqa: BLE001
                # ★ 要区分两种失败（2026-10-23 改）：
                #   · **网络/服务端不通**（LinkLost / 连接错误）
                #     → 凭证**可能没问题**，是网络抖了 → 该重试，不是重下包
                #   · **凭证本身不行**（401 票据无效/过期/用过、403 账号不可用）
                #     → 重试没用 → 该回网页重新下载包
                #   原来一律说「启动凭证不可用」，组员看到网络抖动
                #   也以为自己要重新下包，白折腾。
                msg = str(e)
                is_net = isinstance(e, LinkLost) or (
                    "连接失败" in msg or "timed out" in msg.lower()
                    or "ConnectionError" in msg or "Connection aborted" in msg)
                if is_net:
                    self.log(f"启动凭证暂时兑换不了（**网络/服务端不通**，"
                             f"凭证本身可能没问题）：{msg}")
                    self.log("  → 这多半是网络抖动或隧道闪断，"
                             "**过一会儿会自动重试**，不用重新下包。")
                else:
                    self.log(f"启动凭证不可用（{msg}）")
                    self.log("  → 回网页重新下载一次启动包即可"
                             "（包里的凭证跟你登录的账号绑定）。")
                # 票据过期/用过 → 不放弃，退回交互式输入账号密码。
                # ★ 但如果调用方**没给**邮箱密码，CLI 那边是以为"有票据就不用问"，
                #   于是会卡在这 → 这里明确抛错并打标记，让 CLI 改成问密码。
                if not self.email:
                    err = AgentError(f"启动凭证不可用：{e}")
                    err.need_credentials = True      # type: ignore[attr-defined]
                    raise err
                self.ticket = ""
            else:
                self.token = d.get("token") or ""
                self.member = d.get("member") or {}
                self.node = self.node or self.member.get("node_id") or ""
                self.link_lost = False
                with self._lock:
                    self._fail_streak = 0
                self._log_login("凭证已兑换")
                self._start_heartbeat()
                return d
        d = self._login_with_retry()
        self.token = d.get("token") or ""
        self.member = d.get("member") or {}
        self.node = self.node or self.member.get("node_id") or "member:" + self.email
        self.link_lost = False
        with self._lock:
            self._fail_streak = 0
        self._log_login("登录成功")
        self._start_heartbeat()
        return d

    def _login_with_retry(self, tries: int = 3) -> Dict[str, Any]:
        """登录时对**服务端临时不可用**（5xx）重试几次。

        ★ 控制面重启、隧道闪断都会让 agent 碰到 502 ——
          一次就退出太脆（实测：服务端重启 5 秒，agent 就没了）。
          连试几次，每次退避一点；全失败才把「失联」交出去。

        ★ 退避改成 ≥30 秒（2026-10-23 按组员反馈改）：
          原来 1 秒 / 2 秒 —— 隧道闪断那一瞬根本缓不过来，
          连撞三次直接判失联。现在 30 / 45 / 60。
        """
        last: Optional[Exception] = None
        for i in range(max(1, tries)):
            try:
                return self._req("POST", "/api/agent/login",
                                 {"email": self.email, "password": self.password},
                                 auth=False)
            except LinkLost as e:
                last = e
                wait = RETRY_BACKOFF[min(i, len(RETRY_BACKOFF) - 1)]
                self.log(f"登录暂时失败（{e}），{wait} 秒后重试"
                         f"（{i + 1}/{tries}）")
                time.sleep(wait)
        raise LinkLost(str(last) if last else "登录失败")

    def _log_login(self, how: str) -> None:
        """打印登录结果。

        ★ 角色要**兜底**：万一服务端返回里没有 `member` 字段（版本对不上、
          或者包是旧的），显示 `None` 会让人以为是账号有问题。
          这里先补齐（用 `/api/auth/me` 再问一次），补不到就显示「未知」，
          并把服务端返回的键名打出来 —— 出问题时一眼能看出是不是版本不匹配。
        """
        role = (self.member or {}).get("role")
        if not role and self.token:
            self.member = self._whoami() or self.member
            role = (self.member or {}).get("role")
        self.log(f"{how} · 节点 {self.node} · 角色 {role or '未知'}")
        if not role:
            self.log("  ⚠ 服务端没返回角色信息 —— 启动包可能是旧版本，"
                     "建议回网页重新下载一次")

    def _whoami(self) -> Dict[str, Any]:
        try:
            # 辅助请求用**短超时**（10s）：它失败也不影响主流程，
            # 不值得为它占住 20 秒（组员实测：这里白等过 41 秒）。
            return self._req("GET", "/api/agent/me", timeout=HTTP_TIMEOUT_AUX) or {}
        except Exception as e:                             # noqa: BLE001
            self.log(f"取身份失败：{e}")
            return {}

    def _identity_from_token(self) -> Dict[str, Any]:
        """从**令牌本身**里读出身份（email / role / node），**不联网**。

        ★ 为什么需要这条兜底（2026-10-23 实测踩过）
          `login()` 拿「已有令牌」跑时，节点号是问服务端 `/api/agent/me` 拿的。
          那一刻恰好隧道在重连 → 返回 520 → 身份取空 → **节点号成了空串** →
          接着领活时用 `node=''` 去领 → 活被记在一个不存在的节点名下 →
          界面上的「各节点完成数」全对不上。
          而令牌里**本来就带着** email/role/node（JWT payload），
          本地解一下就行，不必依赖网络。
        """
        if not self.token or "." not in self.token:
            return {}
        try:
            import base64
            part = self.token.split(".")[1]
            part += "=" * (-len(part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
        except Exception:                             # noqa: BLE001
            return {}
        return {"email": payload.get("email") or "",
                "role": payload.get("role") or "",
                "node_id": payload.get("node") or ""}

    # ---------------------------------------------------------------- 自主采集
    def run_local(self, sources: str = "", pages: int = 20,
                  tids: Optional[List[int]] = None) -> Dict[str, Any]:
        """**在成员自己机器上**采指定的东西，采完回传服务器。

        ★ 为什么要有这个模式（2026-10-23）
          站方**不提供 CORS** → 浏览器里的网页不能直接采站方，
          成员的采集**必须**跑在自己机器上的程序里。
          以前成员在网页上点「开始采集」，任务其实 spawn 在**管理员的服务器**里
          → 站方看到的流量全部来自管理员那一个 IP，
          「多个分散的普通用户」这个 Beta 的**核心前提**直接落空
          （实测：成员点一次，dept:555 在管理员的 IP 上从 58 页刷到 500 页）。
          所以：成员的采集一律走本机 agent，采完回传。

        两种目标：
          · `sources` 非空 → 采**列表页元数据**（部门/分类/全局视图…）
            → 回传 `/api/agent/ingest-list`（幂等入权威库，**不推水位线**）
          · `tids` 非空   → 采**指定帖的正文**
            → 回传 `/api/assist/submit`（走与协作完全相同的合并路径）
        """
        from .config import Config
        from .db import DB
        from .http_client import Client
        from .listpage import ListCollector
        from .sources import expand_sources

        cfg = Config.load()
        client = Client(cfg, verbose=False)
        out: Dict[str, Any] = {"list_rows": 0, "list_sources": 0,
                               "detail_ok": 0, "detail_failed": 0,
                               "detail_restricted": 0, "skipped": 0}

        # ---------- 列表页元数据 ----------
        if sources.strip():
            srcs = expand_sources(sources.strip())
            if not srcs:
                self.log(f"⚠ 采集源写错了：{sources}（没解析出任何源）")
            # ★ 先问服务端要各源的水位线 —— 从**下一页**接着采，
            #   而不是从第 1 页重来（那会把管理员已采过的页又请求一遍，
            #   白耗站方的耐心，也白费成员的时间）。
            wm: Dict[str, Dict[str, Any]] = {}
            try:
                st = self._req("GET", "/api/agent/watermarks") or {}
                for k, w in (st.get("watermarks") or {}).items():
                    wm[str(k)] = w
            except Exception as e:                     # noqa: BLE001
                self.log(f"（取水位线失败，将从第 1 页开始：{e}）")
            mem = DB(":memory:")
            lc = ListCollector(mem, client, cfg)
            for s in srcs:
                w = wm.get(s.key) or {}
                if str(w.get("status") or "") == "done":
                    self.log(f"  {s.key}：服务端已采完，跳过")
                    continue
                last = int(w.get("last_page") or 0)
                if last:
                    # 在**内存库**里预置水位线 → collect 会从 last_page+1 开始；
                    # 内存库的水位线不会回流到服务器（权威水位线仍归管理员）。
                    mem.save_watermark(
                        s.key, dim=s.dim, code=s.code, name=s.name,
                        url=s.url, status="in_progress", last_page=last,
                        rows_total=int(w.get("rows_total") or 0))
                    self.log(f"  {s.key}：从第 {last + 1} 页接着采"
                             f"（服务端已到第 {last} 页）")
                self._set_state("collecting", f"本机采列表源 {s.key}")
                self.log(f"本机开始采列表源 {s.key}（{s.name}），最多 {pages} 页")
                try:
                    lc.collect(s, max_pages=pages, resume=True,
                               stop_after=0, rescan_head=0)
                except Exception as e:                 # noqa: BLE001
                    self.log(f"⚠ 列表源 {s.key} 采集中断：{e}")
                rows = [dict(r) for r in mem.conn.execute(
                    "SELECT * FROM threads")]
                tags = [[int(t["tid"]), t["dim"], t["code"], t["name"]]
                        for t in mem.conn.execute(
                            "SELECT tid,dim,code,name FROM thread_tags")]
                if not rows:
                    self.log(f"  {s.key}：没采到行")
                    continue
                # 分批回传（一次太多会让请求体过大）
                B = 200
                sent = 0
                for i in range(0, len(rows), B):
                    chunk = rows[i:i + B]
                    ctags = [t for t in tags if int(t[0]) in
                             {int(x["tid"]) for x in chunk}]
                    r = self._req("POST", "/api/agent/ingest-list",
                                  {"rows": chunk, "source": s.key, "tags": ctags})
                    sent += int((r or {}).get("ingested") or 0)
                out["list_rows"] += sent
                out["list_sources"] += 1
                self.log(f"  {s.key}：采到 {len(rows)} 帖，已回传 {sent} 帖")
            mem.close()

        # ---------- 指定帖的正文 ----------
        tids = [int(t) for t in (tids or []) if str(t).strip()]
        if tids:
            code = f"LOCAL:{self.node or 'member'}"
            for tid in tids:
                if self.link_lost:
                    self.log("与控制面失联 → 停止本机采集。")
                    break
                self._set_state("collecting", f"本机采正文 tid={tid}")
                self.log(f"本机开始采正文 tid={tid}")
                try:
                    payload = self.collect_one(tid)
                except Exception as e:                 # noqa: BLE001
                    out["detail_failed"] += 1
                    self.log(f"✗ tid={tid} 采失败：{e}")
                    continue
                ok = bool(payload["summary"].get("ok"))
                restricted = bool(payload["summary"].get("restricted"))
                reason = payload["summary"].get("reason") or ""
                try:
                    self.submit(code, tid, payload)
                except Exception as e:                 # noqa: BLE001
                    out["detail_failed"] += 1
                    self.log(f"✗ tid={tid} 回传失败：{e}")
                    continue
                if ok:
                    out["detail_ok"] += 1
                elif restricted:
                    out["detail_restricted"] += 1
                    self.log(f"⊘ tid={tid} 站方限制：{reason}（已标记，不计入已采详情帖）")
                else:
                    out["detail_failed"] += 1
        return out

    # ---------------------------------------------------------------- 领活
    def pick(self) -> List[Dict[str, Any]]:
        # ★ 节点号为空**不许领活**：否则活会被记在一个不存在的节点名下，
        #   界面上的「各节点完成数」全对不上（实测踩过：身份接口 520
        #   → 节点号空 → 用空节点领了 33 条活）。
        if not self.node:
            raise AgentError("节点号未知，无法领活（请重新启动 agent）")
        body: Dict[str, Any] = {"node": self.node, "limit": self.limit or 50}
        if self.task:
            body["code"] = self.task
        d = self._req("POST", "/api/agent/pick", body)
        return d.get("picks") or []

    # ---------------------------------------------------------------- 采集
    def collect_one(self, tid: int) -> Optional[Dict[str, Any]]:
        """采一帖详情。**全程内存**，采完把内存库整个丢掉。

        ★ 采的过程中如果失联了（心跳线程判定的），
          `should_stop` 会让 collect_one **在下一个页的边界停下**，
          不把整帖白采完。
        """
        cfg = Config.load()
        # ★ 内存库：:memory: → 不产生任何磁盘文件
        mem = DB(":memory:")
        # 详情表有外键指向 threads，先插一行占位（真实库里这行本来就存在）
        with mem.lock:
            mem.conn.execute(
                "INSERT OR IGNORE INTO threads(tid,title,detail_state) VALUES(?,?,'pending')",
                (int(tid), f"tid-{tid}"))
            mem.conn.commit()

        client = Client(cfg, verbose=False)
        dc = DetailCollector(mem, client, cfg)
        res = dc.collect_one(int(tid), should_stop=self.should_stop)

        posts = [dict(r) for r in mem.conn.execute(
            "SELECT pid,page_no,floor,author,author_uid,post_time,content,chars,images "
            "FROM posts WHERE tid=? AND pid>0", (int(tid),))]
        pages = [dict(r) for r in mem.conn.execute(
            "SELECT page_no,pids,n FROM thread_pages WHERE tid=? ORDER BY page_no",
            (int(tid),))]
        mem.close()                      # 内存库随连接一起消失
        # ★ 站方限制（正文不公开）：单独标记回传，**不当作楼层**。
        #   内存库里那条 pid<0 的「站方提示」是标记，不是真实楼层，
        #   所以上面用 pid>0 把它排掉了。
        restricted = bool(res.get("restricted"))
        return {"ok": bool(res.get("ok")), "posts": posts, "pages": pages,
                "stopped": bool(res.get("stopped")),
                "restricted": restricted,
                "summary": {"ok": bool(res.get("ok")),
                            "restricted": restricted,
                            "reason": res.get("reason") or "",
                            "pages": res.get("pages"),
                            "posts": res.get("posts"),
                            "chars": res.get("chars"),
                            "head_pid": res.get("head_pid"),
                            "tail_pid": res.get("tail_pid"),
                            "warnings": res.get("warnings") or []}}

    # ---------------------------------------------------------------- 回传
    def submit(self, code: str, tid: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        r = self._req("POST", "/api/agent/submit", {
            "node": self.node, "code": code, "tid": int(tid),
            "posts": payload["posts"], "pages": payload["pages"],
            "summary": payload["summary"]})
        # ★ 回传完毕 → 立刻清空内存里的正文
        payload["posts"].clear()
        payload["pages"].clear()
        payload["summary"] = {}
        return r

    def _submit_with_retry(self, code: str, tid: int,
                           payload: Dict[str, Any]) -> Dict[str, Any]:
        """回传一帖，**带退避重试**（2026-10-23 按组员反馈加）。

        ★ 为什么必须重试（用户原话：「回传后失联就断开，不重试不大合理」）：
          旧实现：回传时一次 LinkLost → **本帖丢弃**（正文白采了）
                                → **break** 整个采集循环 → 剩下的几十帖全不采了。
          实测（组员日志）：
            20:52:33 ⚠ 连接失败：ReadTimeout（连续第 1 次）
            20:52:33 ✗ tid=8342251 回传时失联，本帖丢弃
            20:52:36 结束：完成 20 帖，失败 1 帖（因与控制面失联而自动停止）
          —— 一次 20 秒超时，代价是**一帖白采 + 整个 agent 停**。

        ★ 重试是安全的：回传走的是「**合并覆盖**」语义（§5.13），
          同一帖重复提交只会覆盖成同样的内容，**幂等**。

        ★ 退避间隔 ≥30 秒（RETRY_BACKOFF），与领活那条路一致。

        返回：{"ok": bool, "was_ok":…, "was_restricted":…, "reason":…,
               "n_pages":…, "n_posts":…, "n_chars":…, "result":…}
        """
        # ★ 关键：submit() 会**清空** payload（传完即删），
        #   所以计数/摘要必须在**每次尝试前**重新取 —— 重试时 payload 已被清空，
        #   得自己留着副本。
        snap = {
            "was_ok": bool(payload["summary"].get("ok")),
            "was_restricted": bool(payload["summary"].get("restricted")),
            "reason": payload["summary"].get("reason") or "",
            "n_pages": payload["summary"].get("pages") or 0,
            "n_posts": payload["summary"].get("posts") or 0,
            "n_chars": payload["summary"].get("chars") or 0,
        }
        last = ""
        for attempt in range(SUBMIT_RETRY):
            if attempt:
                self._backoff_wait()
                # ★ payload 被上一次失败的 submit 清空了吗？
                #   没有 —— submit() 只有在**成功返回后**才清空；
                #   抛异常的话清空那几行不会执行。
                #   但为了稳妥，重试前确认一下还在。
                if not payload.get("posts"):
                    self.log(f"  ⚠ 这一帖的正文已不在内存里，无法重试，放弃")
                    break
            try:
                r = self.submit(code, tid, payload)
                return {"ok": True, "result": r, **snap}
            except LinkLost as e:
                last = str(e)
                self.log(f"⚠ tid={tid} 回传失败（第 {attempt + 1}/{SUBMIT_RETRY} 次）："
                         f"{type(e).__name__}")
                if attempt + 1 < SUBMIT_RETRY:
                    self.log("  → 这一帖**不丢**，等网络恢复后重试"
                             "（回传是合并覆盖，重试是安全的）")
            except Exception as e:                  # noqa: BLE001
                last = str(e)
                self.log(f"✗ tid={tid} 回传出错：{e}")
                break
        self.log(f"✗ tid={tid} 回传重试用尽，放弃这一帖（{last[:120]}）")
        return {"ok": False, "result": {}, **snap}

    # ---------------------------------------------------------------- 主循环
    def run(self, once: bool = False, max_rounds: int = 0) -> Dict[str, Any]:
        self.login()
        # ★ 启动时先**退掉自己上次遗留的活**（2026-10-23）
        #   上一次可能是被 Ctrl+C、被 kill、或窗口直接关掉的 ——
        #   这时 finally 里的退活没跑成，手里那些活就卡在 'taken'。
        #   而领活 SQL 只取 state='pending' → **本节点把自己堵死了**：
        #   池子明明有 pending 的活，它却领不到，界面显示「在等活」。
        #   （实测踩过：重启后 agent 一直说"没活可领"，其实有 197 条 pending，
        #    全被它自己上次没做完的 81 条 taken 挡住了。）
        #   agent 只要活着就每 12 秒发心跳 → 心跳断了 2 分钟的活由服务端回收；
        #   但**自己的**遗留活要自己主动退。
        self.return_picks()
        rounds = 0
        done = failed = restricted = 0
        stopped_by_link = False
        #: 连续回传失败的帖数（2026-10-23 加）。
        #:   单帖回传失败 → 只放弃那一帖，**继续采下一帖**；
        #:   连续 SUBMIT_FAIL_LIMIT 帖都失败 → 才判定控制面真的不可用。
        submit_fail_streak = 0
        try:
            while True:
                if self.link_lost:
                    stopped_by_link = True
                    self.log("与控制面失联 → 停止领活与采集。")
                    break
                if max_rounds and rounds >= max_rounds:
                    break
                rounds += 1
                # ★ 领活时的连接失败**不能直接崩掉整个 agent**：
                #   未达失联阈值 → 当作临时抖动，等一等再试（网络抖一下很常见）；
                #   达到阈值（link_lost 已置位）→ 才正式判定失联并停止。
                #   旧实现让 LinkLost 直接往上抛，agent 第一次连不上就退出，
                #   既没给出「失联」的判断过程，也没汇报「因失联停止」。
                try:
                    picks = self.pick()
                except LinkLost:
                    if self.link_lost:
                        stopped_by_link = True
                        self.log("与控制面失联 → 停止领活与采集。")
                        break
                    # ★ 等 ≥30 秒再重试（2026-10-23 按组员反馈改）：
                    #   原来只等 1~5 秒 —— 网络还没恢复就连撞三次，
                    #   107 秒就把 agent 判死了（活停在半路）。
                    self._backoff_wait()
                    continue
                if not picks:
                    # ★★ 领不到活**必须说话**（2026-10-23 修）
                    #   旧实现这里直接 continue —— agent 一声不吭地空转，
                    #   界面上只看得到「运行中，但很久没动静」，
                    #   用户完全分不清是「卡住了」还是「**没活可领**」。
                    #   实测踩过：另一个节点的 agent 领了 400 条活就崩了，
                    #   活卡在 taken 状态，池子等于被占死 ——
                    #   本机 agent 领不到活又不说，界面看着像程序坏了。
                    if once:
                        break
                    self._set_state("idle",
                                    "池子里暂时没有可领的活（可能被别的节点领走未回传）")
                    # 每 10 轮说一次，避免刷屏
                    if rounds % 10 == 1:
                        self.log(f"… {self.state_note}（继续等，超时未回传的活会自动退回）")
                    time.sleep(self.interval)
                    continue
                self._set_state("collecting", f"正在采第 {rounds} 轮，共 {len(picks)} 帖")
                # ★ 这行**必须打**：本机助手面板靠它数「已领 N 帖」
                #   （正则 /领走 (\d+) 帖/）。不打的话面板永远显示 0，
                #   用户看着就像"什么都没采"（实测踩过，正是用户反馈的那点）。
                self.log(f"领走 {len(picks)} 帖")
                for p in picks:
                    if self.link_lost:
                        stopped_by_link = True
                        break
                    code, tid = p.get("task_code"), int(p.get("tid") or 0)
                    if not tid:
                        continue
                    self.log(f"开始采 {code} tid={tid}")
                    try:
                        payload = self.collect_one(tid)
                        # ★ 必须在 submit 之前取 ok ——
                        #   submit() 会把 payload 整个清空（"传完即删"），
                        #   清空后再读 summary.ok 就永远是 False，计数会全错。
                        was_ok = bool(payload["summary"].get("ok"))
                        was_restricted = bool(payload["summary"].get("restricted"))
                        # ★ reason 要在 submit 之前取 —— submit 会清空整个 payload
                        reason = payload["summary"].get("reason") or ""
                        # ★ 这几个数也要在 submit 之前取（submit 清空 payload）
                        n_pages = payload["summary"].get("pages") or 0
                        n_posts = payload["summary"].get("posts") or 0
                        n_chars = payload["summary"].get("chars") or 0
                        if payload.get("stopped"):
                            self.log(f"⚠ tid={tid} 采到一半被中止（失联），不回传半截数据")
                            failed += 1
                            continue
                        r = self._submit_with_retry(code, tid, payload)
                        if r["ok"]:
                            was_ok = r["was_ok"]
                            was_restricted = r["was_restricted"]
                            reason = r["reason"]
                            n_pages = r["n_pages"]
                            n_posts = r["n_posts"]
                            n_chars = r["n_chars"]
                        else:
                            # ★ 重试用尽仍失败：只放弃**这一帖**，继续采下一帖
                            #   （用户 2026-10-23：「回传后失联就断开，不重试不大合理」）
                            failed += 1
                            submit_fail_streak += 1
                            if submit_fail_streak >= SUBMIT_FAIL_LIMIT:
                                stopped_by_link = True
                                self.log(f"✗ 连续 {submit_fail_streak} 帖回传都失败 —— "
                                         f"判定控制面不可用，停止采集。")
                                break
                            self.log(f"  → 这一帖放弃（数据只在内存里，不会残留），"
                                     f"**继续采下一帖**"
                                     f"（连续失败 {submit_fail_streak}/"
                                     f"{SUBMIT_FAIL_LIMIT}）")
                            continue
                        if was_ok:
                            done += 1
                            # ★ 这行**必须打**：本机助手面板靠它数「已完成 N 帖」
                            #   （正则 /tid=\d+ ✓/）。不打的话面板永远显示 0，
                            #   用户看着就像"什么都没采"（实测踩过）。
                            self.log(f"tid={tid} ✓ {n_pages} 页 / {n_posts} 楼 / "
                                     f"{n_chars} 字")
                        elif was_restricted:
                            # ★ 站方限制：正文不公开 —— 不是成员的失败，
                            #   服务端会把它记成「站方限制」并写入正文标记。
                            restricted += 1
                            self.log(f"⊘ tid={tid} 站方限制：{reason or '正文不公开'}"
                                     f"（已标记，不计入已采详情帖）")
                        else:
                            failed += 1
                        if isinstance(r.get("result"), dict) and r["result"].get("skipped"):
                            self.log(f"· tid={tid} 服务端保留本地已有数据"
                                     f"（{r['result'].get('reason')}），不覆盖")
                        submit_fail_streak = 0
                    except LinkLost:
                        # ★ 采正文阶段失联（不是回传阶段 —— 回传由
                        #   _submit_with_retry 内部重试，不会抛到这里）。
                        #   正文没采完，没有可回传的数据 → 放弃这一帖，
                        #   **继续采下一帖**（2026-10-23：一次抖动不该让整个 agent 停）。
                        #   真正的「失联」由 _fail_streak 累计判定
                        #   （连着 fail_limit 次才置 link_lost）。
                        self.log(f"✗ tid={tid} 采正文时联系不上控制面，本帖放弃"
                                 f"（继续采下一帖）")
                        failed += 1
                        continue
                    except Exception as e:              # noqa: BLE001
                        failed += 1
                        self.log(f"✗ {code} tid={tid} 失败：{e}")
                        # 出错就把栈打出来 —— 只打一行 message 会让人查半天
                        if self.verbose:
                            import traceback
                            self.log(traceback.format_exc()[-1200:])
                if stopped_by_link or once:
                    break
                time.sleep(self.interval)
        finally:
            self._stop_heartbeat()
            # ★ 退出前**主动退活**（2026-10-23）
            #   手里还有没回传的活（关窗口 / Ctrl+C / 跑完一轮就停），
            #   不退回去它们会卡在 taken 直到 30 分钟超时回收 ——
            #   期间整个池子对别的成员是空的（实测踩过）。
            self.return_picks()
        return {"done": done, "failed": failed, "restricted": restricted,
                "rounds": rounds, "stopped_by_link": stopped_by_link}

    def return_picks(self) -> int:
        """把本节点领走但还没回传的活退回池子（退出/启动时调用）。"""
        if not self.token:
            return 0
        try:
            r = self._req("POST", "/api/agent/return", {"node": self.node},
                          count_fail=False, timeout=HTTP_TIMEOUT_AUX)
            n = int((r or {}).get("returned") or 0)
            if n:
                self.log(f"↩ 已把 {n} 条未回传的活退回池子（别人可以领了）")
            return n
        except Exception as e:                            # noqa: BLE001
            # ★ 退活失败**要说出来**（以前这里静默吞掉，
            #   于是 agent 退活没生效却一声不吭 → 池子被自己的遗留活堵死，
            #   界面只显示"在等活"，完全看不出原因）。
            self.log(f"⚠ 退活失败（不影响采集）：{e}")
            return 0
