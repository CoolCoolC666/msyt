# -*- coding: utf-8 -*-
"""验收：并发采集（2026-10-25）。

★ 为什么并发单位是「一帖」而不是「一个请求」
  站方 token 有效期 **< 30 秒**（实测 30/60/120/300 秒全部失效）：
  详情页取到 token 后，**紧接着的那次 ajax 请求中间不得插入任何 sleep**。
  → 同帖两个请求必须贴在一起；
  → 能并发的最小单位是「一帖」，把两个请求拆到不同线程必然踩 token 过期。

★ 并发安全的三条前提（缺一不可）
  ① 领活互斥：`claim_next_detail()` 在共享写锁里
  ② 写库串行：所有写操作走同一把进程级共享锁
  ③ 每 worker 自己的连接与客户端：
     sqlite3 连接不能跨线程；requests.Session 不是线程安全的；
     而且限速状态是**实例级**的 → 共用会让限速被绕过

★ 站方承受力（2026-10-25 实测，真实请求对）
  1 路 0 异常 61,700 帖/天 · 2 路 0 异常 96,000 · **3 路 0 异常 123,000（最优点）**
  5 路 出现 network 错误、吞吐反降 → 所以推荐 3
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
PKG = os.path.join(ROOT, "程序")
sys.path.insert(0, PKG)

OK = 0
FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  —— {extra}" if extra else ""))


from msyt.parallel import run_parallel, split_evenly      # noqa: E402

print("### A. 并发执行器（纯逻辑，不碰站方）")
items = list(range(12))

res, errs = run_parallel(items, lambda x: x * 2, workers=1)
check("workers=1：结果完整且保持顺序", res == [x * 2 for x in items], str(res))
check("workers=1：无错误", not errs)

res, errs = run_parallel(items, lambda x: x * 2, workers=3)
check("workers=3：结果完整且保持顺序", res == [x * 2 for x in items], str(res))
check("workers=3：无错误", not errs)


def _boom(x):
    if x == 5:
        raise ValueError("故意炸")
    return x


res, errs = run_parallel(items, _boom, workers=3)
check("一个任务抛异常，不影响其它任务", len(res) == 11 and len(errs) == 1,
      f"成功 {len(res)} / 失败 {len(errs)}")
check("异常被如实捕获（不吞掉）", errs and isinstance(errs[0][1], ValueError))

seen = []


def _stopper(x):
    seen.append(x)
    return x


res, errs = run_parallel(items, _stopper, workers=1,
                         should_stop=lambda: len(seen) >= 4)
check("should_stop 能提前收工", len(seen) == 4, f"实际处理 {len(seen)} 个")

b = split_evenly(items, 3)
check("split_evenly 均分且不丢", sum(len(x) for x in b) == len(items)
      and all(len(x) == 4 for x in b), str([len(x) for x in b]))

print()
print("### B. 配置与源码约定")
from msyt.config import Config                              # noqa: E402
cfg = Config.load()
check("Config 有 detail_workers", hasattr(cfg, "detail_workers"))
check("默认 detail_workers=3（实测安全档；改成 1 即退回串行）",
      Config().detail_workers == 3, f"默认 {Config().detail_workers}")

detail_src = open(os.path.join(PKG, "msyt", "detail.py"), encoding="utf-8").read()
config_src = open(os.path.join(PKG, "msyt", "config.py"), encoding="utf-8").read()
agent_src = open(os.path.join(PKG, "msyt", "agent.py"), encoding="utf-8").read()
webapp_src = open(os.path.join(PKG, "msyt", "webapp.py"), encoding="utf-8").read()
check("detail.run 支持 workers 参数", "workers: int = 1" in detail_src)
check("detail 有并发实现 _run_parallel", "def _run_parallel" in detail_src)
check("detail 有 for_thread（每线程独立 client/连接）",
      "def for_thread" in detail_src)
check("detail 并发路径用 run 的独立 client",
      "self.for_thread()" in detail_src)
check("webapp 把 workers 传给 dc.run（取**服务端设置**，可网页改）",
      "workers=_detail_workers()" in webapp_src)

print()
print("### ② 日志节奏：只打进度、不逐帖打（2026-10-25 用户要求）")
check("有进度间隔常量（5 秒）",
      "PROGRESS_INTERVAL = 5.0" in detail_src)
check("进度行里有「队列还剩」（用户要「还剩多少篇」）",
      "队列还剩" in detail_src)
check("进度行里有已采 / 目标 / 用时 / 速率",
      "已采" in detail_src and "本次目标" in detail_src
      and "帖/秒" in detail_src)
check("进度行只查一次队列（每 5 秒一次，不心疼）",
      "SELECT COUNT(*) FROM detail_queue WHERE state='pending'" in detail_src)
# 并发路径里**成功**不再逐帖打（受限/失败仍逐帖打）
_par = detail_src.split("def _run_parallel")[-1]
check("并发路径不再逐帖打「成功」日志",
      "✓ {res['pages']} 页" not in _par)
check("并发路径仍逐帖打「站方限制」",
      "⊘ 站方限制" in _par)
check("并发路径仍逐帖打「失败」",
      "✗ {res.get('error')}" in _par)
appjs = open(os.path.join(PKG, "msyt", "web", "app.js"), encoding="utf-8").read()
check("网页任务轮询 5 秒（用户要 5-10s 刷新）",
      "}, 5000);       // ★ 2026-10-25" in appjs)
check("网页助手轮询 5 秒",
      "setInterval(once, 5000)" in appjs)
check("agent 有 workers 参数", "workers: int = 1" in agent_src)
check("agent 有 _process_pick（串行/并发共用）",
      "def _process_pick" in agent_src)
check("agent 有 _session_for_thread（每线程独立会话）",
      "def _session_for_thread" in agent_src)
check("agent 计数加锁", "def _bump" in agent_src and "self._lock" in agent_src)

print()
print("### ③ 任务卡片直接显示「还剩多少」（2026-10-25 补）")
webapp_src = open(os.path.join(PKG, "msyt", "webapp.py"), encoding="utf-8").read()
check("服务端给任务带 last_progress 字段",
      '"last_progress": last_progress' in webapp_src)
check("last_progress 是从日志里摘最后一条「进度：」行",
      'if "进度：" in m' in webapp_src)
check("前端有 jobProgressText（把进度拼到卡片上）",
      "function jobProgressText" in appjs)
check("卡片渲染里用到了 jobProgressText",
      "jobProgressText(j)" in appjs)
check("前端取「已采 / 目标 / 还剩」三个数",
      "已采" in appjs and "队列还剩" in appjs)
check("并发上限写在文档里（3 路最优点）",
      "3 路" in config_src and "123,000" in config_src)

print()
print("### C. 真实采集：workers=1 与 workers=3 结果必须一致")
from msyt.db import DB                                     # noqa: E402
from msyt.detail import DetailCollector                    # noqa: E402
from msyt.http_client import Client                        # noqa: E402

db = DB(cfg.db_path)
conn = sqlite3.connect(cfg.db_path)
conn.row_factory = sqlite3.Row
# ★ 挑**已采完**的可见帖，不是待采的：
#   待采的帖在生产库的 detail_queue 里，服务端采集器（按 pub_ts 升序领）
#   会**领到同一批** → 双方同时打站方 → 触发限流提示页 → 断言飘。
#   已采完的帖队列里是 done，服务端不会再碰 → 彻底隔离。
picks = [int(r["tid"]) for r in conn.execute(
    """SELECT tid FROM threads
        WHERE detail_state='done'
          AND pub_ts >= strftime('%s','2023-12-31')
        ORDER BY tid DESC LIMIT 6""")]
rows_pick = [dict(r) for r in conn.execute(
    "SELECT tid,title,replies,author,pub_time,pub_ts FROM threads "
    "WHERE tid IN (%s)" % ",".join(str(t) for t in picks))]
conn.close()
check("取到 6 个**已采完**的可见帖（与服务端隔离）", len(picks) == 6, str(picks))

# ★★ **必须用独立临时库**，不能碰生产库：
#   ① 生产库上跑 run() 会跟**服务端采集 / 组员 agent** 抢同一批帖
#      （实测：测试刚把某帖清空、另一个进程又写回 → 状态自相矛盾）
#   ② `claim_next_detail()` 是**按 pub_ts 升序**领活的（先旧后新，设计如此），
#      生产库队列里最老的是 2012 年的老帖 → 根本领不到我们插进去的 6 个
#      （实测 attempts=0，restricted=6 全是老帖）。
#   → 建一个只装这 6 帖的临时库，完整跑「领活 + 采集 + 写库」。
import shutil                                           # noqa: E402
import tempfile                                         # noqa: E402

tmpdir = tempfile.mkdtemp(prefix="msyt-par-test-")
_seq = [0]


def _fresh_tmpdb():
    """建一个只含这 6 帖的临时库（含队列行）。

    ★★ **每次必须用全新的文件名**，不能「删了重建」：
      SQLite 的 WAL 模式下，`msyt.db-wal` / `msyt.db-shm` 是**独立的文件**。
      只删主库 → 残留的 WAL 会被新连接读走，多个连接对着同一份残留 WAL 写
      → **数据互相覆盖，采到的楼层凭空消失**（实测：并发轮 6 帖只写进 2 帖，
      另外 4 帖楼层数 0、状态还停在 pending）。
      → 所以用唯一文件名 + 结束时整目录删掉。
    """
    _seq[0] += 1
    path = os.path.join(tmpdir, f"t{_seq[0]}.db")
    tdb = DB(path)
    c = tdb.conn
    for r in rows_pick:
        c.execute(
            "INSERT OR IGNORE INTO threads(tid,title,replies,author,pub_time,pub_ts) "
            "VALUES(?,?,?,?,?,?)",
            (r["tid"], r["title"], r["replies"], r["author"],
             r["pub_time"], r["pub_ts"]))
    c.executemany(
        "INSERT OR IGNORE INTO detail_queue(tid,state,enqueued_at,updated_at) "
        "VALUES(?, 'pending', '2026-10-25', '2026-10-25')", [(t,) for t in picks])
    c.commit()
    return tdb


def _collect(workers, attempt=1):
    tdb = _fresh_tmpdb()
    dc = DetailCollector(tdb, Client(cfg, verbose=False), cfg)
    t0 = time.time()
    r = dc.run(limit=len(picks), workers=workers)
    el = time.time() - t0
    got = {}
    c = tdb.conn
    for t in picks:
        n = c.execute("SELECT COUNT(DISTINCT pid) FROM posts WHERE tid=? AND pid>0",
                      (t,)).fetchone()[0]
        st = c.execute("SELECT detail_state FROM threads WHERE tid=?",
                        (t,)).fetchone()[0]
        qst = c.execute("SELECT state FROM detail_queue WHERE tid=?",
                        (t,)).fetchone()[0]
        got[t] = (n, st, qst)
    c.close()
    tdb.conn.close()
    print(f"    workers={workers} 第{attempt}次: {r} 用时 {el:.0f}s"
          f"（{el/max(1,len(picks)):.1f}s/帖）")
    for t in picks:
        print(f"       tid={t}: {got[t][0]} 楼  状态={got[t][1]}  队列={got[t][2]}")
    return got, r, el


def _collect_retry(workers, tries=2):
    """★ 整轮重试：站方会**间歇性**返回「提示信息」页（密集访问时出现）。
    那种帖这轮会采到 0 层（判为临时故障、不是受限）→ 断言会飘。
    → 整轮重试一次；两轮都拿不到才算真问题。"""
    got = r = el = None
    for i in range(1, tries + 1):
        got, r, el = _collect(workers, i)
        if all(v[0] > 0 for v in got.values()):
            return got, r, el
        if i < tries:
            print("       （有帖这轮没采到 —— 疑似站方瞬时提示页，整轮重试）")
            time.sleep(3)
    return got, r, el


ser, r1, el1 = _collect_retry(1)
par, r3, el3 = _collect_retry(3)

check("两种并发度采到的楼数完全一致",
      {k: v[0] for k, v in ser.items()} == {k: v[0] for k, v in par.items()},
      f"串行 {[v[0] for v in ser.values()]} / 并发 {[v[0] for v in par.values()]}")
check("两种并发度采到的状态完全一致",
      {k: v[1] for k, v in ser.items()} == {k: v[1] for k, v in par.items()},
      f"串行 {[v[1] for v in ser.values()]} / 并发 {[v[1] for v in par.values()]}")
check("并发不超采（领活数 == limit）",
      r3.get("done", 0) + r3.get("failed", 0) + r3.get("restricted", 0) <= len(picks),
      f"{r3}（限额 {len(picks)}）")
check("并发采的帖状态都是 done",
      all(v[1] == "done" for v in par.values()),
      str([v[1] for v in par.values()]))
check("队列行都归位（不再是 pending）",
      all(v[2] != "pending" for v in par.values()),
      str([v[2] for v in par.values()]))
check("并发确实更快（同样 6 帖）", el3 < el1,
      f"串行 {el1:.0f}s → 并发 {el3:.0f}s")
check("两轮都没有把帖误判成站方限制",
      all(v[1] != "restricted" for v in par.values()),
      str([v[1] for v in par.values()]))
shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("### D. 库状态：没有重复 pid、队列归位")
conn = sqlite3.connect(cfg.db_path)
dup = conn.execute(
    "SELECT COUNT(*) FROM (SELECT pid FROM posts WHERE pid>0 "
    "GROUP BY pid HAVING COUNT(*)>1)").fetchone()[0]
check("库里没有重复 pid（并发不会重复写同一层）", dup == 0, f"重复 {dup} 个")
orphan = conn.execute(
    "SELECT COUNT(*) FROM posts p LEFT JOIN threads t ON t.tid=p.tid "
    "WHERE t.tid IS NULL").fetchone()[0]
check("没有孤儿楼层", orphan == 0, f"孤儿 {orphan}")
conn.close()
db.conn.close()

print()
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
