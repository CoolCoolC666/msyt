# -*- coding: utf-8 -*-
"""
webapp.py —— msyt 的本地 Web 前端
================================================================
为什么要它
----------
命令行能跑，但"给小组看"和"自己翻数据"需要界面。本模块把 `msyt` 已有的
能力原样搬到浏览器里：**不重写任何采集逻辑**，只是把
`DB` / `ListCollector` / `DetailCollector` 包成 HTTP 接口。

三个必须处理的技术点
--------------------
1) **SQLite 多线程**：`DB.__init__` 用的是 `sqlite3.connect(path, timeout=30)`，
   默认 `check_same_thread=True` —— 同一个连接跨线程用会抛
   `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
   所以这里用**线程局部（threading.local）**给每个线程各开一个连接。
   库是 WAL 模式，读写可以并发。

2) **长任务不能阻塞 HTTP**：`msyt list --pages 500` 要跑几十分钟，
   不能在请求线程里同步跑。这里用**进程内任务表 + 后台线程**：
   启动接口立刻返回 job_id，前端轮询 `/api/jobs/{id}` 看进度和日志。

3) **日志怎么捕获**：采集器的所有进度输出都走 `Client.log()`（不是裸 print），
   所以只要给 Client 套一个把消息写进任务缓冲的子类就能拿到实时日志，
   **不必重定向 stdout**（重定向是全局的，多任务时会串）。

任务模型
--------
    pending → running → done / failed / stopped
`stopped` 是 Web 前端特有的：用户点"停止"，采集器在**下一个页/帖的边界**
优雅停下 —— 进度已落库，下次继续（断点续传）。
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse, Response,
                              StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assist import AssistService
from .auth import (email_allowed, hash_pw, make_token, new_secret, read_token,
                   verify_pw)
from . import bundle
from .config import Config
from .db import DB
from .detail import DetailCollector
from .hub import Hub
from .http_client import Client
from .listpage import ListCollector
from .sources import Source, describe, expand_sources, make_source
from .taxonomy import CATEGORIES, LIGHTS, MAILBOXES, STATES

WEB_DIR = Path(__file__).resolve().parent / "web"

# ★ 控制台编码兜底（Windows 默认 GBK）
#   采集日志里有「⚠」「≠」「✓」等字符，GBK 编不了 → print 直接抛
#   UnicodeEncodeError → 采集线程被打挂（实测踩过）。
#   这里把 stdout/stderr 换成 UTF-8 + replace，之后就算有编不了的字符也只是显示成 ?。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                 # noqa: BLE001
        pass

app = FastAPI(title="民生e点通 · 舆情采集器", version="1.1.0",
              description="本地 Web 前端。所有数据来自本机 SQLite，不做任何写操作到站方。")


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """静态资源**禁止缓存**。

    ⚠ 不加这一段，浏览器会用启发式缓存（Last-Modified 没有配 Cache-Control 时
    照样缓存），改了 app.js 之后浏览器还在跑**旧代码** ——
    症状是「我明明改了，怎么没生效」，极难自查。
    本地开发工具本来就该每次都拿最新的，所以直接 no-store。
    """
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path == "/":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


#: agent 专用子域名（`agentapi.…`）。那边只开放 `/api/agent/*`。
AGENT_HOST_PREFIX = "agentapi."
AGENT_API_PREFIX = "/api/agent/"


@app.middleware("http")
async def _restrict_agent_host(request: Request, call_next):
    """**agent 专用域名上只开放 `/api/agent/*`。**

    ★ 为什么要有这一道：
      `agentapi.…` 是给成员端 agent 用的域名，它在 Cloudflare Access 那边
      **整个域名 bypass**（agent 没有浏览器会话，过不了 Access 的登录门）。
      于是这个域名上的**其它**路径默认也是公开可达的 ——
      虽然它们仍要程序自己的 JWT，但「公网可达」这件事本身该收紧。

    这里按 Host 判定：不在 `/api/agent/` 下的路径一律 403。
    主域名（网页 UI）不受影响，照旧走 Access + 程序双层。
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.startswith(AGENT_HOST_PREFIX):
        if not request.url.path.startswith(AGENT_API_PREFIX):
            return JSONResponse(
                {"detail": f"该域名只开放 {AGENT_API_PREFIX}（agent 专用）"},
                status_code=403)
    return await call_next(request)


# ====================================================================== 公网加固
# ★ 背景（2026-10-26 实测）
#   公网入口是 Cloudflare **quick tunnel**，它上面**没有 Cloudflare Access**
#   （Access 只能挂在命名隧道的固定 Public Hostname 上）→ 匿名访客能直接
#   摸到这个 Web 应用。实测匿名能拿到：
#     · `/docs` + `/openapi.json` → **58 个接口的完整地图**（含字段名）
#     · `/api/health` → **本机绝对路径**（含用户名、盘符、项目名）
#     · `/` 首页 → **采集目标站名**（天一论坛 · 群众留言板）
#   数据接口本身有鉴权（401），但「地图」本身就够外网人定位我们了。
#
# ★ 这一段做四件事：
#   ① 本机判定（看 **Host**，不看 client.host —— cloudflared 是本机回源的，
#      用 client.host 会把公网访客当成本机用户放过去，等于加固白做）
#   ② 关闭 Swagger / openapi（只对本机开放）
#   ③ `/api/health` 公网只回 `{"ok": true}`
#   ④ 站点访问口令（可选启用：`程序/data/gate-key.txt` 存在且非空即开）


def _host_of(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].lower()


def _is_local_request(request: Request) -> bool:
    """这个请求是不是**本机浏览器**发出来的？

    ★ 为什么看 Host 而不是 client.host：
      cloudflared 在**本机**回源，所以经隧道进来的公网请求，
      `request.client.host` 同样是 127.0.0.1 —— 用它判断会把
      **公网访客当成本机用户**直接放过去。
      Host 头不会被改：本机是 `127.0.0.1:8765` / `localhost`，
      公网是 `xxx.trycloudflare.com`。
    """
    h = _host_of(request)
    return h in ("localhost", "::1", "[::1]") or h.startswith("127.")


def _gate_key() -> str:
    """站点访问口令 —— `程序/data/gate-key.txt` 存在且非空即启用。"""
    try:
        p = Path(_cfg().db_path).parent / "gate-key.txt"
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _client_ip(request: Request) -> str:
    """**真实客户端 IP**（限速必须用它）。

    ★ 为什么不能用 `request.client.host`：
      经 cloudflared 回源时它**恒为 127.0.0.1** —— 照它限速等于把
      **全世界的公网访客算成同一个人**：
      外人撞 10 次密码 → 全组成员一起被锁 5 分钟（自己人反被 DoS）；
      而对单个攻击者来说，限速也几乎不起作用。
      Cloudflare 边缘会带 `CF-Connecting-IP`（真实访客 IP），
      其次 `X-Forwarded-For` 首段，最后才退回 client.host。
    """
    for h in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        v = request.headers.get(h)
        if v:
            first = v.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else "?"


#: 口令门放行的路径（前缀匹配）
#: ★ `/static/` **不在白名单里** —— 静态资源（CSS/JS）也会暴露项目结构与
#:   注释指纹，匿名能拿到就没必要给。口令页是内联样式，不依赖 static。
GATE_OPEN_PREFIXES = (
    "/api/agent/",       # 组员 agent（机器上没浏览器，过不了任何网页登录门）
    "/api/auth/login",   # 登录接口本身
    "/api/auth/gate",    # 口令校验接口
    "/api/health",       # 探活（隧道守护靠它判存活）
    "/gate",
    "/favicon.ico",
)


@app.middleware("http")
async def _public_exposure_guard(request: Request, call_next):
    """**公网暴露面收敛**（本机开发与组员 agent 都不受影响）。"""
    path = request.url.path
    local = _is_local_request(request)

    # ① Swagger / openapi：只对本机开放
    if not local and (path in ("/docs", "/redoc", "/openapi.json")
                      or path.startswith("/docs/")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    # ② 站点访问口令（可选启用）
    key = _gate_key()
    if key and not local:
        opened = (any(path.startswith(p) for p in GATE_OPEN_PREFIXES)
                  or request.cookies.get("msyt_gate") == key
                  or request.headers.get("x-msys-gate") == key)
        if not opened:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "需要访问口令"}, status_code=401)
            return RedirectResponse(url=("/gate?next=" + quote(path)),
                                    status_code=302)
    return await call_next(request)


# ====================================================================== 连接
_tls = threading.local()


def get_db(cfg: Config) -> DB:
    """线程局部的 DB 连接（见文件头第 1 点）。"""
    key = str(cfg.db_path)
    cached = getattr(_tls, "dbs", None)
    if cached is None:
        cached = {}
        _tls.dbs = cached
    db = cached.get(key)
    if db is None:
        db = DB(key)
        cached[key] = db
    return db


def _cfg() -> Config:
    cfg = Config.load()
    cfg.ensure_dirs()
    return cfg


# ====================================================================== 任务
class Job:
    """一个后台采集任务。

    `node` = 采集节点标识。现在只有管理员本机（"admin"），
    将来小组成员通过域名服务接入时各自带一个 node id
    → **同节点互斥、跨节点并行**（分工协作的基础，见分布式方案报告）。
    """

    def __init__(self, kind: str, params: Dict[str, Any], node: str = "admin"):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.node = node or "admin"
        self.state = "running"
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.stop_requested = False
        self.logs: deque = deque(maxlen=1000)
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self.logs.append({"t": time.time(), "m": str(msg)})

    def should_stop(self) -> bool:
        return self.stop_requested

    def to_dict(self, tail: int = 200) -> Dict[str, Any]:
        with self._lock:
            all_logs = list(self.logs)
            lines = all_logs[-tail:] if tail else []
            # ★ 最后一条**进度行**（2026-10-25 加）
            #   任务卡片上要直接显示「已采 N 帖，还剩 M 帖」，
            #   而列表接口的 logs 是空的（tail=0）→ 单独把进度行摘出来。
            last_progress = ""
            for x in reversed(all_logs[-60:]):
                m = str(x.get("m") or "")
                if "进度：" in m:
                    last_progress = m
                    break
        d = {
            "id": self.id,
            "kind": self.kind,
            "node": self.node,
            "params": self.params,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "result": self.result,
            "error": self.error,
            "stop_requested": self.stop_requested,
            "log_count": len(self.logs),
            "last_progress": last_progress,
        }
        d["logs"] = lines
        return d


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, params: Dict[str, Any],
               node: str = "admin") -> Job:
        job = Job(kind, params, node=node)
        with self._lock:
            # 只保留最近 50 个任务，防止内存无限涨
            if len(self._jobs) >= 50:
                old = sorted(self._jobs.values(), key=lambda j: j.started_at)[0]
                self._jobs.pop(old.id, None)
            self._jobs[job.id] = job
        return job

    def get(self, jid: str) -> Job:
        with self._lock:
            job = self._jobs.get(jid)
        if job is None:
            raise HTTPException(404, f"任务 {jid} 不存在")
        return job

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)
        return [{k: v for k, v in j.to_dict(tail=0).items() if k != "logs"}
                for j in jobs]

    def remove(self, jid: str) -> bool:
        """删掉**一个已结束**的任务（连同它的日志）。"""
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return False
            if job.state == "running":
                raise HTTPException(400, "任务还在跑 —— 先停止再删")
            del self._jobs[jid]
            return True

    def clear_finished(self) -> int:
        """删掉**所有已结束**的任务，返回删了几条。

        ★ 为什么要有这个：任务表只保留最近 50 条，回归测试每跑一轮
          就会留下一堆「已完成」的详情/列表/体检任务 → 把「任务运行情况」
          刷成一片噪音，管理员根本看不见真正在跑的那个。
        ★ **运行中的不删**（那是真的在采集）。
        """
        with self._lock:
            gone = [jid for jid, j in self._jobs.items() if j.state != "running"]
            for jid in gone:
                del self._jobs[jid]
            return len(gone)

    def spawn(self, job: Job, fn: Callable[[Job], Dict[str, Any]]) -> Job:
        def runner() -> None:
            try:
                job.result = fn(job)
                job.state = "stopped" if job.stop_requested else "done"
            except Exception as e:                       # noqa: BLE001
                job.state = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.log(f"任务失败：{job.error}")
                job.log(traceback.format_exc()[-1200:])
            finally:
                job.finished_at = time.time()

        threading.Thread(target=runner, name=f"msyt-job-{job.id}",
                         daemon=True).start()
        return job


JOBS = JobManager()


class _JobClient(Client):
    """把采集器的进度输出同时送到任务缓冲、控制台与**实时日志总线**。"""

    def __init__(self, cfg: Config, job: Job):
        super().__init__(cfg, verbose=True)
        self._job = job

    def log(self, msg: str) -> None:
        self._job.log(msg)
        # ★ 打印到控制台必须**兜住异常**：
        #   Windows 控制台默认 GBK，采集日志里的「⚠」「≠」等字符会直接抛
        #   UnicodeEncodeError —— 而这行 print 在采集线程里，
        #   一抛就把**整个采集任务**打挂（实测：漂移警告触发的崩溃，
        #   任务在 dept:512 中断，后面所有源都没采）。
        #   日志打印失败绝不该影响采集 —— 吞掉它，任务继续。
        try:
            print(f"[{self._job.kind}:{self._job.id}] {msg}", flush=True)
        except Exception:                             # noqa: BLE001
            try:
                enc = (getattr(sys.stdout, "encoding", None) or "ascii")
                print(f"[{self._job.kind}:{self._job.id}] "
                      f"{msg.encode(enc, 'replace').decode(enc, 'replace')}",
                      flush=True)
            except Exception:                         # noqa: BLE001
                pass
        # 实时总线：管理员与成员都看得到（带任务编号）
        HUB.publish(self._job.node, msg, task=self._job.kind)


# ====================================================================== 任务体
def _job_taxonomy(cfg: Config, job: Job) -> Dict[str, Any]:
    db = get_db(cfg)
    client = _JobClient(cfg, job)
    lc = ListCollector(db, client, cfg)
    tax = lc.collect_taxonomy()
    n = db.backfill_dept_meta()
    return {"depts": len(tax["depts"]), "districts": len(tax["districts"]),
            "recomputed": n}


def _job_list(cfg: Config, job: Job) -> Dict[str, Any]:
    p = job.params
    db = get_db(cfg)
    client = _JobClient(cfg, job)
    depts = [dict(r) for r in db.conn.execute(
        "SELECT typeid,name,group_code,group_name,is_district FROM depts")]

    # ---- 采集源：网页端的「下拉多选」优先，再叠加高级手写描述 ----
    srcs: List[Source] = []
    if p.get("use_global"):
        srcs.append(make_source("global", ""))
    ids = [str(x) for x in (p.get("dept_ids") or [])]
    if ids:
        srcs.extend(expand_sources("dept:" + ",".join(ids), depts))
    if p.get("sources"):
        srcs.extend(expand_sources(p["sources"], depts))

    # 去重（同一个源可能被「多选」和「手写」同时点到）
    seen, uniq = set(), []
    for s in srcs:
        if s.key in seen:
            continue
        seen.add(s.key)
        uniq.append(s)
    srcs = uniq

    if not srcs:
        raise RuntimeError("没选出任何采集源")
    job.log(f"本次采集源 {len(srcs)} 个：")
    job.log(describe(srcs[:20]) + (f"\n  …（其余 {len(srcs) - 20} 个从略）"
                                 if len(srcs) > 20 else ""))
    lc = ListCollector(db, client, cfg)
    if p.get("taxonomy"):
        lc.collect_taxonomy()
    results = []
    for s in srcs:
        if job.should_stop():
            job.log("收到停止指令，结束列表采集")
            break
        results.append(lc.collect(
            s, max_pages=p.get("pages") or None,
            resume=not p.get("restart"),
            stop_after=p.get("stop_after") or 0,
            should_stop=job.should_stop))
    cutoff = lc.set_cutoff_from_global()
    if cutoff:
        lc.apply_cutoff(cutoff)
    tids = [r[0] for r in db.conn.execute(
        "SELECT tid FROM threads WHERE COALESCE(detail_state,'pending')='pending'")]
    q, sk = db.enqueue_details(tids, cutoff)
    job.log(f"[queue] 新入队 {q} 条，水位线跳过 {sk} 条")
    return {"sources": [r.get("source") for r in results], "results": results,
            "cutoff": cutoff, "enqueued": q, "skipped": sk}


def _job_detail(cfg: Config, job: Job) -> Dict[str, Any]:
    p = job.params
    db = get_db(cfg)
    client = _JobClient(cfg, job)
    if p.get("retry_failed"):
        n = db.conn.execute(
            "UPDATE detail_queue SET state='pending' WHERE state='failed'").rowcount
        db.conn.commit()
        job.log(f"已重置 {n} 条失败任务为待采")

    dc = DetailCollector(db, client, cfg)

    # ---- 目标集合：直接给的 tid  ∪  按部门筛出来的 tid ----
    tids = [int(t) for t in (p.get("tids") or [])]
    dept_ids = [int(t) for t in (p.get("dept_ids") or [])]
    if dept_ids:
        picked = _tids_for_depts(db, dept_ids)
        job.log(f"[detail] 按部门（{len(dept_ids)} 个）筛出待采详情 {len(picked)} 帖"
                f"（先旧后新）")
        known = set(tids)
        tids.extend(t for t in picked if t not in known)

    if tids:
        # ★ 并发采集（2026-10-25）：并发单位是**一帖**，
        #   每 worker 用自己的 client 与 DB 连接（见 DetailCollector.for_thread）。
        #   workers=1 时 run_parallel 走串行分支，行为与改造前一致。
        from .parallel import run_parallel

        # ★ 并发度取**服务端设置**（管理员可在网页上改），不是写死的。
        _w = _detail_workers()
        job.log(f"[detail] 并发采集启动：{_w} 路"
                + ("（串行）" if _w == 1 else f"（每路独立限速 {cfg.interval}s）"))

        def _one(tid: int) -> Dict[str, Any]:
            c = dc.for_thread()
            return c.collect_one(tid, should_stop=job.should_stop)

        results, errors = run_parallel(tids, _one,
                                       workers=_w,
                                       should_stop=job.should_stop)
        out = [r for r in results if isinstance(r, dict)]
        for tid, e in errors:
            job.log(f"tid={tid} ✗ {type(e).__name__}: {e}")
        for r in out:
            job.log(f"tid={r.get('tid')} {'✓' if r.get('ok') else '✗'} "
                    f"{r.get('pages')} 页 / {r.get('posts')} 楼")
        ok = sum(1 for r in out if r.get("ok"))
        return {"targets": out, "done": ok, "failed": len(out) - ok}

    res = dc.run(limit=p.get("limit") or 0,
                 max_attempts=p.get("max_attempts") or 3,
                 should_stop=job.should_stop,
                 workers=_detail_workers())
    return res


# ====================================================================== 请求体
def _reject_if_running(kind: str, node: str) -> None:
    """同节点同类任务已在跑 → 拒绝再起一个。

    ★ 互斥域是 **(任务类型, 采集节点)**，不是全局：
      · 同一节点起两个同类任务 → **重复请求同一批页**，白白翻倍请求量，
        还会让两个任务互相踩水位线（实测：两个 109 部门任务把 dept:726 的 23,000 行
        重采了一遍，且同时在采 dept:730）。必须挡住。
      · **不同节点之间允许并行** —— 这正是留给小组成员协作的 Beta 能力：
        管理员本机一个节点、每个成员一个节点，各采各的，靠 tid 幂等去重汇总。
    """
    for j in JOBS.list():
        if (j["kind"] == kind and j["state"] == "running"
                and (j.get("node") or "admin") == (node or "admin")):
            raise HTTPException(
                409,
                f"本节点（{node or 'admin'}）已有一个「{kind}」任务在跑（{j['id']}）。"
                f"再起一个会重复请求同一批页，请先停止它。"
                f"（不同节点的并行是允许的 —— 那留给小组成员协作。）")


class ListReq(BaseModel):
    #: 高级用法：直接写源描述（global / dept:495 / group:func / category:1 …）
    sources: str = ""
    #: 网页端「下拉多选 + 全选」选中的部门 typeid
    dept_ids: List[int] = []
    #: 是否同时采全站视图（global，500 页顶）
    use_global: bool = False
    pages: int = 0
    stop_after: int = 0
    restart: bool = False
    taxonomy: bool = False
    #: 采集节点标识。同节点的任务互斥；不同节点可并行（小组成员协作的 Beta 预留）
    node: str = "admin"


class DetailReq(BaseModel):
    limit: int = 0
    max_attempts: int = 3
    retry_failed: bool = False
    #: 直接指定 tid（调试 / 定点补采）
    tids: List[int] = []
    #: 按部门筛（**与列表采集走同一个源**）：
    #: 这些部门下尚未采详情的帖，按先旧后新入队
    dept_ids: List[int] = []
    #: 采集节点标识。同节点互斥；不同节点可并行（小组成员协作的 Beta 预留）
    node: str = "admin"


def _tids_for_depts(db: DB, dept_ids: List[int]) -> List[int]:
    """选出「属于这些部门、且详情还没采」的 tid，按先旧后新排序。

    部门的归属有两个来源，都要算：
      · `dept_id`        主部门
      · `extra_dept_ids` 多标签部门（逗号分隔）

    ⚠ `extra_dept_ids` 是逗号串，直接 `LIKE '%512%'` 会误命中 1512 这种。
    所以两边都归一到「逗号包起来的串」再用 `instr` 精确找：
        instr(',' || extra_dept_ids || ',', ',512,') > 0

    ★★ 2026-10-25 修：必须**同时排除 `restricted`**
      原来只排除了 `done` → 41.8 万「站方限制（正文不公开）」的帖
      被当成「还没采详情」全筛进来（实测筛出 584,587 帖）→
      这些帖**永远采不到**（站方只给提示页），却排在队首 →
      worker 一上来就撞上它们 → 白烧产能、界面看着像「如跑」。
      → 口径与 `/api/pending` 一致：`NOT IN ('done','restricted')`。
      （`stale` = 待重采，仍要入选 —— 它确实还要采。）
    """
    ids = [str(x) for x in (dept_ids or []) if str(x).strip()]
    if not ids:
        return []
    parts, params = [], []
    for i in ids:
        parts.append("dept_id = ?")
        params.append(i)
        parts.append("instr(',' || COALESCE(extra_dept_ids,'') || ',', ?) > 0")
        params.append(f",{i},")
    rows = db.conn.execute(
        "SELECT tid FROM threads "
        "WHERE COALESCE(detail_state,'pending') NOT IN ('done','restricted') "
        f"AND ({' OR '.join(parts)}) "
        "ORDER BY COALESCE(pub_ts,0) ASC, tid ASC", params).fetchall()
    return [r["tid"] for r in rows]


# ====================================================================== API
@app.get("/favicon.ico")
def favicon():
    """返回一个极简 SVG 图标 —— 免得浏览器控制台永远挂一条 favicon 404。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#17635e"/>'
        '<text x="16" y="24" font-size="20" font-family="Georgia,serif" '
        'font-style="italic" fill="#ffffff" text-anchor="middle">e</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/health")
def api_health(request: Request) -> Dict[str, Any]:
    cfg = _cfg()
    try:
        db = get_db(cfg)
        n = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        p = db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        fts = db.fts_available()
        err = None
    except Exception as e:                                # noqa: BLE001
        n = p = 0
        fts = False
        err = str(e)
    # ★ 公网只回「活着没有」；库路径、条数、版本号**只给本机**。
    #   db_path 是本机绝对路径（含用户名、盘符、项目名）—— 对外泄露等于
    #   直接告诉外网人「这是谁在跑、跑的是什么项目」。
    if not _is_local_request(request):
        return {"ok": err is None}
    return {"ok": err is None, "error": err, "db_path": cfg.db_path,
            "db_exists": os.path.exists(cfg.db_path), "threads": n, "posts": p,
            "fts": fts, "version": app.version}


@app.get("/api/stats")
def api_stats(request: Request) -> Dict[str, Any]:
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    st = db.stats()
    st["site_stats"] = db.latest_site_stats()
    st["watermarks"] = [dict(r) for r in db.conn.execute(
        "SELECT source,dim,code,name,last_page,max_page,rows_total,oldest_ts,"
        "newest_ts,status,updated_at FROM list_watermark ORDER BY source")]
    return st


@app.get("/api/meta")
def api_meta(request: Request) -> Dict[str, Any]:
    """筛选器要用的字典。"""
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    from . import pinyin as _py
    # ★ 部门/区县市：带上**拼音首字母**与**分组**（2026-10-24）
    #   「输入名称、拼音或首字母」—— 站方那个「107家部门与企事业单位在线」
    #   的搜索框就是这个能力，109 个部门用原生下拉框根本没法找。
    depts = []
    for r in db.conn.execute(
            "SELECT typeid,name,group_code,group_name,is_district,py_initials "
            "FROM depts ORDER BY group_name, name"):
        d = dict(r)
        d["py"] = d.get("py_initials") or _py.initials(d.get("name") or "")
        d["group"] = d.get("group_name") or ""
        depts.append(d)
    districts = []
    for r in db.conn.execute(
            "SELECT typeid,name,total_msgs,last_month_msgs,py_initials "
            "FROM districts ORDER BY name"):
        d = dict(r)
        d["py"] = d.get("py_initials") or _py.initials(d.get("name") or "")
        d["group"] = "district"
        districts.append(d)
    return {
        "categories": _py.dict_items(CATEGORIES),
        "states": _py.dict_items(STATES),
        "mailboxes": _py.dict_items(MAILBOXES),
        "lights": _py.dict_items(LIGHTS),
        "depts": depts,
        "districts": districts,
        "groups": [
            # 用站方 tab 的原名，不要自造简称 —— 报告与界面口径一致
            {"code": "func", "name": "职能部门和群团组织", "short": "职能",
             "py": "BMDWQTTZZ"},
            {"code": "district", "name": "区（县、市）及开发园区", "short": "区县市",
             "py": "QXSJSJKFQQ"},
            {"code": "enterprise", "name": "民生企事业单位", "short": "民生企事业",
             "py": "MSQYDW"},
            {"code": "other", "name": "其他", "short": "其他", "py": "QT"},
        ],
    }


@app.get("/api/threads")
def api_threads(
    request: Request,
    q: str = Query("", description="关键词，空=不过滤"),
    scope: str = Query("all", pattern="^(thread|post|all)$"),
    dept: str = "", group: str = "", district: str = "",
    category: str = "", state: str = "", mailbox: str = "", light: str = "",
    date_from: str = "", date_to: str = "",
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = "time_desc",
) -> Dict[str, Any]:
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    return db.search(q, scope=scope, dept=dept, group=group, district=district,
                     category=category, state=state, mailbox=mailbox, light=light,
                     date_from=date_from, date_to=date_to, limit=limit,
                     offset=offset, sort=sort)


@app.get("/api/threads/{tid}")
def api_thread(tid: int, request: Request) -> Dict[str, Any]:
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    row = db.conn.execute(
        "SELECT * FROM threads WHERE tid=?", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"帖子 {tid} 不在库里")
    thread = dict(row)
    posts = [dict(r) for r in db.conn.execute(
        "SELECT pid,page_no,floor,author,post_time,chars,content,images "
        "FROM posts WHERE tid=? ORDER BY page_no, floor, pid", (tid,))]
    flows = [dict(r) for r in db.conn.execute(
        "SELECT step,name,time FROM flows WHERE tid=? ORDER BY step", (tid,))]
    pages = [dict(r) for r in db.conn.execute(
        "SELECT page_no,pids,n,status,fetched_at FROM thread_pages "
        "WHERE tid=? ORDER BY page_no", (tid,))]
    queue = db.conn.execute(
        "SELECT * FROM detail_queue WHERE tid=?", (tid,)).fetchone()
    return {"thread": thread, "posts": posts, "flows": flows, "pages": pages,
            "queue": dict(queue) if queue else None}


@app.get("/api/pending")
def api_pending(request: Request,
                dept_ids: str = Query("", description="逗号分隔的部门 typeid；空=不限")) -> Dict[str, Any]:
    """按部门统计「详情还没采」的帖数 —— 给网页端选完部门后显示真实工作量。"""
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    ids = [int(x) for x in (dept_ids or "").split(",") if x.strip().isdigit()]
    tids = _tids_for_depts(db, ids)
    total_pending = db.conn.execute(
        "SELECT COUNT(*) FROM threads "
        "WHERE COALESCE(detail_state,'pending') NOT IN ('done','restricted')"
    ).fetchone()[0]
    return {"dept_ids": ids, "pending": len(tids), "total_pending": total_pending,
            "sample": tids[:20]}


@app.get("/api/pending-by-dept")
def api_pending_by_dept(request: Request) -> Dict[str, Any]:
    """**每个部门**还欠多少详情（详情采集的部门选择器要用）。

    ★ 为什么要单独一个接口（2026-10-25 用户反馈）
      详情采集的选择器原来在部门名后面挂的是**部门编号**（typeid，如 533、728），
      看着像「已采页数」，而选部门的意义其实是「这个部门还欠多少正文」。
      → 改成显示**待采详情数**，一眼能看出哪些部门是大头。

    ★ 为什么一次返回全部、而不是每个部门发一次请求
      109 个部门发 109 次 → 接口刷屏、页面卡。
      → 一次查询 + 内存归组。

    ★ 口径与 `/api/pending` 一致：`NOT IN ('done','restricted')`
      （restricted = 站方不公开正文，永远采不到，不该算进工作量）
    """
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    rows = db.conn.execute(
        "SELECT dept_id, extra_dept_ids FROM threads "
        "WHERE COALESCE(detail_state,'pending') NOT IN ('done','restricted')"
    ).fetchall()
    counts: Dict[int, int] = {}
    for r in rows:
        did = r["dept_id"]
        if did:
            try:
                counts[int(did)] = counts.get(int(did), 0) + 1
            except (TypeError, ValueError):
                pass
        # 多标签部门：一个帖可能同时属于多个部门
        for extra in (r["extra_dept_ids"] or "").split(","):
            extra = extra.strip()
            if not extra.isdigit():
                continue
            e = int(extra)
            counts[e] = counts.get(e, 0) + 1
    return {"counts": counts, "total": len(rows)}


@app.get("/api/list-changes")
def api_list_changes(request: Request,
                     limit: int = Query(200, ge=1, le=1000),
                     kind: str = "", source: str = "") -> Dict[str, Any]:
    """**列表阶段的变更留痕**（2026-10-23 加）。

    列表采集是「upsert 覆盖」——旧值一被盖掉就永远查不到了。
    这张表把「覆盖前那一瞬间」存下来，于是可以回答：
    「这帖站方是什么时候受理的？标题什么时候改的？这一趟多出了哪些新帖？」

    记四类：`new` 新帖 / `state` 受理状态变了 / `replies` 回复数变了 / `title` 标题改了。
    """
    _require_member(request)
    db = get_db(_cfg())
    items = db.list_changes(limit=limit, kind=kind, source=source)
    counts = {r["kind"]: r["n"] for r in db.conn.execute(
        "SELECT kind, COUNT(*) n FROM list_changes GROUP BY kind")}
    return {"items": items, "counts": counts}


@app.get("/api/integrity")
def api_integrity(request: Request) -> Dict[str, Any]:
    """完整性体检 —— **管理员专属**（同步版，给脚本/测试用）。

    成员看不到这一页：它是「我们对全库数据是否完整」的自查，
    属于质量管控视角；成员只需要知道自己采的那部分有没有成。

    返回四类：
      problems  硬问题（缺头/缺尾/页数不足/状态不对）
      warnings  软警告（站方计数不自洽等）
      growth    **疑似有增量**（站方又回复了，本地正文过时 → 可重跑）
      stale     已被标记为「待重采」的帖

    ★ 网页界面**不用这个接口**（2026-10-25 用户反馈「容易卡」）：
      它会同步扫全库（60 万帖量级），页面请求会一直挂着。
      界面走 `POST /api/integrity/run`（后台任务）+ 轮询，页面不挂死。
    """
    require_admin(request)
    cfg = _cfg()
    db = get_db(cfg)
    dc = DetailCollector(db, Client(cfg, verbose=False), cfg)
    return dc.integrity()


@app.post("/api/integrity/run")
def api_integrity_run(request: Request) -> Dict[str, Any]:
    """**后台**跑一次完整性体检（界面用这个）。

    ★ 为什么要后台（2026-10-25 用户反馈「体检界面打开就卡」）
      体检要扫全库（60 万帖量级），同步跑会让浏览器的请求一直挂着，
      页面看着像死了。改成后台任务后：
        · 页面立刻返回任务号，界面显示「体检中…」
        · 前端轮询任务状态，跑完再渲染结果
        · 期间页面其它功能照常用
    """
    require_admin(request)
    # 同类任务互斥：体检很重，不许同时跑两个
    for j in JOBS.list():
        if j.get("kind") == "integrity" and j.get("state") == "running":
            raise HTTPException(409, "已有一个体检任务在跑，请等它跑完")
    cfg = _cfg()
    job = JOBS.create("integrity", {}, node="admin")
    job.log("开始体检：扫描全库详情帖…（全库量级，耗时较长）")

    def _run(j: Job) -> Dict[str, Any]:
        db = get_db(cfg)
        dc = DetailCollector(db, Client(cfg, verbose=False), cfg)
        j.log("扫描中…")
        r = dc.integrity()
        j.log(f"体检完成：检查 {r.get('checked')} 帖，"
              f"问题 {len(r.get('problems') or [])} 条，"
              f"增量 {len(r.get('growth') or [])} 条")
        return r

    JOBS.spawn(job, _run)
    return {"job": job.to_dict(tail=0)}


@app.post("/api/integrity/retry")
def api_integrity_retry(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**重跑**：把选中的帖丢回队列，重新采一遍（软失败 —— 数据留着）。

    与「清除」的区别：
      · 重跑 = 数据保留，重新采一遍；采到的新楼层按合并规则进来
      · 清除 = 数据彻底删掉，从零开始
    """
    require_admin(request)
    tids = [int(x) for x in (req.get("tids") or []) if str(x).strip().isdigit()]
    if not tids:
        raise HTTPException(400, "没选帖子")
    db = get_db(_cfg())
    res = db.retry_details(tids, reason=str(req.get("reason") or "体检页人工重跑"))
    HUB.info("admin", f"重跑 {res['reset']} 帖（丢回队列重采）", task="")
    return {"ok": True, "action": "retry", **res, "tids": tids}


@app.post("/api/integrity/purge")
def api_integrity_purge(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**自行清除**：把选中的帖的详情彻底清空（楼层 / 分页凭据 / 受理流程 / 队列项）。

    破坏性操作 —— 界面必须二次确认，并把「删了多少楼层、多少字」说清楚。
    清空后下一次采集是从零开始，不会拿旧内容凑数。
    """
    require_admin(request)
    tids = [int(x) for x in (req.get("tids") or []) if str(x).strip().isdigit()]
    if not tids:
        raise HTTPException(400, "没选帖子")
    db = get_db(_cfg())
    res = db.purge_details(tids)
    HUB.info("admin",
             f"清除 {res['purged']} 帖（删了 {res['posts']} 个楼层、"
             f"{res['pages']} 页凭据、{res['flows']} 条流程）", task="")
    return {"ok": True, "action": "purge", **res, "tids": tids}


@app.get("/api/fetchlog")
def api_fetchlog(request: Request, limit: int = Query(200, ge=1, le=2000),
                 phase: str = "") -> Dict[str, Any]:
    _require_member(request)
    cfg = _cfg()
    db = get_db(cfg)
    if phase:
        rows = db.conn.execute(
            "SELECT * FROM fetch_log WHERE phase=? ORDER BY id DESC LIMIT ?",
            (phase, limit)).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/agent/login")
def api_agent_login(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """成员 agent 的登录（`/api/auth/login` 的别名）。

    ★ 为什么要有这一组 `/api/agent/*` 别名：
      成员机器上的 agent **没有浏览器**，也就没有 Cloudflare Access 的会话 Cookie
      —— 直接打 `/api/auth/login` 会被 Access 挡下，返回的是**登录页 HTML**。
      于是把 agent 真正要用的接口**集中到 `/api/agent/*` 一个前缀下**，
      Access 只需要为这一个前缀放行（bypass），其余路径照旧受 Access 保护。
    """
    body = dict(req)
    body["email"] = str(body.get("email") or "").strip().lower()
    body["password"] = str(body.get("password") or "")
    # 限速逻辑与网页登录**共用同一套**（本机豁免 + 双闸）—— 别两处各写一份。
    return _login_with_rate(request, body)


@app.get("/api/agent/me")
def api_agent_me(request: Request) -> Dict[str, Any]:
    """agent 的「我是谁」（`/api/auth/me` 的别名）。"""
    return api_me(request)


def _current_endpoints() -> tuple[List[str], str]:
    """当前可用的公网入口列表 + 更新时间。

    ★ 数据来自隧道守护写的 `data/agent-endpoints.json`
      （`程序/tunnel.ps1` 每次隧道地址变了就重写它）。
    """
    p = Path(__file__).resolve().parent.parent / "data" / "agent-endpoints.json"
    eps: List[str] = []
    updated = ""
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            for u in (d.get("endpoints") or []):
                u = str(u).strip().rstrip("/")
                if u and u not in eps:
                    eps.append(u)
            updated = str(d.get("updated_at") or "")
    except Exception:                                        # noqa: BLE001
        eps = []
    return eps, updated


@app.get("/api/agent/endpoints")
def api_agent_endpoints() -> Dict[str, Any]:
    """**当前可用的控制面地址列表**（隧道换地址后让 agent 自己捞回来）。

    ★ 为什么开放这个（2026-10-26 加）：
      公网入口是**隧道地址**，隧道一重启地址就变
      （quick tunnel 每次给一个随机的 `*.trycloudflare.com`）。
      只要还剩一条隧道活着，agent 就能从它这里拿到新地址列表、
      自己接上 —— 组员不必挨个通知重下启动包。

    ★ 只暴露**地址**，不含任何凭证（token/口令都不在这里）。
    ★ 守护进程没在跑时返回空列表（agent 会退回文件里的候选）。
    """
    eps, updated = _current_endpoints()
    return {"endpoints": eps, "updated_at": updated, "count": len(eps)}


@app.post("/api/agent/pick")
def api_agent_pick(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """agent 领活（`/api/assist/pick` 的别名）。"""
    return _retry_locked(api_assist_pick, request, req)


@app.post("/api/agent/submit")
def api_agent_submit(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """agent 回传（`/api/assist/submit` 的别名）。"""
    return api_assist_submit(request, req)


@app.post("/api/agent/return")
def api_agent_return(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """agent 退活（`/api/assist/return` 的别名）。

    ★ agent 退出前会调它 —— 把手里没做完的活还给池子，
      别的成员马上能领到（实测踩过：关窗口后活卡在 taken 30 分钟）。
    """
    return api_assist_return(request, req)


#: 登录失败限流（按 IP，内存计数）。公网上开着登录口就必须有这道闸。
#: ★ 阈值为什么放大到 20 次 / 10 分钟：
#:   管理员与组员**同校园网** → 出口 IP 相同 → 一个人输错密码多了，
#:   阈值太小会**把全组一起锁**（自己人反被 DoS）。
#:   只统计**密码错误**，正常成员不会触发。
_LOGIN_FAIL: Dict[str, List[float]] = {}
_LOGIN_FAIL_MAX = 20         # 10 分钟内失败 20 次就锁
_LOGIN_FAIL_WINDOW = 600.0

#: 第二个闸：按「IP + 邮箱」—— 防**针对某个成员**被专门撞
#:   （共享出口 IP 的场景下，这道闸比 IP 总闸更准）。
_ACCT_FAIL: Dict[str, List[float]] = {}
_ACCT_FAIL_MAX = 5           # 10 分钟内同一 (IP,邮箱) 失败 5 次就锁
_ACCT_FAIL_WINDOW = 600.0


def _login_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_FAIL.get(ip, []) if now - t < _LOGIN_FAIL_WINDOW]
    _LOGIN_FAIL[ip] = hits
    return len(hits) < _LOGIN_FAIL_MAX


def _login_fail(ip: str) -> None:
    now = time.time()
    hits = [t for t in _LOGIN_FAIL.get(ip, []) if now - t < _LOGIN_FAIL_WINDOW]
    hits.append(now)
    _LOGIN_FAIL[ip] = hits


def _login_ok(ip: str) -> None:
    _LOGIN_FAIL.pop(ip, None)


def _acct_rate_ok(acct: str) -> bool:
    now = time.time()
    hits = [t for t in _ACCT_FAIL.get(acct, []) if now - t < _ACCT_FAIL_WINDOW]
    _ACCT_FAIL[acct] = hits
    return len(hits) < _ACCT_FAIL_MAX


def _acct_fail(acct: str) -> None:
    now = time.time()
    hits = [t for t in _ACCT_FAIL.get(acct, []) if now - t < _ACCT_FAIL_WINDOW]
    hits.append(now)
    _ACCT_FAIL[acct] = hits


def _acct_ok(acct: str) -> None:
    _ACCT_FAIL.pop(acct, None)


#: 注册限流（按 IP，内存计数）。公网上的陌生人能无限注册 pending 账号，
#: 把 members 表灌垃圾 —— 登不进来，但会挤占管理界面、干扰审核。
_REGISTER_HITS: Dict[str, List[float]] = {}
_REGISTER_MAX = 5             # 1 小时内 5 次
_REGISTER_WINDOW = 3600.0


def _register_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _REGISTER_HITS.get(ip, []) if now - t < _REGISTER_WINDOW]
    _REGISTER_HITS[ip] = hits
    return len(hits) < _REGISTER_MAX


def _register_hit(ip: str) -> None:
    now = time.time()
    hits = [t for t in _REGISTER_HITS.get(ip, []) if now - t < _REGISTER_WINDOW]
    hits.append(now)
    _REGISTER_HITS[ip] = hits


@app.get("/api/jobs")
def api_jobs(request: Request) -> Dict[str, Any]:
    """任务列表。

    ★ 管理员看全部；**成员只看自己节点的任务** ——
      成员的界面是给他干活的，不是给他看别人在干什么的。
    """
    m = require_auth(request)
    items = JOBS.list()
    if (m.get("role") or "") != "admin":
        mine = m.get("node_id") or ""
        items = [j for j in items if (j.get("node") or "admin") == mine]
    return {"items": items}


@app.get("/api/jobs/{jid}")
def api_job(request: Request, jid: str, tail: int = Query(300, ge=0, le=1000)) -> Dict[str, Any]:
    m = require_auth(request)
    job = JOBS.get(jid)
    if (m.get("role") or "") != "admin" and (job.node or "admin") != (m.get("node_id") or ""):
        raise HTTPException(403, "只能看自己节点的任务")
    d = job.to_dict(tail=tail)
    # 附带实时进度快照（从库里算，比解析日志可靠）
    try:
        cfg = _cfg()
        db = get_db(cfg)
        d["progress"] = {
            "threads": db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0],
            "posts": db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "queue": {r["state"]: r["n"] for r in db.conn.execute(
                "SELECT state, COUNT(*) n FROM detail_queue GROUP BY state")},
        }
    except Exception:                                      # noqa: BLE001
        d["progress"] = None
    return d


#: ★ 成员点了「开始采集」时给的提示（2026-10-23）
#:   原来这三个端点用的是 require_auth → 成员能在**管理员的机器**上启动采集，
#:   实测：成员点一次，dept:555 在**管理员的 IP** 上从 58 页刷到 500 页。
#:   这既白耗管理员的机器/带宽，更把「站方看到的是多个分散的普通用户」
#:   这个 Beta 的**核心前提**给架空了。
#:   所以服务器端采集一律**管理员专属**；成员要走本机 agent 采集。
_MEMBER_NO_SERVER_JOBS = (
    "服务器端采集只对管理员开放。采集必须发生在你自己的电脑上"
    "（站方不提供 CORS，浏览器不能直接采）——"
    "请用「协作采集」里的本机 agent：点「下载启动包」→ 解压 → 双击「启动助手.cmd」"
    "→ 回网页点「在本机采集」。")


def _require_admin_jobs(request: Request) -> Dict[str, Any]:
    """服务器端采集任务：**管理员专属**。"""
    m = require_auth(request)
    if (m.get("role") or "") != "admin":
        raise HTTPException(403, _MEMBER_NO_SERVER_JOBS)
    return m


@app.get("/api/agent/watermarks")
def api_agent_watermarks(request: Request) -> Dict[str, Any]:
    """给 agent 的**各源水位线**（本机续采时要知道从第几页接着采）。

    ★ 为什么要单独开这个端点
      agent 走的是 `agentapi.…` 那个**专用子域名**，那边**只开放 `/api/agent/*`**
      （其余路径一律 403，那是刻意的收紧）。所以它拿不到网页用的 `/api/stats`。
      没有水位线的话，成员本机采列表就得**从第 1 页重来** →
      把管理员已经采过的页又请求一遍（白耗站方，也白费成员的时间）。

    ★ 这里只给**读**，且**不含**任何敏感信息（就是几个页码与行数）。
    """
    require_auth(request)
    db = get_db(_cfg())
    out = {}
    for r in db.conn.execute(
            "SELECT source,last_page,status,rows_total,max_page FROM list_watermark"):
        out[str(r["source"])] = {
            "last_page": int(r["last_page"] or 0),
            "status": str(r["status"] or ""),
            "rows_total": int(r["rows_total"] or 0),
            "max_page": int(r["max_page"] or 0),
        }
    return {"watermarks": out}


@app.post("/api/agent/ingest-list")
def api_agent_ingest_list(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**成员本机 agent 回传列表页元数据**（他们在自己电脑上采的那部分）。

    ★ 为什么要有这个
      站方**不提供 CORS**，浏览器不能直接采站方 → 成员的采集只能跑在自己机器上；
      而 Beta 的初衷是「站方看到的是多个分散的普通用户，不是一个服务器在集中爬」。
      所以成员的列表采集必须走本机 agent 采完再回传，
      而不是让成员点一下按钮、用**管理员的机器**去刷站方。

    ★ 为什么**不推水位线**
      水位线是「这个源采到哪了」的**权威续采状态**。
      若多台机器采同一个源、各自推水位线，管理员的续采会从被推高的位置开始
      → **中间的页被整个跳过**（漏采，而且是静默的）。
      所以这里**只入元数据**（幂等 upsert），水位线一个字都不动。

    ★ 入库口径与管理员本地采集**完全同一条路径**（`upsert_threads` + `add_source`
      + `set_tags`），所以成员采到的元数据和管理员采的没有区别。
    """
    m = require_auth(request)
    from .parsers import ListRow
    fields = set(ListRow.__dataclass_fields__.keys())
    src = str(req.get("source") or "")[:60]
    rows = []
    for r in (req.get("rows") or []):
        if not isinstance(r, dict):
            continue
        kw = {k: v for k, v in r.items() if k in fields}
        if not kw.get("tid"):
            continue
        for k in ("extra_dept_ids", "extra_dept_names"):
            v = kw.get(k)
            if isinstance(v, str):
                kw[k] = [x for x in v.split(",") if x]
            elif v is None:
                kw[k] = []
        try:
            rows.append(ListRow(**kw))
        except Exception:                             # noqa: BLE001
            continue
    if not rows:
        return {"ok": True, "ingested": 0, "source": src}
    db = get_db(_cfg())
    n = db.upsert_threads(rows)
    # 来源与标签（agent 那边算好了一并回传）
    tags_by_tid: Dict[int, List] = {}
    for item in (req.get("tags") or []):
        try:
            tid, dim, code, name = (list(item) + ["", "", ""])[:4]
            tags_by_tid.setdefault(int(tid), []).append((str(dim), str(code), str(name)))
        except Exception:                             # noqa: BLE001
            continue
    for r in rows:
        if src:
            db.add_source(r.tid, src)
        tg = tags_by_tid.get(r.tid)
        if tg:
            db.set_tags(r.tid, tg)
    db.backfill_dept_meta()
    node = str(m.get("node_id") or "")
    HUB.info(node, f"回传列表元数据 {n} 帖（源 {src or '未标注'}）", task="")
    return {"ok": True, "ingested": n, "source": src, "node": node}


@app.post("/api/jobs/taxonomy")
def api_job_taxonomy(request: Request) -> Dict[str, Any]:
    m = _require_admin_jobs(request)
    cfg = _cfg()
    node = _job_node(m)
    _reject_if_running("taxonomy", node)
    job = JOBS.create("taxonomy", {}, node=node)
    JOBS.spawn(job, lambda j: _job_taxonomy(cfg, j))
    return {"job": job.to_dict(tail=0)}


@app.post("/api/jobs/list")
def api_job_list(request: Request, req: ListReq = Body(...)) -> Dict[str, Any]:
    m = _require_admin_jobs(request)
    cfg = _cfg()
    node = _job_node(m, req.node)
    req.node = node
    _reject_if_running("list", node)
    job = JOBS.create("list", req.model_dump(), node=node)
    JOBS.spawn(job, lambda j: _job_list(cfg, j))
    return {"job": job.to_dict(tail=0)}


@app.post("/api/jobs/detail")
def api_job_detail(request: Request, req: DetailReq = Body(...)) -> Dict[str, Any]:
    m = _require_admin_jobs(request)
    cfg = _cfg()
    node = _job_node(m, req.node)
    req.node = node
    _reject_if_running("detail", node)
    job = JOBS.create("detail", req.model_dump(), node=node)
    JOBS.spawn(job, lambda j: _job_detail(cfg, j))
    return {"job": job.to_dict(tail=0)}


@app.post("/api/jobs/{jid}/stop")
def api_job_stop(request: Request, jid: str) -> Dict[str, Any]:
    m = require_auth(request)
    job = JOBS.get(jid)
    if (m.get("role") or "") != "admin" and (job.node or "admin") != (m.get("node_id") or ""):
        raise HTTPException(403, "只能停止自己节点的任务")
    job.stop_requested = True
    job.log("已请求停止（将在下一个页/帖的边界停下）")
    return {"job": job.to_dict(tail=0)}


@app.delete("/api/jobs/{jid}")
def api_job_delete(request: Request, jid: str) -> Dict[str, Any]:
    """**删掉一个已结束的任务**（连同它的日志）。

    ★ 管理员专属：这是**管理动作**（清界面噪音），不是成员自己的数据。
    ★ 只删**任务运行记录**，**不碰采集结果** ——
      库里的帖/正文/队列一行都不动（那是权威数据，删不得）。
    ★ 运行中的任务不给删 —— 先「停止」再删。
    """
    require_admin(request)
    if not JOBS.remove(jid):
        raise HTTPException(404, f"任务 {jid} 不存在")
    return {"ok": True, "id": jid}


@app.post("/api/jobs/clear")
def api_jobs_clear(request: Request) -> Dict[str, Any]:
    """**一键清理所有已结束的任务**（管理员专属）。

    ★ 回归测试每跑一轮就留下一堆已完成任务 → 界面被刷成噪音。
      这个接口把它们一次性收掉；**运行中的任务保留**。
    """
    require_admin(request)
    n = JOBS.clear_finished()
    return {"ok": True, "removed": n}


@app.post("/api/export")
def api_export(request: Request,
               out: str = Body(default="threads.csv", embed=True)) -> Dict[str, Any]:
    m = require_auth(request)
    cfg = _cfg()
    db = get_db(cfg)
    os.makedirs(cfg.export_dir, exist_ok=True)
    safe = os.path.basename(out or "threads.csv") or "threads.csv"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    path = os.path.join(cfg.export_dir, safe)
    rows = db.conn.execute(
        "SELECT t.tid,t.title,t.url,t.pub_time,t.pub_day,t.dept_name,t.dept_group,"
        "t.dept_district,t.category,t.state,t.mailbox_id,t.light_code,t.light_name,"
        "t.author,t.replies,t.views,t.detail_state,t.posts_n,t.chars,"
        "t.first_post_id,t.last_post_id,t.pages_total,"
        "(SELECT group_concat(content,'\\n') FROM posts p WHERE p.tid=t.tid) AS full_text "
        "FROM threads t ORDER BY t.pub_ts ASC").fetchall()
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if rows:
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow(list(r))
    HUB.info(m.get("node_id") or "admin",
             f"{m.get('email')} 导出 {len(rows)} 条 → {os.path.basename(path)}", task="")
    return {"ok": True, "path": path, "rows": len(rows),
            "by": m.get("email"), "node": m.get("node_id")}


# ====================================================================== 认证 / 协作
HUB = Hub()


def _secret(cfg: Config) -> str:
    """JWT 密钥：存库（meta 表），首次启动生成，之后重启不失效。"""
    db = get_db(cfg)
    s = db.get_meta("jwt_secret")
    if not s:
        s = new_secret()
        db.set_meta("jwt_secret", s)
    return s


def ensure_admin(cfg: Config) -> str:
    """首次启动时建管理员账号。返回 JWT 密钥。"""
    db = get_db(cfg)
    sec = _secret(cfg)
    admin_email = str((cfg.extra or {}).get("admin_email")
                      or "admin@example.com").strip().lower()
    with db.lock:
        exists = db.conn.execute("SELECT 1 FROM members WHERE email=?",
                                 (admin_email,)).fetchone()
        if not exists:
            pw = os.environ.get("MSYT_ADMIN_PW") or ("msyt-" + new_secret()[:12])
            db.conn.execute(
                "INSERT INTO members(email,pw_hash,role,node_id,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (admin_email, hash_pw(pw), "admin", "admin", "active",
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            db.conn.commit()
            # 账号密码落到本地文件，忘了能在机器上找到（**不要提交到仓库**）
            try:
                p = Path(cfg.db_path).parent / "admin-账号.txt"
                p.write_text(
                    f"管理员邮箱 : {admin_email}\n初始密码   : {pw}\n"
                    f"（登录后可在「协作」页改密码；这个文件可随时删）\n",
                    encoding="utf-8")
            except Exception:
                pass
            print("=" * 66)
            print(f"[auth] 已创建管理员账号：{admin_email}")
            print(f"[auth] 初始密码：{pw}")
            print(f"[auth] 账号密码也写到了：{Path(cfg.db_path).parent / 'admin-账号.txt'}")
            print("=" * 66)
    return sec


def _member_by_email(db: DB, email: str) -> Optional[Dict[str, Any]]:
    r = db.conn.execute("SELECT * FROM members WHERE email=?", (email.strip().lower(),)).fetchone()
    return dict(r) if r else None


def require_auth(request: Request, query_token: str = "") -> Dict[str, Any]:
    """从 Authorization 头解出成员信息。

    `query_token`：给 **EventSource** 用的后门 ——
    EventSource **不能带自定义请求头**，所以 SSE 只能把 token 放在查询串里。
    只在流式端点开这个口子，其他端点仍只认 Authorization 头。
    """
    cfg = _cfg()
    tok = ""
    h = request.headers.get("Authorization") or ""
    if h.startswith("Bearer "):
        tok = h[7:]
    elif query_token:
        tok = query_token
    if not tok:
        raise HTTPException(401, "请先登录")
    d = read_token(tok, _secret(cfg))
    if not d:
        raise HTTPException(401, "登录已过期，请重新登录")
    db = get_db(cfg)
    m = _member_by_email(db, d.get("email", ""))
    if not m or (m.get("status") or "active") != "active":
        raise HTTPException(403, "账号不可用")
    m["node_id"] = m.get("node_id") or d.get("node") or "member:" + m["email"]
    return m


def require_admin(request: Request) -> Dict[str, Any]:
    m = require_auth(request)
    if (m.get("role") or "") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return m


def _require_member(request: Request) -> Dict[str, Any]:
    """只要登录即可（管理员与成员都行）。

    「成员权限」= 能看数据、能用采集与协作界面；
    不等于「管理员权限」—— 成员名单、完整性体检仍是管理员专属。
    """
    return require_auth(request)


def _job_node(m: Dict[str, Any], requested: str = "") -> str:
    """决定这次采集任务挂在哪个节点上。

    ★ 成员**不能自报 node**：否则可以冒充 admin 节点，
      绕开「同节点同类任务互斥」这道防重复的闸门
      （两个任务重采同一批页、互相踩水位线，实测踩过）。
    管理员保留自报能力（调试用），默认仍落在 admin 节点。
    """
    if (m.get("role") or "") == "admin":
        return requested or "admin"
    return m.get("node_id") or ("member:" + str(m.get("email") or ""))


@app.post("/api/auth/login")
def api_login(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**浏览器登录**（带失败限速 + 防枚举）。

    ★ 为什么要限速：公网入口开着，登录口就是**撞库口**。
      之前只有 `/api/agent/login` 那个别名限速，**主登录口没限** ——
      外网人可以拿一串邮箱密码无限试。
    ★ 为什么要模糊文案：`email_allowed` 原本会区分
      「不在名单 / 待审核 / 已停用」→ 外网人拿邮箱一试就知道
      **哪些邮箱是成员**（邮箱枚举）。现在统一成「邮箱或密码不对」，
      只保留「待管理员审核」这一条（那是本人刚自助注册的人要看的）。
    """
    return _login_with_rate(request, req)


def _login_with_rate(request: Request, req: dict) -> Dict[str, Any]:
    """登录 + 失败限速（两道闸）。

    ★ **本机豁免**：`127.0.0.1` / `localhost` 直接放行。
      限速是为了防**公网撞库**；管理员本机（和本机上的测试/工具）
      不该被自己人的一次手滑锁住。
    ★ 两道闸：① 按 IP（防撞库）② 按「IP + 邮箱」（防针对单个成员）。
      阈值放大到 20 次 / 10 分钟，是因为**管理员与组员同校园网**、
      出口 IP 相同 —— 阈值太小会把自己人一起锁。
    """
    local = _is_local_request(request)
    ip = _client_ip(request)
    email = str(req.get("email") or "").strip().lower()
    acct = ip + "|" + email
    if not local:
        if not _login_rate_ok(ip):
            raise HTTPException(429, "登录失败次数过多，请 10 分钟后再试")
        if not _acct_rate_ok(acct):
            raise HTTPException(429, "该账号登录失败次数过多，请 10 分钟后再试")
    try:
        res = _login_core(req)
    except HTTPException:
        if not local:
            _login_fail(ip)
            _acct_fail(acct)
        time.sleep(0.25)          # 慢一点，别让密码被高速枚举
        raise
    if not local:
        _login_ok(ip)
        _acct_ok(acct)
    return res


def _login_core(req: dict) -> Dict[str, Any]:
    cfg = _cfg()
    db = get_db(cfg)
    email = str(req.get("email") or "").strip().lower()
    pw = str(req.get("password") or "")
    members = {r["email"]: dict(r) for r in db.conn.execute("SELECT * FROM members")}
    allow_domains = str((cfg.extra or {}).get("allow_domains") or "")
    ok, why = email_allowed(email, members, allow_domains)
    if not ok:
        # 「待审核」保留原文（本人自助注册后要知道去等管理员），
        # 其余一律模糊 → 防**邮箱枚举**（外人试邮箱就能试出谁是成员）。
        if "待管理员审核" in why:
            raise HTTPException(401, why)
        raise HTTPException(401, "邮箱或密码不对")
    m = members[email]
    if not verify_pw(pw, m["pw_hash"]):
        raise HTTPException(401, "邮箱或密码不对")
    node = m.get("node_id") or ("admin" if m.get("role") == "admin" else "member:" + email)
    if not m.get("node_id"):
        with db.lock:
            db.conn.execute("UPDATE members SET node_id=? WHERE id=?", (node, m["id"]))
            db.conn.commit()
    m["node_id"] = node

    def _touch_login():
        # ★ 写之前先 commit 掉读事务（2026-10-23 修）：
        #   上面那句 `SELECT * FROM members` 已经开了**读事务**，
        #   后面的 UPDATE 属于「读事务升级为写」——
        #   SQLite **不走 busy handler**，别的进程正写库时立刻抛
        #   `database is locked` → 登录 500 → 谁都登不进去。
        #   实测：回归脚本与登录并发时，登录整片 500。
        with db.lock:
            db.conn.commit()          # ← 结束读事务
            db.conn.execute("UPDATE members SET last_login=? WHERE id=?",
                            (time.strftime("%Y-%m-%d %H:%M:%S"), m["id"]))
            db.conn.commit()

    # 撞锁就退避重试 —— 登录是命脉接口，不能因为并发写就挂。
    _retry_locked(_touch_login)
    token = make_token({"email": email, "role": m.get("role") or "member",
                        "node": node}, _secret(cfg))
    HUB.info(node, f"{email} 登录", task="")
    return {"token": token, "member": {"email": email, "role": m.get("role"),
                                       "node_id": node, "quota": m.get("quota"),
                                       "quota_unlimited": True}}


@app.get("/api/auth/me")
def api_me(request: Request) -> Dict[str, Any]:
    m = require_auth(request)
    return {"email": m["email"], "role": m.get("role"), "node_id": m.get("node_id"),
            "quota": m.get("quota"), "quota_unlimited": True}


@app.get("/api/auth/config")
def api_auth_config() -> Dict[str, Any]:
    """登录页要用的公开信息（不含任何密钥）。

    给出管理员邮箱 —— 否则管理员自己会试 `admin@example.com` 之类而登不进去。
    """
    cfg = _cfg()
    return {
        "admin_email": str((cfg.extra or {}).get("admin_email")
                           or "admin@example.com"),
        "allow_register": True,
        "auto_approve": bool((cfg.extra or {}).get("auto_approve_registration")),
        "allow_domains": str((cfg.extra or {}).get("allow_domains") or ""),
    }


@app.post("/api/auth/register")
def api_register(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**自助注册**：填邮箱 + 密码即可申请一个成员账号。

    注册后账号状态为 `pending`（待管理员审核），**管理员在「成员名单」里点「通过」才能登录**。
    这是有意为之：站点即将暴露到公网（Cloudflare 隧道），
    若注册即可用，任何拿到域名的人都能开始打站方，把配额和风控预算烧掉。

    想改成「注册即可用」，在 config.json 里设 `"auto_approve_registration": true`。

    ★ 公网加固（2026-10-26）：
      ① **注册限速**（同 IP 1 小时 5 次）—— 陌生人可以无限注册 pending
        账号，把 members 表灌满垃圾（虽然 pending 登不进来）。
      ② 「已注册」提示改模糊 → 防**邮箱枚举**（外人拿邮箱一试，
        就能确认哪些邮箱是成员）。
    """
    cfg = _cfg()
    db = get_db(cfg)
    ip = _client_ip(request)
    if not _register_rate_ok(ip):
        raise HTTPException(429, "注册太频繁，请稍后再试")
    email = str(req.get("email") or "").strip().lower()
    pw = str(req.get("password") or "")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "邮箱格式不对")
    if len(pw) < 6:
        raise HTTPException(400, "密码至少 6 位")
    allow_domains = str((cfg.extra or {}).get("allow_domains") or "")
    if allow_domains:
        doms = [d.strip().lower() for d in allow_domains.split(",") if d.strip()]
        if email.split("@", 1)[1] not in doms:
            raise HTTPException(403, f"该邮箱域名不在允许范围内（{allow_domains}）")
    with db.lock:
        if _member_by_email(db, email):
            # 模糊文案：不告诉外人「这个邮箱是成员」
            raise HTTPException(403, "该邮箱不可用或已注册")
        auto = bool((cfg.extra or {}).get("auto_approve_registration"))
        status = "active" if auto else "pending"
        node = f"member:{email}"
        db.conn.execute(
            "INSERT INTO members(email,pw_hash,role,node_id,status,quota,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (email, hash_pw(pw), "member", node, status,
             int((cfg.extra or {}).get("default_quota") or 50), "自助注册",
             time.strftime("%Y-%m-%d %H:%M:%S")))
        db.conn.commit()
    _register_hit(ip)
    HUB.info("admin", f"新注册 {email}（状态 {status}）", task="")
    return {"ok": True, "email": email, "status": status, "node_id": node,
            "message": ("注册成功，可以登录了" if status == "active"
                        else "注册成功，等待管理员审核通过后即可登录")}


@app.post("/api/auth/password")
def api_change_password(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**本人改密码**（要验旧密码）。

    ★ 这个接口是「管理员重置了别人的密码之后，对方能自己改回来」的兜底：
      重置只是给一个能登进去的临时口令，真正的密码必须由本人自己定。
      少了这一步，被重置的成员就只能一直用管理员知道的密码 —— 那等于没改。
    """
    m = require_auth(request)
    old = str(req.get("old_password") or "")
    new = str(req.get("new_password") or "")
    confirm = req.get("confirm_password")
    if len(new) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if confirm is not None and str(confirm) != new:
        raise HTTPException(400, "两次输入的新密码不一致")
    if new == old:
        raise HTTPException(400, "新密码不能和原密码一样")
    db = get_db(_cfg())
    if not verify_pw(old, m["pw_hash"]):
        raise HTTPException(400, "原密码不对")

    def _do():
        with db.lock:
            db.conn.execute("UPDATE members SET pw_hash=? WHERE id=?",
                            (hash_pw(new), m["id"]))
            db.conn.commit()

    # ★ 写操作加退避重试（2026-10-23）
    #   并发写撞车时 SQLite 会抛 `database is locked` → 500，
    #   而「改密码失败」对成员来说就是「我登不进去了」，
    #   属于最不该出错的一个接口。重试几次通常就好了。
    _retry_locked(_do)
    HUB.info(m.get("node_id"), f"{m['email']} 修改了密码", task="")
    return {"ok": True, "email": m["email"]}


@app.post("/api/members/{mid}/approve")
def api_member_approve(mid: int, request: Request) -> Dict[str, Any]:
    """管理员审核通过一个待注册账号。"""
    require_admin(request)
    db = get_db(_cfg())
    with db.lock:
        db.conn.execute("UPDATE members SET status='active' WHERE id=?", (mid,))
        db.conn.commit()
    r = db.conn.execute("SELECT email,status FROM members WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404, "成员不存在")
    HUB.info("admin", f"审核通过 {r['email']}", task="")
    return {"ok": True, "email": r["email"], "status": r["status"]}


@app.post("/api/members/{mid}/quota")
def api_member_quota(mid: int, request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    require_admin(request)
    db = get_db(_cfg())
    q = max(1, min(int(req.get("quota") or 50), 500))
    with db.lock:
        db.conn.execute("UPDATE members SET quota=? WHERE id=?", (q, mid))
        db.conn.commit()
    r = db.conn.execute("SELECT email FROM members WHERE id=?", (mid,)).fetchone()
    return {"ok": True, "email": r["email"] if r else "", "quota": q}


@app.post("/api/members/{mid}/reset-password")
def api_member_reset_pw(mid: int, request: Request) -> Dict[str, Any]:
    """管理员重置某成员的密码，返回新密码（只显示这一次）。

    ★ 重置只是给一个**能登进去的临时口令**，不是最终密码 ——
      成员登录后必须能自己改（见 /api/auth/password）。
      所以下面这句不是客套，是操作说明：**告诉对方重置后先自行改密码**。
    """
    require_admin(request)
    db = get_db(_cfg())
    pw = "msyt-" + new_secret()[:10]

    def _do():
        with db.lock:
            db.conn.execute("UPDATE members SET pw_hash=? WHERE id=?", (hash_pw(pw), mid))
            db.conn.commit()

    # ★ 同上：写操作加退避重试（并发写撞车 → 500 → 成员拿不到新密码）
    _retry_locked(_do)
    r = db.conn.execute("SELECT email FROM members WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404, "成员不存在")
    HUB.info("admin", f"重置了 {r['email']} 的密码", task="")
    return {"ok": True, "email": r["email"], "password": pw,
            "message": f"把 {pw} 告诉 {r['email']}，并提醒他登录后在右上角「改密码」"
                       f"自行改掉这个临时密码。"}


@app.delete("/api/members/{mid}")
def api_member_delete(mid: int, request: Request) -> Dict[str, Any]:
    """删除成员（连同其协作任务认领记录）。"""
    require_admin(request)
    db = get_db(_cfg())
    r = db.conn.execute("SELECT email FROM members WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404, "成员不存在")
    if (r["email"] or "").lower() == str((cfg_admin_email()) or "").lower():
        raise HTTPException(400, "不能删管理员自己")
    with db.lock:
        db.conn.execute("DELETE FROM members WHERE id=?", (mid,))
        db.conn.commit()
    HUB.info("admin", f"删除了成员 {r['email']}", task="")
    return {"ok": True, "email": r["email"]}


def cfg_admin_email() -> str:
    return str((_cfg().extra or {}).get("admin_email") or "admin@example.com")


@app.get("/api/members")
def api_members(request: Request) -> Dict[str, Any]:
    require_admin(request)
    db = get_db(_cfg())
    rows = [dict(r) for r in db.conn.execute(
        "SELECT id,email,role,node_id,status,quota,last_login,last_seen,created_at,note "
        "FROM members ORDER BY id")]
    # 在线判定：5 分钟内有心跳算在线
    now = time.time()
    for r in rows:
        ls = r.get("last_seen") or ""
        r["online"] = False
        if ls:
            try:
                t = time.mktime(time.strptime(ls, "%Y-%m-%d %H:%M:%S"))
                r["online"] = (now - t) < 300
            except Exception:
                r["online"] = False
        r["quota_unlimited"] = True      # Beta 期间不限配额
    return {"items": rows}


@app.post("/api/members")
def api_member_add(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    require_admin(request)
    cfg = _cfg()
    db = get_db(cfg)
    email = str(req.get("email") or "").strip().lower()
    pw = str(req.get("password") or "") or ("msyt-" + new_secret()[:10])
    if "@" not in email:
        raise HTTPException(400, "邮箱格式不对")
    with db.lock:
        if db.conn.execute("SELECT 1 FROM members WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "该邮箱已存在")
        db.conn.execute(
            "INSERT INTO members(email,pw_hash,role,node_id,status,quota,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (email, hash_pw(pw), req.get("role") or "member",
             req.get("node_id") or f"member:{email}", "active",
             int(req.get("quota") or 50), req.get("note") or "",
             time.strftime("%Y-%m-%d %H:%M:%S")))
        db.conn.commit()
    HUB.info("admin", f"新增成员 {email}", task="")
    return {"ok": True, "email": email, "password": pw}


@app.post("/api/members/{mid}/toggle")
def api_member_toggle(mid: int, request: Request) -> Dict[str, Any]:
    require_admin(request)
    db = get_db(_cfg())
    with db.lock:
        db.conn.execute("UPDATE members SET status=CASE status WHEN 'active' "
                        "THEN 'disabled' ELSE 'active' END WHERE id=?", (mid,))
        db.conn.commit()
    r = db.conn.execute("SELECT email,status FROM members WHERE id=?", (mid,)).fetchone()
    return {"ok": True, "email": r["email"], "status": r["status"]}


# ---------------------------------------------------------------- 协作任务
@app.post("/api/assist/task")
def api_assist_task(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    require_admin(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    try:
        t = svc.create(req.get("title") or "", [int(x) for x in (req.get("dept_ids") or [])],
                       int(req.get("total") or 50), "admin")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"task": t}


@app.get("/api/assist/tasks")
def api_assist_tasks(request: Request) -> Dict[str, Any]:
    """任务列表：管理员与成员都能看（成员要知道大家在干什么）。

    ★ 每条任务都带 **拆开的进度**：
      `done`（已完成）/ `inflight`（已领未回传）/ `pending`（还剩多少没人领）
      以及每个节点的 **完成/已领/失败** 三个数。
      之前只有一个「各节点贡献 N」，那其实是**领了多少**，
      看着像在采、其实一条没做 —— 已改。
    """
    require_auth(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    return {"items": svc.tasks()}


@app.get("/api/assist/online")
def api_assist_online(request: Request) -> Dict[str, Any]:
    """**谁还在线**：心跳 + 每个节点的领用中/已完成/失败。

    界面上要能一眼看出「谁在采、有没有卡住」——
    之前只有一句「本机 agent 运行中 27s」，没法确认到底采没采。
    """
    require_auth(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    return {"items": svc.online()}


@app.post("/api/assist/pick")
def api_assist_pick(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """成员领活。node 优先用请求里的（agent 自报），否则用登录身份。

    ★ **不限配额**：`limit` 只是「这一轮领多少帖」的批量大小，
      不再受 `members.quota` 约束（Beta 期间配额取消，在线即可）。
    """
    m = require_auth(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    node = str(req.get("node") or m.get("node_id") or "")
    limit = int(req.get("limit") or 50)
    return svc.pick(node, limit, req.get("code") or None)


@app.post("/api/assist/return")
def api_assist_return(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**退活**：把本节点领走但还没回传的活退回池子。

    ★ 为什么需要
      「超时才回收」太慢：成员关掉窗口是最常见的死法，
      等 30 分钟期间池子对其他成员是空的。
      agent 退出时主动退活 → 池子立刻恢复可用。
      测试脚本领完活也必须退回去（否则自己把池子占死）。
    """
    m = require_auth(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    node = str(req.get("node") or m.get("node_id") or "")
    n = svc.return_picks(node, req.get("code") or None)
    if n:
        HUB.info(node, f"退回 {n} 条未回传的活", task="")
    return {"ok": True, "returned": n, "node": node}


@app.get("/api/assist/task/{code}")
def api_assist_task_detail(request: Request, code: str,
                           limit: int = Query(200, ge=1, le=1000),
                           offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    """协作任务的**详情**（概要 + 这个任务里的每一帖）。

    ★ 成员也能看
      协作的意义就是「大家能看到彼此干了什么」——
      只给管理员看，成员就不知道自己领的活最后怎么样了。
    """
    _require_member(request)
    svc = AssistService(get_db(_cfg()), HUB)
    d = svc.task_detail(code, limit=limit, offset=offset)
    if not d:
        raise HTTPException(404, f"任务 {code} 不存在")
    return d


@app.delete("/api/assist/task/{code}")
def api_assist_task_remove(request: Request, code: str) -> Dict[str, Any]:
    """**移除**一个协作任务（管理员专属）。

    ★ 与「撤销」的区别
      撤销 = 停止派活、活作废，**可恢复**；
      移除 = 从列表里删掉，**不可恢复**。
    ★ **已采到的正文不会被删** —— 正文独立于任务存在在权威库里。
      移除只删「这个任务」与「它的活」，合并痕迹（审计记录）也保留。
    """
    require_admin(request)
    svc = AssistService(get_db(_cfg()), HUB)
    r = svc.remove(code, "admin")
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "移除失败")
    return r


@app.post("/api/assist/cancel")
def api_assist_cancel(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**撤销**协作任务（管理员专属）。

    ★ 语义：**已完成的保留**（用户 2026-10-23 明确）
      撤销 = 这个任务作废、不再派活。
      · 已完成 / 失败 / 站方限制的活 → **原样保留**（正文已在权威库）
      · 未完成（pending/taken）的活 → 标成 `cancelled`（留痕，不删）
      · 权威库里的正文 → **一个字不动**
    """
    require_admin(request)
    code = str(req.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "没指定任务编号")
    svc = AssistService(get_db(_cfg()), HUB)
    r = svc.cancel(code, "admin")
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "撤销失败")
    return r


@app.post("/api/assist/restore")
def api_assist_restore(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """**恢复**被撤销的协作任务（管理员专属）：作废的活重新放回池子。"""
    require_admin(request)
    code = str(req.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "没指定任务编号")
    svc = AssistService(get_db(_cfg()), HUB)
    r = svc.restore(code, "admin")
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "恢复失败")
    return r


@app.get("/api/assist/conflicts")
def api_assist_conflicts(request: Request, code: str = "",
                         limit: int = Query(20, ge=1, le=200),
                         offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    """回传冲突记录（「不跳过、合并覆盖」的可查痕迹）—— **带分页**。

    ★ 为什么默认 limit 从 200 降到 20（2026-10-23）：
      合并记录会越积越多，一次性拉 200 条把整页撑得很长，
      反而让人找不到要看的东西。改成**每页 20 条 + 可翻页 + 可折叠**。
    """
    _require_member(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    items, total = svc.conflicts(code=code, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.post("/api/agent/heartbeat")
def api_agent_heartbeat(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    """成员 agent 的心跳。

    作用有三个：
      ① 让管理员在成员名单上看到「在线 / 离线」
      ② 与 agent 侧的「失联自动停止」配合 —— 心跳断了，agent 就会自己停下
      ③ **带上 agent 当前状态**（正在采 / 在等活）——
         这样界面上能区分「卡住了」和「**没活可领**」，
         而不必靠「多久没动静」去猜（实测踩过：池子被别的节点占死，
         本机 agent 领不到活却一声不吭，界面看着像程序坏了）。
    """
    m = require_auth(request)
    db = get_db(_cfg())
    node = str(req.get("node") or m.get("node_id") or "")
    state = str(req.get("state") or "")[:20]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with db.lock:
        db.conn.execute("UPDATE members SET last_seen=? WHERE id=?", (now, m["id"]))
        db.conn.commit()
    # ★ 状态**只在变化时**才落库（2026-10-23 改）
    #   原来每次心跳都写一次 meta —— agent 每 12 秒一次，
    #   几个节点一起来就把写锁抢得很凶，
    #   撞上别的写操作 → `database is locked` → 500
    #   （实测：管理员改密码被心跳顶掉 → 整个改密码流程 500）。
    #   状态本来就是「变了才需要记」的东西，没必要每次都写。
    if state:
        key = f"agent_state:{node}"
        try:
            old = json.loads(db.get_meta(key, "") or "{}")
        except Exception:                             # noqa: BLE001
            old = {}
        if str(old.get("state") or "") != state:
            db.set_meta(key, json.dumps(
                {"state": state, "note": str(req.get("note") or "")[:200],
                 "ts": now}, ensure_ascii=False))
    return {"ok": True, "node": node, "server_time": now}


# ---------------------------------------------------------------- 启动包（动态生成）
def _public_origin(request: Request) -> str:
    """当前访问者用的那个地址（**动态**）。

    ★ 为什么不能写死：
      管理员在本机打开是 http://127.0.0.1:8765，
      成员从公网打开是 https://msyt.example.com，
      域名/端口以后还可能再变。
      启动包是**每次下载现场生成**的 —— 里面的 `--server` 用的是
      下载者**此刻**访问的地址，所以永远是对的。
    """
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "127.0.0.1:8765").split(",")[0].strip()
    return f"{proto}://{host}"


@app.post("/api/agent/redeem")
def api_agent_redeem(req: dict = Body(...)) -> Dict[str, Any]:
    """用票据换 JWT。

    ★ 两种票据（2026-10-23 分开）：
      · **一次性票据**（`repeatable=0`）：网页签发，用一次作废。
        防的是「票据泄露后被别人复用」。
      · **包用长期凭证**（`repeatable=1`）：**30 天有效、可重复兑换**。
        为什么要分开：一次性票据让组员**第二次运行包就没凭证了**，
        30 分钟过期更是让他晚点跑也不行 ——
        启动包是**下载一次、长期使用**的东西，凭证必须跟得上。
        安全闸没丢：账号被停用（status != active）仍然兑换失败；
        管理员删掉该账号的票据记录也立即失效。

    这是给成员端 agent 用的：它拿到下载来的启动包 → 用凭证换到 JWT →
    就能领活、采集、回传，全程不需要密码。
    """
    ticket = str(req.get("ticket") or "").strip()
    if not ticket:
        raise HTTPException(400, "没有票据")
    db = get_db(_cfg())
    row = db.conn.execute("SELECT * FROM agent_tickets WHERE ticket=?",
                          (ticket,)).fetchone()
    if not row:
        raise HTTPException(401, "票据无效")
    repeatable = int(row["repeatable"] or 0)
    if row["used_at"] and not repeatable:
        raise HTTPException(401, "票据已用过（一次性票据）")
    if (row["expires_at"] or 0) < time.time():
        raise HTTPException(401, "票据已过期，请回网页重新下载启动包")
    m = _member_by_email(db, row["email"])
    if not m or (m.get("status") or "active") != "active":
        raise HTTPException(403, "账号不可用")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    node = m.get("node_id") or ("member:" + row["email"])
    with db.lock:
        db.conn.execute("UPDATE agent_tickets SET used_at=? WHERE ticket=?",
                        (now, ticket))
        db.conn.commit()
    token = make_token({"email": row["email"], "role": m.get("role") or "member",
                        "node": node}, _secret(_cfg()))
    HUB.info(node, f"{row['email']} 用启动凭证接入了采集", task="")
    return {"token": token, "member": {"email": row["email"],
                                       "role": m.get("role"),
                                       "node_id": node}}


def _cf_service_token() -> tuple:
    """读本机的 Cloudflare Access **service token**。

    ★ 当前方案**用不到它**（改用「独立 API 子域名 + Access bypass」了）。
      留在这里以备后用：若将来要把 agent 放回主域名、或需要给别的程序走 Access，
      service token 是在**没有浏览器会话**的情况下过 Access 的正规做法。

      ★ 存在哪：`程序/data/cf-service-token.txt`（不进仓库、不进记忆）。
      ★ 吊销：DELETE /accounts/{account_id}/access/service_tokens/{token_id}
    """
    p = Path(__file__).resolve().parent.parent / "data" / "cf-service-token.txt"
    try:
        if not p.exists():
            return "", ""
        kv = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip().lower()] = v.strip()
        return kv.get("id", ""), kv.get("secret", "")
    except Exception:                                 # noqa: BLE001
        return "", ""


# ====================================================================== 小助手端口
#: 成员端「本机小助手」在成员自己电脑上的监听端口（默认 8766）
#: ★ 为什么要能改（2026-10-24 用户要求）：
#:   8766 可能被别的软件占着（或公司电脑封了某些端口）→ 换一个。
#:   设置存在**服务端**（`程序/data/helper-port.txt`），因为**下载的启动包要跟它同步**：
#:   包里的「启动助手.cmd」会带上这个端口，网页也是用这个端口去呼叫它。
DEFAULT_HELPER_PORT = 8766
HELPER_PORT_FILE = Path(__file__).resolve().parent.parent / "data" / "helper-port.txt"


def _helper_port() -> int:
    """当前生效的小助手端口（读服务端设置，坏了就用默认值）。"""
    try:
        v = int(Path(HELPER_PORT_FILE).read_text(encoding="utf-8").strip())
        if 1024 <= v <= 65535:
            return v
    except Exception:                                 # noqa: BLE001
        pass
    return DEFAULT_HELPER_PORT


def _set_helper_port(port: int) -> int:
    """保存小助手端口。返回实际生效的端口。"""
    port = int(port)
    if not (1024 <= port <= 65535):
        raise HTTPException(400, "端口要在 1024~65535 之间")
    Path(HELPER_PORT_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(HELPER_PORT_FILE).write_text(str(port), encoding="utf-8")
    return port


@app.get("/api/agent/helper-port")
def api_agent_helper_port(request: Request) -> Dict[str, Any]:
    """小助手端口（**成员也能读** —— 网页要用它去呼叫成员本机的助手）。"""
    _require_member(request)
    p = _helper_port()
    return {"port": p, "default": DEFAULT_HELPER_PORT}

@app.post("/api/agent/helper-port")
def api_agent_helper_port_set(request: Request,
                              port: int = Body(..., embed=True)) -> Dict[str, Any]:
    """改小助手端口（**管理员专属**）。

    ★ 改完要提醒：**已经下载过包的成员需要重新下载一次**，
      否则包里的「启动助手.cmd」用的还是旧端口，网页与本机对不上。
    """
    require_admin(request)
    p = _set_helper_port(port)
    return {"ok": True, "port": p,
            "message": f"新下载的启动包会改用端口 {p}；"
                       f"已经下载过包的成员需要**重新下载一次**才能对上。"}


# ====================================================================== 采集并发数
#: 详情采集的**并发线程数**（2026-10-25 加）
#:
#: ★ 为什么能调、推荐多少
#:   实测（2026-10-25，真实请求对，逐级加压，每档之间歇 20 秒）：
#:     1 路 0 异常 ≈ 61,700 帖/天    2 路 0 异常 ≈ 96,000 帖/天
#:     3 路 0 异常 ≈ 123,000 帖/天   ← **推荐值**（产能翻倍且零异常）
#:     5 路 出现 network 错误、吞吐反降（105,000）
#:   → 3 是安全档；再往上站方会限速，**反而更慢**。
#:   → 站方是**按 IP 限流**的：各成员在不同 IP 上，互不影响；
#:     但**同一台机器**上跑多个采集进程会互相挤占。
#:
#: ★ 设置存在**服务端**（`程序/data/detail-workers.txt`），
#:   成员也可以只改**自己那台机器**（在助手里设，网页上显示本机值）。
DEFAULT_DETAIL_WORKERS = 3
RECOMMENDED_DETAIL_WORKERS = 3
DETAIL_WORKERS_FILE = (Path(__file__).resolve().parent.parent
                       / "data" / "detail-workers.txt")
DETAIL_WORKERS_MIN = 1
DETAIL_WORKERS_MAX = 8


def _detail_workers() -> int:
    """当前生效的采集并发数（读服务端设置，坏了就用推荐值）。"""
    try:
        v = int(DETAIL_WORKERS_FILE.read_text(encoding="utf-8").strip())
        if DETAIL_WORKERS_MIN <= v <= DETAIL_WORKERS_MAX:
            return v
    except Exception:                                 # noqa: BLE001
        pass
    return RECOMMENDED_DETAIL_WORKERS


def _set_detail_workers(n: int) -> int:
    """保存服务端默认的采集并发数。返回实际生效的值。"""
    n = int(n)
    if not (DETAIL_WORKERS_MIN <= n <= DETAIL_WORKERS_MAX):
        raise HTTPException(
            400, f"并发数要在 {DETAIL_WORKERS_MIN}~{DETAIL_WORKERS_MAX} 之间")
    DETAIL_WORKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DETAIL_WORKERS_FILE.write_text(str(n), encoding="utf-8")
    return n


@app.get("/api/agent/workers")
def api_agent_workers(request: Request) -> Dict[str, Any]:
    """采集并发数（**成员也能读** —— 助手起 agent 时要用它）。"""
    _require_member(request)
    w = _detail_workers()
    return {"workers": w, "recommended": RECOMMENDED_DETAIL_WORKERS,
            "min": DETAIL_WORKERS_MIN, "max": DETAIL_WORKERS_MAX,
            "tip": "推荐 3 路：实测 1 路约 6.2 万帖/天，3 路约 12.3 万（零异常）；"
                   "5 路起站方会限速，反而更慢。"}


@app.post("/api/agent/workers")
def api_agent_workers_set(request: Request,
                          workers: int = Body(..., embed=True)) -> Dict[str, Any]:
    """改**服务器默认**的采集并发数（**管理员专属**）。

    ★ 改完不需要成员重下包 —— 助手每次起 agent 前都会来读一次这个值。
    """
    require_admin(request)
    w = _set_detail_workers(workers)
    return {"ok": True, "workers": w, "recommended": RECOMMENDED_DETAIL_WORKERS,
            "message": (f"服务器默认并发数已改为 {w} 路。"
                        + ("（正是推荐值）" if w == RECOMMENDED_DETAIL_WORKERS
                           else f"（推荐 {RECOMMENDED_DETAIL_WORKERS} 路）"))}


def _agent_api_base(request: Request) -> str:
    """agent 专用的 API 根地址。

    ★ 为什么和「下载者访问的地址」分开：
      成员机器上的 agent **没有浏览器、没有 Access 会话 Cookie**，
      打主域名会被 Cloudflare Access 挡下（返回登录页 HTML）。
      所以给 agent 单独开一个**子域名** `api.msyt.…`，那边只挂 `/api/agent/*`
      且 Access 对它 bypass —— agent 能直连，而主域名的网页 UI 照旧受 Access 保护。

    地址写在 `程序/data/agent-api-base.txt`（改域名/端口后改这里 + 重启即可）。
    没配就退回「下载者此刻访问的地址」（本地调试时正确）。
    """
    p = Path(__file__).resolve().parent.parent / "data" / "agent-api-base.txt"
    try:
        if p.exists():
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return v.rstrip("/")
    except Exception:                                 # noqa: BLE001
        pass
    return _public_origin(request)


@app.post("/api/agent/ticket")
def api_agent_ticket(request: Request) -> Dict[str, Any]:
    """**签发一次性启动凭证**（给当前登录的人）。

    ★ 这就是「配合网站登录」的那一环：
      凭证**只有在程序账号登录成功之后**才能签发（这里是 require_auth）。
      所以 agentapi 那个端口就算被人扫到了，没有网站签发的凭证，
      就换不到 JWT、领不到活、回传不了 —— 端口开着，**门是锁着的**。

    票据：30 分钟内有效、用一次作废、绑定到本人。
    """
    m = require_auth(request)
    db = get_db(_cfg())
    ticket = new_secret()[:32]
    origin = _agent_api_base(request)
    now = time.time()
    with db.lock:
        # 顺手清掉过期票据，免得表里越积越多
        db.conn.execute("DELETE FROM agent_tickets WHERE expires_at < ?",
                        (int(now) - 3600,))
        db.conn.execute(
            "INSERT INTO agent_tickets(ticket,email,node_id,server,expires_at,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (ticket, m["email"], m.get("node_id"), origin, int(now + 1800),
             time.strftime("%Y-%m-%d %H:%M:%S")))
        db.conn.commit()
    return {"ok": True, "ticket": ticket, "server": origin, "expires_in": 1800,
            "message": "凭证 30 分钟内有效、只能用一次；过期回网页重新生成即可。"}


@app.get("/api/agent/bundle.zip")
def api_agent_bundle(request: Request) -> Response:
    """**实时生成**成员端启动包并下载。

    ★★ 这个包是「**本地浏览器 + 我们的服务器**协同生成」的（2026-10-23）：
      两边各出一半，合起来才是一个能用的包：

      | 来源 | 提供什么 |
      |---|---|
      | **本地浏览器**（下载者此刻访问的地址） | 「我是从哪儿访问的」 |
      | **我们的服务器**（部署配置 `data/agent-api-base.txt`） | 「我部署在哪儿」 |

      合成结果 = 包里带**两个**候选地址：

      | 下载来源 | 主用 | 备用 |
      |---|---|---|
      | **本地**（127.0.0.1 / localhost） | 本地地址（本地调试，agent 直连本机） | agentapi 子域名（公网） |
      | **公网** | agentapi 子域名（**绕过 Access**，agent 唯一能直连的） | 下载者访问的地址 |

      agent 启动时**自己探一下**：主地址不通就换备用 ——
      组员不必为了换个地址重下包。

    ★ 包里没有密码，只有**绑定到当前账号的启动凭证**。
    ★★ 凭证是「30 天有效 + 可重复兑换」（2026-10-23 改）：
       原来是一次性票据（30 分钟过期、用一次作废）——
       启动包是**下载一次、长期使用**的东西，
       一次性的凭证让组员第二次运行、或晚点运行就完全没法用了。
       安全闸不变：账号被停用仍然兑换失败。
    """
    m = require_auth(request)
    db = get_db(_cfg())
    # ---- 「本地浏览器」那边告诉服务器：我此刻访问的是哪个地址 ----
    origin = _public_origin(request)
    # ---- 「我们的服务器」那边告诉包：我部署在哪个地址 ----
    api_base = _agent_api_base(request)

    # 判定下载者是「本地访问」还是「公网访问」
    host = origin.split("//", 1)[-1].split(":", 1)[0].lower()
    is_local = host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")

    if is_local:
        # 本地调试：agent 直连本机最省事；公网当备用
        primary, alt = origin, api_base
    else:
        # 公网访问：agentapi 子域名是它唯一能直连的（Access 对它 bypass）
        primary, alt = api_base, origin
    if primary == alt:
        alt = ""
    # ★ 备用地址写成**多行**（2026-10-26）：隧道守护维持多条隧道，
    #   每条一个地址 → 全写进 `server-alt.txt`，组员哪条通就用哪条。
    eps, _ = _current_endpoints()
    alt_all = "\n".join([u for u in ([alt] + eps) if u])

    ticket = new_secret()[:32]
    now = time.time()
    with db.lock:
        # 顺手清掉过期票据，免得表里越积越多
        db.conn.execute("DELETE FROM agent_tickets WHERE expires_at < ?",
                        (int(now) - 3600,))
        db.conn.execute(
            "INSERT INTO agent_tickets(ticket,email,node_id,server,expires_at,"
            "created_at,repeatable) VALUES(?,?,?,?,?,?,1)",
            (ticket, m["email"], m.get("node_id"), primary,
             int(now + 30 * 86400),          # ★ 30 天
             time.strftime("%Y-%m-%d %H:%M:%S")))
        db.conn.commit()
    pkg_dir = Path(__file__).resolve().parent.parent
    data, filename = bundle.build_bundle(
        primary, ticket, str(pkg_dir),
        helper_src=str(pkg_dir / "msyt-helper.py"),
        server_alt=alt,
        server_alt_all=alt_all,
        helper_port=_helper_port(),
        agent_workers=_detail_workers())
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="msyt-agent-bundle.zip"'})


def _retry_locked(fn, *args, **kwargs):
    """遇到 SQLite「database is locked」就**重试**几次。

    ★ 为什么需要（2026-10-23 实测踩过）
      服务端是多线程、**每个线程一条 SQLite 连接**（get_db 是 thread-local），
      同一时刻可能有两条连接在写。WAL 下写是串行的，后一条要等前一条提交；
      但「读事务中途升级为写」的情形 SQLite **不走 busy handler**，
      会直接抛 `database is locked` → HTTP 500。
      而 agent 把 5xx 当成「可能失联」→ 连着几次就**自己停了**，
      成员的活停在半路，界面看着像程序坏了。
      所以：写路径上遇到锁冲突就退避重试，别让成员白跑。
    """
    import sqlite3
    last = None
    for i in range(4):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = e
            time.sleep(0.4 * (i + 1))
    raise last


@app.post("/api/assist/submit")
def api_assist_submit(request: Request, req: dict = Body(...)) -> Dict[str, Any]:
    m = require_auth(request)
    cfg = _cfg()
    svc = AssistService(get_db(cfg), HUB)
    node = str(req.get("node") or m.get("node_id") or "")
    return _retry_locked(svc.submit, node, str(req.get("code") or ""),
                         int(req.get("tid") or 0),
                         req.get("posts") or [], req.get("pages") or [],
                         req.get("summary") or {})


@app.get("/api/stream")
async def api_stream(request: Request, since: int = 0, node: str = "", token: str = ""):
    """SSE：实时日志流。管理员看全部，成员只看自己节点的。"""
    m = require_auth(request, query_token=token)
    filt = None if (m.get("role") == "admin") else (m.get("node_id") or node)
    return StreamingResponse(
        HUB.stream(seq=since, node=filt or None),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


# ====================================================================== 静态页
def _asset_ver(name: str) -> str:
    """静态资源的版本号 —— **按文件的 mtime 现算**。

    ★ 为什么不能写死（2026-10-23 实测踩过）
      index.html 里原来是 `style.css?v=1.1.0` —— 版本号是手写的常量。
      改了 CSS/JS 之后版本号没跟着变，浏览器就**继续用旧缓存**：
      服务端返回的是新文件，页面加载的还是旧的
      → 「我明明改了，怎么界面没变」。
      而且这个坑只有在**改完前端、又没重启浏览器**时才显形，极难自查。
      改成按 mtime 现算：文件一改，URL 就变，浏览器必然重新取。
    """
    p = WEB_DIR / name
    try:
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"


@app.get("/")
def index() -> Response:
    try:
        ensure_admin(_cfg())      # 首次访问时确保管理员账号存在
    except Exception:
        pass
    try:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    except OSError:
        return FileResponse(WEB_DIR / "index.html")
    html = (html.replace("__CSSV__", _asset_ver("style.css"))
                .replace("__JSV__", _asset_ver("app.js")))
    return Response(content=html, media_type="text/html; charset=utf-8")


#: 访问口令页 —— 刻意**不出现**项目名、目标站名、任何业务信息。
#:   外网人就算猜到地址，也只会看到一个中性验证页。
GATE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>访问验证</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#f4f6f8;
         font-family: system-ui, "Microsoft YaHei", sans-serif; }
  .card { background:#fff; padding:32px 28px; border-radius:10px;
          box-shadow:0 2px 14px rgba(0,0,0,.08); width:320px; }
  h1 { font-size:17px; margin:0 0 6px; color:#222; }
  p  { font-size:13px; color:#666; margin:0 0 18px; line-height:1.6; }
  input { width:100%; box-sizing:border-box; padding:9px 10px; font-size:14px;
          border:1px solid #d5d9de; border-radius:6px; }
  button { width:100%; margin-top:12px; padding:10px; font-size:14px; cursor:pointer;
           border:0; border-radius:6px; background:#2b6cb0; color:#fff; }
  #err { color:#c0392b; font-size:13px; margin-top:10px; min-height:18px; }
</style>
</head>
<body>
  <form class="card" onsubmit="return false;">
    <h1>访问验证</h1>
    <p>本系统仅供授权人员使用。请输入访问口令。</p>
    <input id="k" type="password" placeholder="访问口令" autocomplete="off">
    <button onclick="go()">进入</button>
    <div id="err"></div>
  </form>
<script>
async function go() {
  var k = document.getElementById('k').value.trim();
  var e = document.getElementById('err');
  if (!k) { e.textContent = '请输入口令'; return; }
  var next = new URLSearchParams(location.search).get('next') || '/';
  try {
    var r = await fetch('/api/auth/gate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: k, next: next})
    });
    if (r.ok) { location.href = next; return; }
    e.textContent = '口令不对';
  } catch (err) { e.textContent = '网络异常，请重试'; }
}
document.getElementById('k').addEventListener('keydown', function(ev){
  if (ev.key === 'Enter') go();
});
</script>
</body>
</html>
"""


@app.get("/gate")
def gate_page() -> Response:
    """**访问口令页**（口令门启用时，未带口令的浏览器访问会先落到这里）。"""
    if not _gate_key():
        return RedirectResponse(url="/", status_code=302)
    return Response(content=GATE_HTML, media_type="text/html; charset=utf-8")


#: 口令门**自己的**失败限流（按 IP）。
#: ★ 为什么必须单独有：口令校验接口 `/api/auth/gate` 在**白名单里**（否则
#:   外人根本进不到口令页）→ 它是**公开可打**的 → 不限速就等于
#:   摆了一个「无限次猜口令」的靶子。
_GATE_FAIL: Dict[str, List[float]] = {}
_GATE_MAX = 10             # 5 分钟内错 10 次就锁
_GATE_WINDOW = 300.0


def _gate_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _GATE_FAIL.get(ip, []) if now - t < _GATE_WINDOW]
    _GATE_FAIL[ip] = hits
    return len(hits) < _GATE_MAX


def _gate_fail(ip: str) -> None:
    now = time.time()
    hits = [t for t in _GATE_FAIL.get(ip, []) if now - t < _GATE_WINDOW]
    hits.append(now)
    _GATE_FAIL[ip] = hits


@app.post("/api/auth/gate")
def api_gate(request: Request, req: dict = Body(...)) -> Response:
    """校验访问口令 → 下发 Cookie（7 天）。

    ★ 这个接口在口令门的**白名单**里（外人得先能打开口令页），
      所以它是**公开可打**的 → 必须**自己有限速**，
      否则等于挂了一个「无限次猜口令」的靶子。
    """
    key = _gate_key()
    if not key:
        return JSONResponse({"ok": True, "next": "/"})
    local = _is_local_request(request)      # ★ 本机豁免（本机调试/测试不计数）
    ip = _client_ip(request)
    if not local:
        if not _gate_rate_ok(ip):
            raise HTTPException(429, "口令错误次数过多，请 5 分钟后再试")
    if str(req.get("key") or "").strip() != key:
        if not local:
            _gate_fail(ip)
        time.sleep(0.3)              # 慢一点，别让口令被高速枚举
        raise HTTPException(401, "口令不对")
    if not local:
        _GATE_FAIL.pop(ip, None)
    nxt = str(req.get("next") or "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    resp = JSONResponse({"ok": True, "next": nxt})
    resp.set_cookie("msyt_gate", key, httponly=True, samesite="lax",
                    max_age=7 * 24 * 3600, path="/")
    return resp


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
