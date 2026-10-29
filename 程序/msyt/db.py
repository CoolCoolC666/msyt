# -*- coding: utf-8 -*-
"""
数据库层：帖 / 楼层 / 受理流程 / 多维标签 + 两张 FTS5 索引 + 组合检索
================================================================
移植自 cacx（01 项目）的 db.py，**保留其检索语义**：
  · FTS5 外部内容表 + trigram 分词器 + 触发器 + rebuild
  · 短词（< 3 字）**整体**退回 LIKE，避免「半边索引半边扫描」漏结果
  · search() 返回 {total, items, mode, took_ms}，mode 标 fts/like

与 cacx 的三点差别（都是模型差异，不是偷懒）：
  1. cacx 是「一篇文章」一层；本项目是**帖 → 楼层**两层，
     所以拆成 threads_fts（标题/部门）与 posts_fts（楼层正文）两张表。
  2. cacx 一篇文章的正文在一个字段里；本项目一个帖的正文分散在 N 个楼层，
     所以检索统一在**帖**这一层收敛（scope=thread/post/all），
     否则一条帖会在结果里出现十几次。
  3. cacx 的 articles 无「维度标签」概念；本项目站方自带
     类别/状态/部门/区县市/留言箱/时效灯六条正交标签轴，
     入库时打上 tags，检索时可任意切片。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- 帖（列表页可得）
CREATE TABLE IF NOT EXISTS threads (
    tid            INTEGER PRIMARY KEY,
    title          TEXT,
    url            TEXT,
    -- 多维标签（站方自带）
    category       TEXT,      -- 类别
    state          TEXT,      -- 受理状态
    dept_id        TEXT,      -- 主部门 typeid
    dept_name      TEXT,
    dept_group     TEXT,      -- 部门所属分组：func/district/enterprise/other
    dept_district  TEXT,      -- 若主部门属区县市，则为其名称
    extra_dept_ids TEXT,      -- 多标签部门，逗号分隔
    extra_dept_names TEXT,
    mailbox_id     TEXT,      -- 留言箱 23/24
    mailbox_name   TEXT,
    light_code     TEXT,      -- green/yellow/grey/warn
    light_name     TEXT,
    -- 元数据
    author         TEXT,
    author_uid     TEXT,
    pub_time       TEXT,
    pub_day        TEXT,
    pub_ts         INTEGER,
    replies        INTEGER DEFAULT 0,
    views          INTEGER DEFAULT 0,
    has_image      INTEGER DEFAULT 0,
    last_reply_by  TEXT,
    last_reply_time TEXT,
    last_reply_ts  INTEGER,
    -- 详情页补全
    detail_state   TEXT DEFAULT 'pending',
    detail_warn    TEXT,          -- 采到了但有告警（如站方楼层计数不自洽）
    first_post_id  INTEGER,   -- 完整性：头
    last_post_id   INTEGER,   -- 完整性：尾
    pages_total    INTEGER,
    posts_n        INTEGER,
    chars          INTEGER DEFAULT 0,
    list_page      INTEGER,
    first_seen     TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_dept     ON threads(dept_id);
CREATE INDEX IF NOT EXISTS idx_threads_group    ON threads(dept_group);
CREATE INDEX IF NOT EXISTS idx_threads_cat      ON threads(category);
CREATE INDEX IF NOT EXISTS idx_threads_state    ON threads(state);
CREATE INDEX IF NOT EXISTS idx_threads_mailbox  ON threads(mailbox_id);
CREATE INDEX IF NOT EXISTS idx_threads_pub      ON threads(pub_ts);
CREATE INDEX IF NOT EXISTS idx_threads_detail   ON threads(detail_state);

-- ---------------------------------------------------------------- 楼层
CREATE TABLE IF NOT EXISTS posts (
    pid        INTEGER PRIMARY KEY,
    tid        INTEGER NOT NULL,
    page_no    INTEGER,
    floor      INTEGER,
    author     TEXT,
    author_uid TEXT,
    post_time  TEXT,
    post_ts    INTEGER,
    content    TEXT,
    chars      INTEGER DEFAULT 0,
    images     TEXT,
    fetched_at TEXT,
    FOREIGN KEY (tid) REFERENCES threads(tid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_posts_tid  ON posts(tid);
CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(post_ts);

-- ---------------------------------------------------------------- 受理流程时间线
CREATE TABLE IF NOT EXISTS flows (
    tid       INTEGER NOT NULL,
    step      INTEGER NOT NULL,
    name      TEXT,
    time      TEXT,
    ts        INTEGER,
    PRIMARY KEY (tid, step)
);

-- ---------------------------------------------------------------- 标签 & 采集源
CREATE TABLE IF NOT EXISTS thread_tags (
    tid  INTEGER NOT NULL,
    dim  TEXT NOT NULL,     -- dept/group/district/category/state/mailbox/light
    code TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (tid, dim, code)
);
CREATE INDEX IF NOT EXISTS idx_ttags_dim ON thread_tags(dim, code);

CREATE TABLE IF NOT EXISTS thread_sources (
    tid        INTEGER NOT NULL,
    source     TEXT NOT NULL,   -- global / dept:495 / group:func / category:1 ...
    first_seen TEXT,
    PRIMARY KEY (tid, source)
);

-- ---------------------------------------------------------------- 列表形态
-- ★ 这个 tid 在列表页里**以什么形态出现过**（2026-10-25 加）
--   kind: row      普通帖行（tbody[id^=normalthread_]）
--         stick    置顶帖行（tbody[id^=stickthread_]）
--         announce 站方公告区（#msbgt 里的裸链接）
-- ★ 为什么单独记：
--   站方对**历史帖**只给作者本人查看，但**置顶的老帖能看**
--   （实测 tid=6177045，2013-08-30 的置顶帖 → 77 楼全采到；
--     同一天发的另外 13 个帖全部受限）。
--   → 批量标注「受限帖」时，置顶帖必须**豁免**，否则会误标重要的老帖。
--   → 单独建表而不是给 thread_sources 加列：不动已有表结构，零迁移风险。
CREATE TABLE IF NOT EXISTS thread_list_forms (
    tid        INTEGER NOT NULL,
    source     TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- row / stick / announce
    first_seen TEXT,
    PRIMARY KEY (tid, source, kind)
);
CREATE INDEX IF NOT EXISTS idx_tlf_kind ON thread_list_forms(kind, tid);

-- ---------------------------------------------------------------- 列表变更留痕
-- ★ 列表页那一趟**看到了什么、和上次比变了什么**（2026-10-23 加）
--   为什么必须留：列表采集是「upsert 覆盖」——
--   旧值被新值覆盖后就**永远查不到了**，
--   于是「这帖站方什么时候受理的 / 什么时候回的」这类问题无从追查。
--   增量检测（detect_reply_growth）只能在**覆盖前那一瞬间**比一次，
--   比完就没了；这张表把那一瞬间**存下来**。
CREATE TABLE IF NOT EXISTS list_changes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    source  TEXT,     -- global / dept:495 / group:func …
    tid     INTEGER,
    kind    TEXT,     -- new 新帖 / state 受理状态变了 / replies 回复数变了 / title 标题改了
    old_val TEXT,
    new_val TEXT,
    note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_lchg_tid ON list_changes(tid);
CREATE INDEX IF NOT EXISTS idx_lchg_ts  ON list_changes(ts);
CREATE INDEX IF NOT EXISTS idx_lchg_src ON list_changes(source);
CREATE INDEX IF NOT EXISTS idx_lchg_kind ON list_changes(kind);

-- ---------------------------------------------------------------- 词典
CREATE TABLE IF NOT EXISTS depts (
    typeid     TEXT PRIMARY KEY,
    name       TEXT,
    group_code TEXT,
    group_name TEXT,
    is_district INTEGER DEFAULT 0,
    url        TEXT,
    updated_at TEXT,
    py_initials TEXT
);

CREATE TABLE IF NOT EXISTS districts (
    typeid     TEXT PRIMARY KEY,
    name       TEXT,
    last_month_msgs    INTEGER,
    last_month_accepted INTEGER,
    total_msgs         INTEGER,
    total_accepted     INTEGER,
    updated_at TEXT,
    py_initials TEXT
);

CREATE TABLE IF NOT EXISTS site_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT,
    total_msgs  INTEGER, this_year_msgs INTEGER, last_month_msgs INTEGER,
    total_accepted INTEGER, this_year_accepted INTEGER, last_month_accepted INTEGER,
    raw         TEXT
);

-- ---------------------------------------------------------------- 断点续传
CREATE TABLE IF NOT EXISTS list_watermark (
    source      TEXT PRIMARY KEY,
    dim         TEXT, code TEXT, name TEXT, url TEXT,
    last_page   INTEGER DEFAULT 0,
    max_page    INTEGER DEFAULT 0,
    rows_total  INTEGER DEFAULT 0,
    oldest_ts   INTEGER,
    newest_ts   INTEGER,
    status      TEXT DEFAULT 'pending',   -- pending/in_progress/done/paused
    -- 漂移（页码漂移）诊断用：
    --   last_tid    上一批最后一页的末行 tid → 续采时比对首行 tid，判定页码是否漂移
    --   drift_hits  累计检测到的漂移次数
    --   rescan_rows 补漏回扫补回的帖子数
    last_tid    TEXT,
    drift_hits  INTEGER DEFAULT 0,
    rescan_rows INTEGER DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS detail_queue (
    tid         INTEGER PRIMARY KEY,
    state       TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/skipped
    attempts    INTEGER DEFAULT 0,
    last_error  TEXT,
    pages_done  INTEGER DEFAULT 0,
    pages_total INTEGER DEFAULT 0,
    enqueued_at TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_dq_state ON detail_queue(state, tid);

-- 每页楼层 pid 清单 —— 完整性与串帖核查的凭据
CREATE TABLE IF NOT EXISTS thread_pages (
    tid        INTEGER NOT NULL,
    page_no    INTEGER NOT NULL,
    pids       TEXT,
    n          INTEGER,
    status     TEXT,
    fetched_at TEXT,
    PRIMARY KEY (tid, page_no)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT,
    phase  TEXT,
    target TEXT,
    kind   TEXT,
    ok     INTEGER,
    msg    TEXT
);
CREATE INDEX IF NOT EXISTS idx_flog_phase ON fetch_log(phase, ts);

-- ---------------------------------------------------------------- 成员（Beta）
CREATE TABLE IF NOT EXISTS members (
    id         INTEGER PRIMARY KEY,
    email      TEXT UNIQUE NOT NULL,
    pw_hash    TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'member',   -- admin | member
    node_id    TEXT,                            -- 采集节点标识；登录时自动分配
    status     TEXT DEFAULT 'active',           -- active | disabled
    quota      INTEGER DEFAULT 50,              -- 单次最多领多少帖
    note       TEXT,
    last_login TEXT,
    created_at TEXT
);

-- ---------------------------------------------------------------- 协作任务
CREATE TABLE IF NOT EXISTS assist_tasks (
    id         INTEGER PRIMARY KEY,
    code       TEXT UNIQUE NOT NULL,             -- 编号：A1 / A2 / A3…
    title      TEXT,
    dept_ids   TEXT,                            -- 逗号分隔的部门 typeid
    total      INTEGER DEFAULT 0,               -- 目标帖数
    done       INTEGER DEFAULT 0,
    state      TEXT DEFAULT 'open',              -- open | active | done | closed
    created_by TEXT,                            -- 发起人（成员 email）
    created_at TEXT,
    updated_at TEXT
);

-- 协作任务里被拆成单帖的活；成员领走具体 tid
CREATE TABLE IF NOT EXISTS assist_picks (
    id        INTEGER PRIMARY KEY,
    task_code TEXT NOT NULL,
    tid       INTEGER NOT NULL,
    node      TEXT,                             -- 领走的节点
    state     TEXT DEFAULT 'pending',            -- pending | taken | done | failed
    posts     INTEGER DEFAULT 0,
    chars     INTEGER DEFAULT 0,
    taken_at  TEXT,
    done_at   TEXT,
    UNIQUE(task_code, tid)
);
CREATE INDEX IF NOT EXISTS idx_ap_state ON assist_picks(state, task_code);
CREATE INDEX IF NOT EXISTS idx_ap_node ON assist_picks(node, state);

-- ---------------------------------------------------------------- 协作回传冲突
-- ★ 「重复的以我们本地的为准」要**可查**，不能只写一句日志。
--   每次回传，凡是本地已有 / 内容不一致 / 楼层数对不上，都在这里留一行。
CREATE TABLE IF NOT EXISTS assist_conflicts (
    id         INTEGER PRIMARY KEY,
    task_code  TEXT,
    tid        INTEGER,
    node       TEXT,                             -- 回传的节点
    kind       TEXT,                             -- local_done | dup_pid | content_diff | count_diff
    reason     TEXT,
    local_val  TEXT,                             -- 本地已有的值（摘要）
    remote_val TEXT,                             -- 成员回传的值（摘要）
    resolved   INTEGER DEFAULT 0,                -- 0=已按本地为准处理 1=人工处理过
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ac_tid ON assist_conflicts(tid);
CREATE INDEX IF NOT EXISTS idx_ac_code ON assist_conflicts(task_code);

-- ---------------------------------------------------------------- 启动票据
-- ★ 成员端的启动包是**网站实时生成并下载**的，里面不带密码，只带一张
--   **一次性票据**：agent 启动时用它换 JWT（用完即焚、限时）。
--   这样「网址是动态的」（生成时注入当前地址）与「密码不落成员磁盘」两件事同时成立。
CREATE TABLE IF NOT EXISTS agent_tickets (
    ticket     TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    node_id    TEXT,
    server     TEXT,
    expires_at INTEGER,
    used_at    TEXT,
    created_at TEXT,
    -- ★ repeatable（2026-10-23 加）：1 = 「包用长期凭证」
    --   0 = 一次性票据（网页签发、用一次作废，防泄露后被复用）
    --   1 = 启动包里的凭证：30 天有效、**可重复兑换**，
    --       否则组员第二次运行包、或晚点运行，就没凭证了。
    --   安全闸不变：账号被停用（status != active）仍然兑换失败。
    repeatable INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_at_exp ON agent_tickets(expires_at);

-- ---------------------------------------------------------------- 体检结果
-- ★ 2026-10-28 加：体检结果**不再整份挂在 job.result 上**。
--   原因（实测）：60 万帖量级时，那一份 JSON 有几百 MB ——
--   服务端序列化一份、前端 JSON.parse 另一份、浏览器再一次性 innerHTML
--   出几十万行 <tr> → 标签页直接被撑爆（DOM 爆 + payload 爆，两处）。
--   改成：体检**逐条**写进下面这两张表，接口按类分页取。
--   · integrity_runs  —— 每次体检一行：检查数、通过数、各类条数（摘要）
--   · integrity_items —— 明细，一行一条（run_id + kind + 序号 + tid + JSON）
CREATE TABLE IF NOT EXISTS integrity_runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT,
    checked    INTEGER DEFAULT 0,
    ok         INTEGER DEFAULT 0,
    counts     TEXT
);
CREATE TABLE IF NOT EXISTS integrity_items (
    run_id  TEXT    NOT NULL,
    kind    TEXT    NOT NULL,   -- problems / growth / warnings / restricted
    ord     INTEGER NOT NULL,  -- 同一类里的稳定序号（保证翻页不重不漏）
    tid     INTEGER NOT NULL,
    payload TEXT    NOT NULL,   -- 该条的 JSON（字段少，体积小）
    PRIMARY KEY (run_id, kind, ord)
);
CREATE INDEX IF NOT EXISTS idx_int_items_tid ON integrity_items(run_id, kind, tid);

-- ---------------------------------------------------------------- FTS5
CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts USING fts5(
    title,
    dept_name,
    content='threads',
    content_rowid='tid',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS threads_ai AFTER INSERT ON threads BEGIN
    INSERT INTO threads_fts(rowid, title, dept_name)
    VALUES (new.tid, new.title, new.dept_name);
END;
CREATE TRIGGER IF NOT EXISTS threads_ad AFTER DELETE ON threads BEGIN
    INSERT INTO threads_fts(threads_fts, rowid, title, dept_name)
    VALUES ('delete', old.tid, old.title, old.dept_name);
END;
CREATE TRIGGER IF NOT EXISTS threads_au AFTER UPDATE ON threads BEGIN
    INSERT INTO threads_fts(threads_fts, rowid, title, dept_name)
    VALUES ('delete', old.tid, old.title, old.dept_name);
    INSERT INTO threads_fts(rowid, title, dept_name)
    VALUES (new.tid, new.title, new.dept_name);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    content,
    content='posts',
    content_rowid='pid',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, content) VALUES (new.pid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, content)
    VALUES ('delete', old.pid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, content)
    VALUES ('delete', old.pid, old.content);
    INSERT INTO posts_fts(rowid, content) VALUES (new.pid, new.content);
END;
"""


# ------------------------------------------------------------------ 切词
def _split_query(q: str) -> List[str]:
    """按显式分隔符切词（用户自己打了空格/逗号的场景）。"""
    return [t for t in re.split(r'[\s,，、;；]+', (q or "").strip()) if t]


def _tokenize_for_search(q: str, tokenize: bool) -> List[str]:
    """切词：先按显式分隔符切，tokenize=True 时再把每个长串交给 jieba。

    没有 jieba 就退回不切 —— 装了才切，没装不报错。
    """
    toks = _split_query(q)
    if not tokenize or not toks:
        return toks
    try:
        import jieba  # type: ignore
    except Exception:
        return toks
    out: List[str] = []
    for t in toks:
        if len(t) >= 2:
            out.extend([x.strip() for x in jieba.cut_for_search(t) if x.strip()])
        else:
            out.append(t)
    return out or toks


class _WriteLock:
    """写锁：**进入时先把可能存在的读事务提交掉**。

    ★ 为什么需要这个（2026-10-23 实测踩过）
      Python 的 sqlite3 默认是 `isolation_level=""`（deferred）：
      一条 SELECT 就会**开一个读事务**，直到下次 commit 才结束。
      于是「先查一下、再写一下」的代码路径会变成
      **「读事务升级为写」** —— 而这恰恰是 SQLite **唯一不走 busy handler**
      的情形：别的进程正写库时，它**不等**，立刻抛
      `database is locked`（→ HTTP 500）。

      实测症状：`api_login` 先 `SELECT * FROM members` 再
      `UPDATE members SET last_login` → 与别的进程并发时**登录整片 500**。
      「登录」是命脉接口，不能因为并发写就挂。

    ★ 怎么根治
      进入写块时先 `commit()` —— 读事务结束，
      后面的写就是**开一个新写事务**，会正常走 busy handler（等 30 秒）。

    ★ 用起来跟 RLock 一样：`with db.lock:` —— 所以现有代码一行不用改。
    """

    def __init__(self, rlock: threading.RLock, conn: sqlite3.Connection):
        self._rlock = rlock
        self._conn = conn

    def acquire(self) -> None:
        self._rlock.acquire()

    def release(self) -> None:
        self._rlock.release()

    def __enter__(self):
        self._rlock.acquire()
        try:
            # 结束可能存在的读事务（没有读事务时 commit 是空操作）
            self._conn.commit()
        except Exception:                    # noqa: BLE001
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc) -> bool:
        self._rlock.release()
        return False


class DB:
    """线程安全（单机单用户）的 SQLite 封装。"""

    # ★ 进程级**共享**写锁表（2026-10-23 补，治并发写 500 的病根）
    #   为什么需要：webapp 的 get_db 是 thread-local —— **每个线程一条连接**。
    #   如果锁挂在实例上，每个线程就各有一把锁，`with self.lock:` 只能防住
    #   「同一线程重入」，**完全挡不住两个线程同时写**。
    #   于是并发写全靠 SQLite 自己扛，而「读事务中途升级为写」这种情形
    #   SQLite **不走 busy handler**，直接抛 `database is locked` → HTTP 500
    #   （实测：管理员改密码被 agent 心跳顶掉 → 整个改密码流程 500，
    #     agent 接着把 5xx 判成「失联」→ 自己停了）。
    #   对策：同一进程内**指向同一个库文件的连接共用一把锁**，
    #   写事务真正串行化。读不占用这把锁（WAL 下读可以并发）。
    #   注意：锁只保护**本进程**；跨进程（比如 CLI 同时在跑）仍靠 SQLite 的
    #   busy_timeout + 上层 _retry_locked 兜底。
    _shared_locks: Dict[str, "threading.RLock"] = {}
    _shared_locks_guard = threading.Lock()

    @classmethod
    def _shared_lock(cls, path: str) -> "threading.RLock":
        key = str(os.path.abspath(path)) if path != ":memory:" else ":memory:"
        with cls._shared_locks_guard:
            lk = cls._shared_locks.get(key)
            if lk is None:
                lk = threading.RLock()
                cls._shared_locks[key] = lk
            return lk

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # ★ 显式 busy_timeout（2026-10-23 补）
        #   服务端是**多线程各自持一条连接**（get_db 是 thread-local），
        #   同一时刻可能有两条连接在写 —— WAL 下写是串行的，
        #   后一条要等前一条提交。connect(timeout=30) 本该覆盖这个，
        #   但**「读事务中途升级为写」**的情形 SQLite 不会走 busy handler，
        #   会直接抛 `database is locked`（HTTP 500，
        #   实测把 agent 打成「失联自动停止」）。
        #   显式设置一次，别指望默认值。
        self.conn.execute("PRAGMA busy_timeout=30000")
        # ★ 关键：锁必须是**进程级共享**的（见类注释「_shared_locks」）。
        #   早先这里写的是 threading.RLock()（每实例一把），
        #   对 thread-local 的多连接场景等于没锁 → 并发写 500。
        # ★ 而且是**写锁**（_WriteLock）：进入时先把可能存在的读事务提交掉。
        #   原因见 _WriteLock 的类注释 —— 这是「读事务升级为写」那个
        #   SQLite 唯一不走 busy handler 的坑的根治办法。
        self.lock = _WriteLock(self._shared_lock(path), self.conn)
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """轻量迁移：给已存在的库补上后加的列（不重建、不丢数据）。"""
        wanted = {
            "threads": {
                "detail_warn": "TEXT",
                "dept_group": "TEXT",
                "dept_district": "TEXT",
                "pub_day": "TEXT",
            },
            "list_watermark": {
                "last_tid": "TEXT",
                "drift_hits": "INTEGER DEFAULT 0",
                "rescan_rows": "INTEGER DEFAULT 0",
            },
            "members": {
                # 成员最后心跳时间 —— 用来在成员名单上显示「在线 / 离线」
                "last_seen": "TEXT",
            },
            "agent_tickets": {
                # ★ repeatable（2026-10-23）：启动包里的凭证要能**反复兑换**，
                #   否则组员第二次运行包、或晚点运行，就没凭证了。
                #   0 = 一次性票据（网页签发，防泄露后被复用）
                #   1 = 包用长期凭证（30 天，可重复）
                "repeatable": "INTEGER DEFAULT 0",
            },
            "depts": {
                # ★ 拼音首字母（2026-10-24）：支持「输入名称、拼音或首字母」检索
                "py_initials": "TEXT",
            },
            "districts": {
                "py_initials": "TEXT",
            },
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    # ---------------------------------------------------------------- 日志
    def log(self, phase: str, target: str, kind: str, ok: bool = True,
            msg: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO fetch_log(ts,phase,target,kind,ok,msg) VALUES(?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), phase, target, kind,
                 1 if ok else 0, msg[:500]))
            self.conn.commit()

    # ---------------------------------------------------------------- 帖
    THREAD_COLS = (
        "tid,title,url,category,state,dept_id,dept_name,dept_group,dept_district,"
        "extra_dept_ids,extra_dept_names,mailbox_id,mailbox_name,light_code,light_name,"
        "author,author_uid,pub_time,pub_day,pub_ts,replies,views,has_image,"
        "last_reply_by,last_reply_time,last_reply_ts"
    )

    def upsert_threads(self, rows: Iterable) -> int:
        """写入列表页元数据。**不覆盖**详情页补全的字段。

        ★ 公告行（`#msbgt`）**只允许填空，不允许覆盖**（2026-10-24 加）
          理由：同一个 tid 在站方不同板块列表页里，**站方给的标题不一样**
            · 主板块置顶行：「必读公告：中国宁波网民生e点通（天一论坛"我要问政"）管理规定」
            · 公告区（每个板块页都有）：「中国宁波网民生e点通群众留言板（…）管理规定」
          而公告行除标题外，**author / replies / pub_time / 部门…全是空**。
          若按普通行那样覆盖式写入，就会把帖行的真值**清成空**
          （实测 6177045 的 author、replies 被清成空/0，标题也被换成简称）。
          → 公告行走另一条 SQL：冲突时**只补空字段**，已有真值一律保留。
          → 帖行（正常行）仍照常覆盖 —— 它的信息本来就是最新的。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        with self.lock:
            for r in rows:
                vals = (r.tid, r.title, r.url, r.category, r.state,
                        r.dept_id, r.dept_name, None, None,
                        ",".join(r.extra_dept_ids), ",".join(r.extra_dept_names),
                        r.mailbox_id, r.mailbox_name, r.light_code, r.light_name,
                        r.author, r.author_uid, r.pub_time,
                        (r.pub_time or "")[:10], r.pub_ts, r.replies, r.views,
                        r.has_image, r.last_reply_by, r.last_reply_time,
                        r.last_reply_ts)
                if getattr(r, "is_announcement", 0):
                    # 公告行：**只填空**，不覆盖已有真值
                    cur = self.conn.execute(
                        f"""INSERT INTO threads ({self.THREAD_COLS})
                            VALUES ({','.join('?' * len(vals))})
                            ON CONFLICT(tid) DO UPDATE SET
                              title=CASE WHEN COALESCE(threads.title,'')=''
                                         THEN excluded.title ELSE threads.title END,
                              url=CASE WHEN COALESCE(threads.url,'')=''
                                       THEN excluded.url ELSE threads.url END,
                              author=CASE WHEN COALESCE(threads.author,'')=''
                                          THEN excluded.author ELSE threads.author END,
                              author_uid=CASE WHEN COALESCE(threads.author_uid,'')=''
                                              THEN excluded.author_uid
                                              ELSE threads.author_uid END,
                              pub_time=CASE WHEN COALESCE(threads.pub_time,'')=''
                                            THEN excluded.pub_time ELSE threads.pub_time END,
                              pub_day=CASE WHEN COALESCE(threads.pub_day,'')=''
                                          THEN excluded.pub_day ELSE threads.pub_day END,
                              pub_ts=CASE WHEN COALESCE(threads.pub_ts,0)=0
                                          THEN excluded.pub_ts ELSE threads.pub_ts END,
                              replies=CASE WHEN COALESCE(threads.replies,0)=0
                                           THEN excluded.replies ELSE threads.replies END,
                              views=CASE WHEN COALESCE(threads.views,0)=0
                                         THEN excluded.views ELSE threads.views END,
                              dept_id=CASE WHEN COALESCE(threads.dept_id,'')=''
                                           THEN excluded.dept_id ELSE threads.dept_id END,
                              dept_name=CASE WHEN COALESCE(threads.dept_name,'')=''
                                             THEN excluded.dept_name
                                             ELSE threads.dept_name END,
                              category=CASE WHEN COALESCE(threads.category,'')=''
                                            THEN excluded.category
                                            ELSE threads.category END,
                              state=CASE WHEN COALESCE(threads.state,'')=''
                                         THEN excluded.state ELSE threads.state END,
                              updated_at=?""",
                        vals + (now,))
                else:
                    cur = self.conn.execute(
                        f"""INSERT INTO threads ({self.THREAD_COLS})
                            VALUES ({','.join('?' * len(vals))})
                            ON CONFLICT(tid) DO UPDATE SET
                              title=excluded.title, url=excluded.url,
                              category=excluded.category, state=excluded.state,
                              dept_id=excluded.dept_id, dept_name=excluded.dept_name,
                              extra_dept_ids=excluded.extra_dept_ids,
                              extra_dept_names=excluded.extra_dept_names,
                              mailbox_id=excluded.mailbox_id, mailbox_name=excluded.mailbox_name,
                              light_code=excluded.light_code, light_name=excluded.light_name,
                              author=excluded.author, pub_time=excluded.pub_time,
                              pub_day=excluded.pub_day, pub_ts=excluded.pub_ts,
                              replies=excluded.replies, views=excluded.views,
                              has_image=excluded.has_image,
                              last_reply_by=excluded.last_reply_by,
                              last_reply_time=excluded.last_reply_time,
                              last_reply_ts=excluded.last_reply_ts,
                              updated_at=?""",
                        vals + (now,))
                n += cur.rowcount or 0
            self.conn.commit()
        return n

    def set_thread_detail(self, tid: int, *, detail_state: str = "",
                          first_post_id: Optional[int] = None,
                          last_post_id: Optional[int] = None,
                          pages_total: Optional[int] = None,
                          posts_n: Optional[int] = None,
                          chars: Optional[int] = None,
                          dept_id: str = "", dept_name: str = "",
                          dept_group: str = "", dept_district: str = "") -> None:
        sets, args = [], []
        if detail_state:
            sets.append("detail_state=?"); args.append(detail_state)
        if first_post_id is not None:
            sets.append("first_post_id=?"); args.append(first_post_id)
        if last_post_id is not None:
            sets.append("last_post_id=?"); args.append(last_post_id)
        if pages_total is not None:
            sets.append("pages_total=?"); args.append(pages_total)
        if posts_n is not None:
            sets.append("posts_n=?"); args.append(posts_n)
        if chars is not None:
            sets.append("chars=?"); args.append(chars)
        if dept_id:
            sets.append("dept_id=?"); args.append(dept_id)
        if dept_name:
            sets.append("dept_name=?"); args.append(dept_name)
        if dept_group:
            sets.append("dept_group=?"); args.append(dept_group)
        if dept_district:
            sets.append("dept_district=?"); args.append(dept_district)
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(time.strftime("%Y-%m-%d %H:%M:%S"))
        args.append(tid)
        with self.lock:
            self.conn.execute(f"UPDATE threads SET {','.join(sets)} WHERE tid=?", args)
            self.conn.commit()

    def backfill_dept_meta(self) -> int:
        """用部门字典**全量重算**帖子的「分组」与「区县市」。

        为什么需要：列表页的每一行只给 typeid，不给分组，也不给区县市；
        这两样都要等 taxonomy 抓完部门目录才知道。
        区县市尤其麻烦 —— 主部门可能是「市公安局」，而这条帖其实投给了江北区，
        区县市只出现在 typewrapmulti 里。

        ⚠ 必须**全量重算**，不能只补空值：
          早期版本用「只补 dept_group 为空的行」来省事，结果 set_dept_meta
          提前把 dept_group 填满、dept_district 留空，回填就被整片跳过 ——
          分组对了、区县市几乎全空。现在改成算一遍、只写有差异的行。
        """
        depts = {r["typeid"]: dict(r) for r in self.conn.execute(
            "SELECT typeid,name,group_code,is_district FROM depts")}
        if not depts:
            return 0
        district_names = {t: d["name"] for t, d in depts.items() if d.get("is_district")}
        rows = self.conn.execute(
            "SELECT tid, dept_id, extra_dept_ids, dept_group, dept_district "
            "FROM threads").fetchall()
        updates = []
        for r in rows:
            d = depts.get(r["dept_id"] or "")
            group = (d.get("group_code") or "") if d else ""
            district = ""
            if d and d.get("is_district"):
                district = d.get("name") or ""
            if not district:
                for t in (r["extra_dept_ids"] or "").split(","):
                    t = t.strip()
                    if t in district_names:
                        district = district_names[t]
                        break
            if (group != (r["dept_group"] or "")
                    or district != (r["dept_district"] or "")):
                updates.append((group, district, r["tid"]))
        if updates:
            with self.lock:
                self.conn.executemany(
                    "UPDATE threads SET dept_group=?, dept_district=? WHERE tid=?",
                    updates)
                self.conn.commit()
        return len(updates)

    def set_detail_warn(self, tid: int, warn: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE threads SET detail_warn=?, updated_at=? WHERE tid=?",
                (warn[:500], time.strftime("%Y-%m-%d %H:%M:%S"), tid))
            self.conn.commit()

    def get_detail_warn(self, tid: int) -> str:
        """读某个帖的告警文本（没有就是空串）。

        ★ 为什么需要（2026-10-23 晚上加）：
          「少贴 → 标记待补采」要判断**这个帖补采过没有** ——
          补采后仍不自洽（站方计数口径本来就有偏差）就**不能无限重采**。
          补采次数就记在 detail_warn 里（「已补采 N 次」）。
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT detail_warn FROM threads WHERE tid=?", (tid,)).fetchone()
        return (row["detail_warn"] or "") if row else ""

    # ---------------------------------------------------------------- 增量
    # ---------------------------------------------------------------- 列表变更留痕
    #: 站方给的三种受理状态（空 = 站方没标）
    STATE_VALUES = ("已回复", "已受理", "未受理")

    @staticmethod
    def _row_val(r, name, default=None):
        """行对象两种形态都支持：dict 与带属性的对象（ListRow / sqlite3.Row）。"""
        if isinstance(r, dict):
            return r.get(name, default)
        return getattr(r, name, default)

    def record_list_changes(self, source: str, rows: Iterable,
                            ) -> Dict[str, List[Dict[str, Any]]]:
        """比对列表行与库里的旧值，**把变化记下来**。

        ★ 必须在 `upsert_threads` **之前**调用
          upsert 是覆盖式写入，旧值被盖掉之后就再也比不出来了。
          所以这一步是「**覆盖前留个底**」。

        记四类变化：

        | kind | 含义 | 为什么值得记 |
        |---|---|---|
        | `new` | 这个 tid 第一次出现在这个源 | 新帖，是舆情流量的源头 |
        | `state` | **受理状态**变了 | ★ 受理状态一变，站方的**受理流程**就多/变了一步 → 详情页那份流程过时了 |
        | `replies` | 回复数变了 | 站方追加了楼层 → 正文过时（这是已有的增量信号） |
        | `title` | 标题被改了 | 站方可能合并/重命名帖子 |

        ⚠ **浏览数（views）不记**：它每趟都在涨，记下来全是噪音。
        """
        rows = list(rows)
        if not rows:
            return {}
        tids = []
        for r in rows:
            t = self._row_val(r, "tid")
            if t is not None:
                tids.append(int(t))
        if not tids:
            return {}
        q = ",".join("?" * len(tids))
        old = {int(r["tid"]): r for r in self.conn.execute(
            f"SELECT tid,title,state,replies FROM threads WHERE tid IN ({q})",
            tids)}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        out: Dict[str, List[Dict[str, Any]]] = {}
        records: List[tuple] = []
        for r in rows:
            tid = self._row_val(r, "tid")
            if tid is None:
                continue
            tid = int(tid)
            o = old.get(tid)
            if o is None:
                out.setdefault("new", []).append(
                    {"tid": tid, "old": "", "new": str(self._row_val(r, "title", ""))})
                records.append((now, source, tid, "new", "",
                                str(self._row_val(r, "title", ""))[:120],
                                "该帖首次出现在这个源"))
                continue
            # ---- 受理状态 ----
            new_state = str(self._row_val(r, "state", "") or "")
            old_state = str(o["state"] or "")
            if new_state != old_state:
                out.setdefault("state", []).append(
                    {"tid": tid, "old": old_state, "new": new_state})
                records.append((now, source, tid, "state", old_state, new_state,
                                "受理状态变化 → 受理流程随之变化，详情待重采"))
            # ---- 回复数 ----
            new_rep = int(self._row_val(r, "replies", 0) or 0)
            old_rep = int(o["replies"] or 0)
            if new_rep != old_rep:
                out.setdefault("replies", []).append(
                    {"tid": tid, "old": str(old_rep), "new": str(new_rep)})
                records.append((now, source, tid, "replies", str(old_rep),
                                str(new_rep), "回复数变化"))
            # ---- 标题 ----
            new_title = str(self._row_val(r, "title", "") or "")
            old_title = str(o["title"] or "")
            if new_title and old_title and new_title != old_title:
                out.setdefault("title", []).append(
                    {"tid": tid, "old": old_title, "new": new_title})
                records.append((now, source, tid, "title", old_title[:120],
                                new_title[:120], "标题被修改"))
        if records:
            with self.lock:
                self.conn.executemany(
                    "INSERT INTO list_changes(ts,source,tid,kind,old_val,new_val,note) "
                    "VALUES(?,?,?,?,?,?,?)", records)
                self.conn.commit()
        return out

    def list_changes(self, limit: int = 200, kind: str = "",
                     source: str = "") -> List[Dict[str, Any]]:
        """查列表阶段的变更留痕（最近若干条）。"""
        sql = "SELECT * FROM list_changes"
        cond, args = [], []
        if kind:
            cond.append("kind=?")
            args.append(kind)
        if source:
            cond.append("source=?")
            args.append(source)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ---------------------------------------------------------- 体检结果
    # ★ 2026-10-28：体检明细落库 + 按类分页取。
    #   之前整份挂在 job.result 上 → 60 万帖量级时几百 MB，
    #   服务端一份、前端一份、浏览器再一次性 innerHTML 撑爆标签页。

    #: 体检明细的四个类别（接口白名单用）
    #: ★ 2026-10-29 下线第五类 `count_mismatch`：
    #:   那个状态的唯一语义是「站方列表计数虚高导致的**假**少贴」，
    #:   而这批帖的正文**本来就采全了、站方也照样公开**（实测 25 条：
    #:   站方锚点 == ajax 正文 == 本地实存楼层，pid 集合逐个对得上）。
    #:   把它们记成「不计入已采详情帖」= **账面上说它们没采到**，
    #:   而正文明明在库里 —— 这才是真正的逻辑错误。
    #:   → 正确处置是**正文齐全即已采**（归为 `done`），
    #:     留痕写在 `detail_warn` 里，不另立状态。详见方案报告 §9.54。
    INTEGRITY_KINDS = ("problems", "growth", "warnings", "restricted")

    def integrity_begin_run(self, run_id: str) -> None:
        """开一次新体检：**清掉上一次的明细**（只保留最新一次）。"""
        with self.lock:
            self.conn.execute("DELETE FROM integrity_items")
            self.conn.execute("DELETE FROM integrity_runs")
            self.conn.execute(
                "INSERT OR REPLACE INTO integrity_runs(run_id,created_at,checked,ok,counts)"
                " VALUES(?,?,0,0,'{}')",
                (run_id, time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def integrity_add_items(self, run_id: str, kind: str,
                            rows: List[Tuple[int, int, str]]) -> int:
        """批量写一批体检明细：rows = [(ord, tid, payload_json), ...]。

        ★ 必须**分批**写：一次几十万条会撑爆内存（这正是本次要修的问题）。
        """
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO integrity_items(run_id,kind,ord,tid,payload)"
                " VALUES(?,?,?,?,?)",
                [(run_id, kind, int(o), int(t), p) for o, t, p in rows])
            self.conn.commit()
        return len(rows)

    def integrity_finish_run(self, run_id: str, checked: int, ok: int,
                              counts: Dict[str, int]) -> None:
        """体检跑完：把摘要写回 runs 表。"""
        with self.lock:
            self.conn.execute(
                "UPDATE integrity_runs SET checked=?, ok=?, counts=? WHERE run_id=?",
                (int(checked), int(ok), json.dumps(counts, ensure_ascii=False), run_id))
            self.conn.commit()

    def integrity_latest_run(self) -> Optional[Dict[str, Any]]:
        """最近一次体检的摘要（没有就跑过 → None）。"""
        row = self.conn.execute(
            "SELECT run_id, created_at, checked, ok, counts FROM integrity_runs"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            counts = json.loads(row["counts"] or "{}")
        except Exception:                                     # noqa: BLE001
            counts = {}
        return {"run_id": row["run_id"], "created_at": row["created_at"],
                "checked": row["checked"], "ok": row["ok"], "counts": counts}

    def integrity_page(self, run_id: str, kind: str, page: int = 1,
                       page_size: int = 50) -> Dict[str, Any]:
        """取某一类的**一页**明细（不把整类读进内存）。"""
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        total = self.conn.execute(
            "SELECT COUNT(*) n FROM integrity_items WHERE run_id=? AND kind=?",
            (run_id, kind)).fetchone()["n"]
        rows = self.conn.execute(
            "SELECT payload FROM integrity_items WHERE run_id=? AND kind=?"
            " ORDER BY ord LIMIT ? OFFSET ?",
            (run_id, kind, page_size, (page - 1) * page_size)).fetchall()
        items = []
        for r in rows:
            try:
                items.append(json.loads(r["payload"]))
            except Exception:                                 # noqa: BLE001
                continue
        pages = (total + page_size - 1) // page_size
        return {"kind": kind, "total": total, "page": page,
                "page_size": page_size, "pages": pages, "items": items}

    def integrity_tids(self, run_id: str, kind: str) -> List[int]:
        """取某一类的**全部 tid**（只回数字数组，比拉全量明细轻两个数量级）。"""
        return [int(r["tid"]) for r in self.conn.execute(
            "SELECT tid FROM integrity_items WHERE run_id=? AND kind=? ORDER BY ord",
            (run_id, kind))]

    def detect_reply_growth(self, rows: Iterable) -> List[Dict[str, Any]]:
        """找出「站方又回复了」**或「受理状态变了」**的帖。

        ★ 增量的**权威信号**是站方自己给的数字与状态：列表页每一行都带
          「回复数」和「受理状态」。
          同一个 tid 再次被列表页看到，如果

            ① `replies` **变大了** → 站方追加了新楼层 → 本地正文**过时了**；
            ② **受理状态变了**（两个都是有效状态）→ 站方受理/回复了
               → **详情页那份「受理流程时间线」必然过时**
               （受理流程是详情页的字段，见 §4.3），本地那份少了新的一步。

          ★ 为什么②必须单列（2026-10-23 用户提醒后补的）
            「未受理 → 已受理」这个动作**回复数不变**（站方受理了但还没回复），
            旧实现只比 replies → **完全检测不到**。
            而这恰恰是舆情分析里最关键的状态推进：群众等的就是"被受理"。
            只看回复数，等于只看得见"被回复"，看不见"被受理"。

        ⚠ 只在「变大」时判定 replies；受理状态则要求**两边都是有效状态**
          且不同。「空 → 有值」不判为增量（老帖在某些列表页有状态、某些页没有，
          那是渲染差异不是站方动作）—— 但它**会被记进列表变更留痕**，
          需要时查得到。

        返回 [{tid, old_replies, new_replies, detail_state, kind, old_state, new_state}]。
        """
        out: List[Dict[str, Any]] = []
        rows = list(rows)
        if not rows:
            return out

        def val(r, name, default=None):
            """行对象两种形态都支持：dict 与带属性的对象（ListRow / sqlite3.Row）。"""
            if isinstance(r, dict):
                return r.get(name, default)
            return getattr(r, name, default)

        tids = []
        for r in rows:
            t = val(r, "tid")
            if t is not None:
                tids.append(int(t))
        if not tids:
            return out
        q = ",".join("?" * len(tids))
        old = {int(r["tid"]): (int(r["replies"] or 0), r["detail_state"],
                              str(r["state"] or ""))
               for r in self.conn.execute(
                   f"SELECT tid,replies,detail_state,state FROM threads WHERE tid IN ({q})",
                   tids)}
        for r in rows:
            tid = val(r, "tid")
            if tid is None:
                continue
            tid = int(tid)
            new_rep = int(val(r, "replies", 0) or 0)
            if tid not in old:
                continue
            old_rep, state, old_state = old[tid]
            if new_rep > old_rep and (state or "pending") == "done":
                out.append({"tid": tid, "old_replies": old_rep,
                            "new_replies": new_rep, "detail_state": state,
                            "kind": "replies", "old_state": old_state,
                            "new_state": str(val(r, "state", "") or "")})
                continue
            # ---- 受理状态变了 → 受理流程随之变化 → 详情待重采 ----
            new_state = str(val(r, "state", "") or "")
            if (new_state in self.STATE_VALUES and old_state in self.STATE_VALUES
                    and new_state != old_state
                    and (state or "pending") == "done"):
                out.append({"tid": tid, "old_replies": old_rep,
                            "new_replies": new_rep, "detail_state": state,
                            "kind": "state", "old_state": old_state,
                            "new_state": new_state})
        return out

    def mark_detail_stale(self, tids: Iterable[int], reason: str = "") -> int:
        """把「站方有新回复」的帖标记为**待重采**。

        ★ 新增状态 `stale`（内容过时，待更新）：
          · `threads.detail_state = 'stale'` —— 界面显示「待更新」而不是「已完成」
          · `detail_queue` 置回 `pending`（attempts 归零、pages_done 归零）
            → 下一次详情采集任务会自动把它捡走重采

        ⚠ 为什么不是直接删数据：重采走的是**同一条合并路径**，
          新楼层会被加进来、旧楼层被更新 —— 不需要清空重来。
        """
        tids = [int(t) for t in tids]
        if not tids:
            return 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        with self.lock:
            for tid in tids:
                cur = self.conn.execute(
                    "UPDATE threads SET detail_state='stale', updated_at=? WHERE tid=?",
                    (now, tid))
                if not cur.rowcount:
                    continue
                self.conn.execute(
                    "INSERT INTO detail_queue(tid,state,attempts,pages_done,"
                    "last_error,enqueued_at,updated_at) VALUES(?,'pending',0,0,?,?,?) "
                    "ON CONFLICT(tid) DO UPDATE SET state='pending', attempts=0, "
                    "pages_done=0, last_error=excluded.last_error, updated_at=excluded.updated_at",
                    (tid, reason[:500], now, now))
                n += 1
            self.conn.commit()
        return n

    def retry_details(self, tids: Iterable[int], reason: str = "人工重跑") -> Dict[str, Any]:
        """**重跑**：把选中的帖重置为待采（软失败 —— 数据留着，重新采一遍）。

        与 `mark_detail_stale` 的区别：那个是「站方有新回复」自动触发，
        这个是管理员在体检页手动点的。两者效果一致：丢回队列，重新采。
        """
        tids = [int(t) for t in tids]
        if not tids:
            return {"reset": 0}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        reset = 0
        with self.lock:
            for tid in tids:
                self.conn.execute(
                    "INSERT INTO detail_queue(tid,state,attempts,pages_done,"
                    "last_error,enqueued_at,updated_at) VALUES(?,'pending',0,0,?,?,?) "
                    "ON CONFLICT(tid) DO UPDATE SET state='pending', attempts=0, "
                    "pages_done=0, last_error=excluded.last_error, updated_at=excluded.updated_at",
                    (tid, reason[:500], now, now))
                self.conn.execute(
                    "UPDATE threads SET detail_state='pending', detail_warn=NULL, "
                    "updated_at=? WHERE tid=?", (now, tid))
                reset += 1
            self.conn.commit()
        return {"reset": reset}

    def purge_details(self, tids: Iterable[int]) -> Dict[str, Any]:
        """**自行清除**：把选中的帖的详情**彻底清空**（楼层 / 分页凭据 / 流程 / 队列项）。

        比「重跑」更彻底：数据没了，下一次采是**从零**开始，
        不会拿旧内容凑数。适用于「怀疑本地这份是脏的」的情况。

        返回 {purged, posts, pages, flows} —— 界面上要把删了多少说清楚。
        """
        tids = [int(t) for t in tids]
        if not tids:
            return {"purged": 0, "posts": 0, "pages": 0, "flows": 0}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n_posts = n_pages = n_flows = 0
        with self.lock:
            for tid in tids:
                n_posts += self.conn.execute(
                    "DELETE FROM posts WHERE tid=?", (tid,)).rowcount or 0
                n_pages += self.conn.execute(
                    "DELETE FROM thread_pages WHERE tid=?", (tid,)).rowcount or 0
                n_flows += self.conn.execute(
                    "DELETE FROM flows WHERE tid=?", (tid,)).rowcount or 0
                self.conn.execute("DELETE FROM detail_queue WHERE tid=?", (tid,))
                self.conn.execute(
                    "UPDATE threads SET detail_state='pending', detail_warn=NULL, "
                    "first_post_id=NULL, last_post_id=NULL, posts_n=0, chars=0, "
                    "pages_total=0, updated_at=? WHERE tid=?", (now, tid))
            self.conn.commit()
        return {"purged": len(tids), "posts": n_posts, "pages": n_pages,
                "flows": n_flows}

    def set_dept_meta(self, dept_id: str, group_code: str, district: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE threads SET dept_group=?, dept_district=?, updated_at=? "
                "WHERE dept_id=? AND (dept_group IS NULL OR dept_group='')",
                (group_code, district, time.strftime("%Y-%m-%d %H:%M:%S"), dept_id))
            self.conn.commit()

    # ---------------------------------------------------------------- 标签
    def set_tags(self, tid: int, tags: List[Tuple[str, str, str]]) -> None:
        if not tags:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO thread_tags(tid,dim,code,name) VALUES(?,?,?,?)",
                [(tid, d, c, n) for d, c, n in tags])
            self.conn.commit()

    def add_source(self, tid: int, source: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO thread_sources(tid,source,first_seen) "
                "VALUES(?,?,?)",
                (tid, source, time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def add_list_form(self, tid: int, source: str, kind: str) -> None:
        """记下「这个 tid 在这个源的列表页里以什么形态出现过」。

        kind: row（普通帖行）/ stick（置顶帖行）/ announce（站方公告区）。
        置顶记录是**批量标注受限帖时的豁免名单**的依据。
        """
        if kind not in ("row", "stick", "announce"):
            return
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO thread_list_forms(tid,source,kind,first_seen) "
                "VALUES(?,?,?,?)",
                (tid, source, kind, time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def add_list_forms(self, rows, source: str) -> int:
        """批量记形态（**一次事务**）。

        ★ 为什么要批量版：列表页一页 99 行，
          逐行单独 commit 会把列表采集拖慢一个数量级。
        """
        items = []
        for r in rows:
            if getattr(r, "is_announcement", 0):
                kind = "announce"
            elif getattr(r, "is_stick", 0):
                kind = "stick"
            else:
                kind = "row"
            items.append((int(r.tid), source, kind,
                          time.strftime("%Y-%m-%d %H:%M:%S")))
        if not items:
            return 0
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO thread_list_forms(tid,source,kind,first_seen) "
                "VALUES(?,?,?,?)", items)
            self.conn.commit()
        return len(items)

    def sticky_tids(self) -> set:
        """曾经以**置顶**形态出现过的 tid 集合（豁免名单）。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT tid FROM thread_list_forms WHERE kind='stick'").fetchall()
        return {int(r[0]) for r in rows}

    def exempt_tids(self) -> set:
        """批量标注受限帖时的**豁免名单**（置顶帖 + 公告帖）。

        ★ 两类都要（2026-10-25 实测）：
          · stick    置顶的老帖，站方主动保留 → 可见
          · announce 站方公告区的链接，没有发帖时间（pub_ts=0）
                     → 会被时间判据误当成"很老的帖"，但它**实际可见**
        """
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT tid FROM thread_list_forms "
                "WHERE kind IN ('stick','announce')").fetchall()
        return {int(r[0]) for r in rows}

    # ---------------------------------------------------------------- 楼层
    def save_posts(self, tid: int, page_no: int, posts: List[Dict]) -> None:
        """落库一页楼层。

        ⚠ `floor` 用 CASE 而不是直接覆盖：
          Discuz 会把**置顶回复**额外渲染在第 1 页顶部，而那个位置**没有楼层号**
          （标签是「顶 来自 #」）；它在自己的页上才有真号（如 33#）。
          所以第 1 页解析出的 floor=0 **不能**覆盖掉后面页解析出的真楼层号。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.conn.executemany(
                """INSERT INTO posts
                   (pid,tid,page_no,floor,author,author_uid,post_time,post_ts,
                    content,chars,images,fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pid) DO UPDATE SET
                     page_no=excluded.page_no,
                     floor=CASE WHEN excluded.floor>0 THEN excluded.floor
                                ELSE posts.floor END,
                     author=excluded.author, author_uid=excluded.author_uid,
                     post_time=excluded.post_time, post_ts=excluded.post_ts,
                     content=excluded.content, chars=excluded.chars,
                     images=excluded.images, fetched_at=excluded.fetched_at""",
                [(p["pid"], tid, page_no, p.get("floor", 0), p.get("author", ""),
                  p.get("author_uid", ""), p.get("post_time", ""), p.get("post_ts", 0),
                  p.get("content", ""), p.get("chars", 0),
                  json.dumps(p.get("images", []), ensure_ascii=False), now)
                 for p in posts])
            self.conn.commit()

    @staticmethod
    def _clean_notice(notice: str) -> str:
        """把站方的提示原文**洗成一句人话**。

        ★ 为什么要洗（2026-10-25 实测踩过）
          站方提示页的正文里，那句提示后面还跟着一整段它的 JS：
              历史帖子仅限作者本人查看 if(history.length > (BROWSER.ie ? 0 : 1))
              { document.write(' [ 点击这里返回上一页 ] '); } else { ... }
          整段有几百个字符。直接拿它当日志行 → **日志框被两行就占满**，
          用户看着像「并行之后看不到日志」（其实是行太长）；
          写进 detail_warn / 正文标记行也一样，会把检索结果污染成一堆 JS。
          → 只留「历史帖子仅限作者本人查看」这一句。
        """
        s = (notice or "").strip()
        if "历史帖子仅限作者本人查看" in s:
            return "历史帖子仅限作者本人查看"
        s = re.sub(r"if\s*\(.*?\)\s*\{.*?\}", " ", s, flags=re.S)
        s = re.sub(r"[{};]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s[:80] if s else "站方限制：历史帖子仅限作者本人查看"

    def mark_restricted(self, tid: int, notice: str) -> None:
        """标记一帖为「**站方限制，正文不公开**」。

        ★ 为什么要单独一个状态（2026-10-23 用户拍板）
          站方对**历史帖子**只给一句「历史帖子仅限作者本人查看」，
          详情页正文对非作者不公开 —— 这是**站方的可见性策略**，
          不是我们程序坏了，也不是成员偷懒。
          所以它既不能算「采到了」，也不能算「采失败」：
            · `detail_state='restricted'` → **不计入**「已采详情帖」
            · 但把提示原文**写进正文** → 检索能查到、分析时知道这是怎么回事
            · 计数单列「站方限制」 → 界面上一眼看清多少帖属于这一类

        ⚠ 正文标记行的 pid 用**负数**（-tid）：真实 pid 都是正数，
          负数保证永远不会和站方真实楼层撞号。
        ⚠ `posts_n` 记 0：这不是一层楼，不能算进楼层数。

        ★ 队列行**必须一并置为终态**（2026-10-25 修）
          原来这里只写 `posts` 与 `threads`，**没碰 `detail_queue`** →
          受限帖在队列里仍是 `pending` → `claim_next_detail` 会**反复把它捡起来重采**，
          每次都白跑一次详情页请求 + 一轮限速等待。
          实测：库里 14,910 个受限帖仍挂在队列 pending 上，
          按单线程 1.5 秒/帖算，**约 41% 的产能被这些永远采不到的帖白占**。
          → 站方限制是**终态**：标了就不该再进队列。
        """
        notice = self._clean_notice(notice)
        content = ("【站方限制】" + notice +
                   " —— 该帖详情正文不公开（仅作者本人可查看），"
                   "非漏采、非失败。")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.conn.execute(
                """INSERT INTO posts
                   (pid,tid,page_no,floor,author,author_uid,post_time,post_ts,
                    content,chars,images,fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pid) DO UPDATE SET
                     content=excluded.content, chars=excluded.chars,
                     fetched_at=excluded.fetched_at""",
                (-int(tid), int(tid), 0, 0, "站方提示", "", now, 0,
                 content, len(content), "[]", now))
            self.conn.execute(
                "UPDATE threads SET detail_state='restricted', detail_warn=?, "
                "posts_n=0, chars=?, updated_at=? WHERE tid=?",
                (notice, len(content), now, int(tid)))
            # ★ 队列行置终态：站方限制不是「还没采」，是「采不到」
            self.conn.execute(
                "UPDATE detail_queue SET state='restricted', last_error=?, "
                "updated_at=? WHERE tid=? AND state IN ('pending','running')",
                (notice, now, int(tid)))
            self.conn.commit()

    def mark_restricted_bulk(self, tids: Iterable[int], notice: str = "") -> int:
        """**批量**标记「站方限制，正文不公开」。

        ★ 为什么要批量版（2026-10-25）
          阈值之前的帖有 **38 万个**量级，逐条调用 `mark_restricted` 意味着
          38 万次独立事务 —— 那要跑到天荒地老。这里**一次事务**搞定。

        ★ 语义与 mark_restricted 完全一致
          · `threads.detail_state='restricted'` → 不计入「已采详情帖」
          · posts 里写一行**提示正文**（pid 用 -tid，负数不与真实楼层撞号）
          · `detail_queue` 行置为终态 → **不再被捡起来重采**
            （这是提速的关键：受限帖是「采不到」，不是「还没采」）
        """
        notice = self._clean_notice(notice)
        content = ("【站方限制】" + notice +
                   " —— 该帖详情正文不公开（仅作者本人可查看），"
                   "非漏采、非失败。")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        tids = [int(t) for t in tids]
        if not tids:
            return 0
        post_rows = [(-t, t, 0, 0, "站方提示", "", now, 0,
                      content, len(content), "[]", now) for t in tids]
        with self.lock:
            self.conn.executemany(
                """INSERT INTO posts
                   (pid,tid,page_no,floor,author,author_uid,post_time,post_ts,
                    content,chars,images,fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pid) DO UPDATE SET
                     content=excluded.content, chars=excluded.chars,
                     fetched_at=excluded.fetched_at""",
                post_rows)
            self.conn.executemany(
                "UPDATE threads SET detail_state='restricted', detail_warn=?, "
                "posts_n=0, chars=?, updated_at=? WHERE tid=?",
                [(notice, len(content), now, t) for t in tids])
            self.conn.executemany(
                "UPDATE detail_queue SET state='restricted', last_error=?, "
                "updated_at=? WHERE tid=? AND state IN ('pending','running')",
                [(notice, now, t) for t in tids])
            self.conn.commit()
        return len(tids)

    def mark_detail_done_bulk(self, items: Iterable[Tuple[int, str]]) -> int:
        """把「**站方列表计数虚高导致的假少贴**」归位为 `done`（正文齐全即已采）。

        ★ 为什么是 `done` 而不是另立一个状态（2026-10-29 用户拍板）
          站方删楼后**列表页的 replies 计数不跟着减**，于是：
            列表页称「应有 4 楼」→ 我们算出 expect = replies+1 = 4
            实际详情页只有 3 层楼 → 实收 3 < 4 → 判成「少贴」。

          现场核对（实测 25 条，逐层对比）：
            站方详情页锚点数 == ajax 取到的正文楼层数 == 本地已存楼层数
            （pid 集合逐个对得上，类型差异除外）
          → **正文我们早就采全了，站方也照样公开**。
            差额 100% 来自站方列表页那个没随删楼而减的计数。

          ★ 所以「它没采到」是**错的记账**：正文明明在库里，
            却因为状态是终态而**不计入「已采详情帖」**
            （`threads_with_body` 的口径是 `chars>0 AND detail_state='done'`），
            界面上还会显示成「不是已采」——
            这就是「详情采集采不到这些篇目」的真正来源。

          → 正确处置：**正文齐全即已采** → 归为 `done`，
            把「站方列表计数虚高」写进 `detail_warn` 留痕即可。

        ★ 队列行**必须一并置为终态**（同 mark_restricted 的教训）
          否则它会一直挂在队列 pending 上 → 每轮详情采集都白跑一遍站方请求。
          实测（2026-10-28）：库里有 4384 条这种帖卡在
          `detail_state='stale'` + 队列 `pending`，
          按单线程 1.5 秒/帖算，**约 41% 的产能被白占**。

        ⚠ `posts_n` **保留**、正文**不动**：正文确实采到了
          （`restricted` 清零是因为那不是一层楼）。
        ⚠ 会**删掉**此前写的正文标记行（`pid = -tid`）：
          那是「站方计数缺陷」状态留下的说明行，
          归位成 done 后它就是多余噪音，还会把 `chars` 算歪。

        `items` = [(tid, note_text), ...]
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        pairs = [(int(t), (n or "").strip()) for t, n in items]
        if not pairs:
            return 0
        with self.lock:
            # 1. 删掉旧状态留下的正文标记行（pid = -tid，负数不与真实楼层撞号）
            self.conn.executemany(
                "DELETE FROM posts WHERE pid=? AND tid=? AND pid<0",
                [(-t, t) for t, _ in pairs])
            # 2. 帖状态：正文齐全 = 已采
            self.conn.executemany(
                "UPDATE threads SET detail_state='done', detail_warn=?, "
                "updated_at=? WHERE tid=?",
                [(n, now, t) for t, n in pairs])
            # 3. 队列行置终态：不是「还没采」，是「采到了，站方计数虚高」
            #    ★ 'count_mismatch' 是 2026-10-28 已下线的历史状态值，
            #      留在列表里是为了能把那批旧行一并收编成 done。
            self.conn.executemany(
                "UPDATE detail_queue SET state='done', last_error=?, "
                "updated_at=? WHERE tid=? "
                "AND state IN ('pending','running','count_mismatch')",
                [(n, now, t) for t, n in pairs])
            self.conn.commit()
        return len(pairs)

    def save_flows(self, tid: int, flows: List[Dict]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO flows(tid,step,name,time,ts) VALUES(?,?,?,?,?)",
                [(tid, f["step"], f["name"], f.get("time", ""), f.get("ts", 0))
                 for f in flows])
            self.conn.commit()

    def record_page(self, tid: int, page_no: int, pids: List[int],
                    status: str = "ok") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO thread_pages(tid,page_no,pids,n,status,fetched_at) "
                "VALUES(?,?,?,?,?,?)",
                (tid, page_no, ",".join(str(p) for p in pids), len(pids), status,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    # ---------------------------------------------------------------- 队列
    def enqueue_details(self, tids: Iterable[int], cutoff_ts: Optional[int] = None) -> Tuple[int, int]:
        """把待采详情入队。返回 (入队数, 因水位线跳过数)。

        ★ 2026-10-25 修：**已终态的帖不再入队**
          「按部门全采」会把该部门下**所有**没采过详情的帖入队，
          而其中 41.8 万帖早已标了 `restricted`（站方不公开正文，采不到）、
          2.2 万帖早已 `done`（采完了）。
          `INSERT OR IGNORE` 只在队列里**没有这一行**时才插入 →
          队列行被清掉过的那些帖会被**重新插回 pending**
          → worker 从队首开始，一路撞上这些「注定白跑」的帖。

          实测（2026-10-25）：「按部门全采」筛出 584,587 帖，
          其中绝大多数早就采不动了 → 白占产能、队首全是老帖。

          → 入队前先看 `threads.detail_state`：
            `restricted` / `done` 是终态，直接跳过；
            `stale`（待重采）**照常入队**（它确实还要采）。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        queued = skipped = 0
        done_hits = 0
        with self.lock:
            for tid in tids:
                # ★ 已终态的帖不入队（done / restricted）
                _st = self.conn.execute(
                    "SELECT detail_state FROM threads WHERE tid=?",
                    (int(tid),)).fetchone()
                if _st and (_st["detail_state"] or "") in ("done", "restricted"):
                    done_hits += 1
                    continue
                if cutoff_ts is not None:
                    r = self.conn.execute(
                        "SELECT pub_ts FROM threads WHERE tid=?", (tid,)).fetchone()
                    if r and r["pub_ts"] and r["pub_ts"] < cutoff_ts:
                        self.conn.execute(
                            "INSERT INTO detail_queue(tid,state,last_error,enqueued_at,updated_at) "
                            "VALUES(?, 'skipped', ?, ?, ?) "
                            "ON CONFLICT(tid) DO UPDATE SET state='skipped', "
                            "last_error=excluded.last_error, updated_at=excluded.updated_at "
                            "WHERE detail_queue.state='pending'",
                            (tid, f"早于水位线 {cutoff_ts}", now, now))
                        skipped += 1
                        continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO detail_queue(tid,state,enqueued_at,updated_at) "
                    "VALUES(?, 'pending', ?, ?)", (tid, now, now))
                queued += cur.rowcount or 0
            self.conn.commit()
        # 第 3 个返回值：因**已终态**（done / restricted）而没入队的数量
        self.last_enqueue_done_hits = done_hits
        return queued, skipped

    def claim_next_detail(self) -> Optional[Dict]:
        """取下一个待采详情。**先旧后新**：按发帖时间升序。

        ★★ 2026-10-25 修一个**并发下的致命 bug**
          原来 WHERE 是 `q.state IN ('pending','running')` ——
          把**正在采**（running）的帖也当成可领的活！
          串行时看不出问题（采完就变 done），但**并发时三个 worker
          会领到同一个 tid**（实测：detail-1/2/3 全领到 tid=8324409，
          同一帖被采 3 遍，另外 4 个帖根本没被领到）。
          → 现在**只领 `state='pending'`**。
          → `running` 的残留（进程被杀留下的）由 `reset_stale_running()`
            在每次采集任务开始时统一归位（断点续传靠它，不靠这里）。

        ★ 双保险：即使锁失效，也不会重复领
          UPDATE 带 `AND state='pending'` 条件，并检查 rowcount ——
          抢不到就换下一条。
        """
        with self.lock:
            r = self.conn.execute(
                """SELECT q.tid, q.pages_done, q.pages_total, q.attempts, t.pub_ts
                   FROM detail_queue q JOIN threads t ON t.tid = q.tid
                   WHERE q.state = 'pending'
                   ORDER BY COALESCE(t.pub_ts, 0) ASC, q.tid ASC LIMIT 1""").fetchone()
            if not r:
                return None
            cur = self.conn.execute(
                "UPDATE detail_queue SET state='running', attempts=attempts+1, updated_at=? "
                "WHERE tid=? AND state='pending'",
                (time.strftime("%Y-%m-%d %H:%M:%S"), r["tid"]))
            if not cur.rowcount:
                # 被人抢走了（说明锁没起到互斥作用）→ 换下一条
                return self.claim_next_detail()
            self.conn.commit()
            return dict(r)

    def set_detail_state(self, tid: int, state: str, error: str = "",
                         pages_done: Optional[int] = None,
                         pages_total: Optional[int] = None) -> None:
        with self.lock:
            sets = ["state=?", "last_error=?", "updated_at=?"]
            args = [state, error[:500], time.strftime("%Y-%m-%d %H:%M:%S")]
            if pages_done is not None:
                sets.append("pages_done=?"); args.append(pages_done)
            if pages_total is not None:
                sets.append("pages_total=?"); args.append(pages_total)
            args.append(tid)
            self.conn.execute(
                f"UPDATE detail_queue SET {','.join(sets)} WHERE tid=?", args)
            self.conn.commit()

    def reset_stale_running(self) -> int:
        """把上次进程被杀留下的 'running' 归位为 'pending'（可续采）。"""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE detail_queue SET state='pending' WHERE state='running'")
            self.conn.commit()
            return cur.rowcount or 0

    def finish_queue(self, tid: int, state: str = "done", error: str = "") -> int:
        """把 `detail_queue` 里这一行置终态（已采完 / 失败）。

        ★★ 为什么需要这个方法（2026-10-25 修）
          `threads.detail_state` 与 `detail_queue.state` 是**两张表**，
          谁改了前者**不会**自动改后者。

          组员回传（`assist.submit`）只写 `threads.detail_state='done'`，
          队列里那行就永远停在 `'pending'` →
            ① 白占产能（会被详情采集领走，发现已有正文，再采一遍）
            ② 每次列表采集都以为它「还没采详情」
          实测：库里积了 231 条这样的孤儿活（全部来自组员回传）。

          → 凡是把帖子置成终态的路径，都必须把队列行一起收掉。
          ★ 只收 `'pending'/'running'` 的行，不碰已终态的。
        """
        with self.lock:
            cur = self.conn.execute(
                "UPDATE detail_queue SET state=?, last_error=?, updated_at=? "
                "WHERE tid=? AND state IN ('pending','running')",
                (state, error[:500], time.strftime("%Y-%m-%d %H:%M:%S"), int(tid)))
            self.conn.commit()
            return cur.rowcount or 0

    # ---------------------------------------------------------------- 水位
    def get_watermark(self, source: str) -> Optional[Dict]:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM list_watermark WHERE source=?", (source,)).fetchone()
        return dict(r) if r else None

    def save_watermark(self, source: str, **kw) -> None:
        cols = ["source"] + list(kw.keys())
        vals = [source] + list(kw.values())
        with self.lock:
            self.conn.execute(
                f"INSERT INTO list_watermark({','.join(cols)}) "
                f"VALUES({','.join('?' * len(vals))}) "
                f"ON CONFLICT(source) DO UPDATE SET "
                f"{','.join(c + '=excluded.' + c for c in kw.keys())}",
                vals)
            self.conn.commit()

    # ---------------------------------------------------------------- 词典
    def save_depts(self, depts: List[Dict]) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        # ★ 顺带存拼音首字母（2026-10-24）：支持「输入名称、拼音或首字母」检索
        try:
            from .pinyin import initials as _pyi
        except Exception:                               # noqa: BLE001
            _pyi = lambda s: ""                          # type: ignore
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO depts(typeid,name,group_code,group_name,"
                "is_district,url,updated_at,py_initials) VALUES(?,?,?,?,?,?,?,?)",
                [(d["typeid"], d["name"], d.get("group_code", ""),
                  d.get("group_name", ""), d.get("is_district", 0),
                  d.get("url", ""), now, _pyi(d.get("name", "")))
                 for d in depts])
            self.conn.commit()

    def save_districts(self, districts: List[Dict]) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            from .pinyin import initials as _pyi
        except Exception:                               # noqa: BLE001
            _pyi = lambda s: ""                          # type: ignore
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO districts(typeid,name,last_month_msgs,"
                "last_month_accepted,total_msgs,total_accepted,updated_at,"
                "py_initials) VALUES(?,?,?,?,?,?,?,?)",
                [(d["typeid"], d.get("name", ""), d.get("last_month_msgs"),
                  d.get("last_month_accepted"), d.get("total_msgs"),
                  d.get("total_accepted"), now, _pyi(d.get("name", "")))
                 for d in districts])
            self.conn.commit()

    def save_site_stats(self, stats: Dict, raw: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO site_stats(captured_at,total_msgs,this_year_msgs,"
                "last_month_msgs,total_accepted,this_year_accepted,"
                "last_month_accepted,raw) VALUES(?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), stats.get("total"),
                 stats.get("this_year"), stats.get("last_month"),
                 stats.get("total_accepted"), stats.get("this_year_accepted"),
                 stats.get("last_month_accepted"), raw))
            self.conn.commit()

    def latest_site_stats(self) -> Optional[Dict]:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM site_stats ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------------- FTS
    def rebuild_fts(self) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO threads_fts(threads_fts) VALUES('rebuild')")
            self.conn.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
            self.conn.commit()

    def fts_available(self) -> bool:
        try:
            with self.lock:
                self.conn.execute(
                    "SELECT count(*) FROM threads_fts WHERE threads_fts MATCH '测试'").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---------------------------------------------------------------- 检索
    @staticmethod
    def _fts_quote(q: str, tokenize: bool = False, op: str = "AND") -> str:
        toks = _tokenize_for_search(q, tokenize)
        if not toks:
            return ""
        joiner = " OR " if op.upper() == "OR" else " AND "
        return joiner.join('"' + t.replace('"', '""') + '"' for t in toks)

    @staticmethod
    def _normalize(dim: str, value: str) -> List[str]:
        """把筛选值规范化成库里实际存的那个形态。

        库里存的是**名字**（category='投诉'、state='已受理'），
        但用户命令行里习惯写**编号**（--category 1、--state 2）。
        两边都认：编号先映射成名字，名字原样保留，编号匹配不到就当编号用。
        """
        from .taxonomy import CATEGORIES, LIGHTS, STATES
        table = {"category": CATEGORIES, "state": STATES, "light": LIGHTS}.get(dim)
        out: List[str] = []
        for v in (value or "").split(","):
            v = v.strip()
            if not v:
                continue
            if table:
                hit = next((x for x in table
                            if x["code"] == v or x.get("alias") == v or x["name"] == v), None)
                if hit:
                    out.append(hit["code"] if dim == "light" else hit["name"])
                    continue
            out.append(v)
        return out

    def _thread_filters(self, dept: str = "", group: str = "", district: str = "",
                        category: str = "", state: str = "", mailbox: str = "",
                        light: str = "", date_from: str = "", date_to: str = "") \
            -> Tuple[List[str], List[Any]]:
        where: List[str] = []
        args: List[Any] = []

        def multi(v: str) -> List[str]:
            return [x.strip() for x in (v or "").split(",") if x.strip()]

        if dept:
            ids = multi(dept)
            if ids:
                # ★ 这里原来写错了（2026-10-24 修）：
                #   旧写法 IN (?,?) 却只给了 1 个参数（把 "556,737" 当一个值绑进去）
                #   → 占位符与参数个数对不上 → sqlite 抛错 → 接口 500
                #   → 页面上表现为「检索失败：」（连错误信息都是空的）。
                # 一条帖可能带**多个部门**（dept_id + extra_dept_ids 逗号分隔），
                # 所以每个 id 要判两次：主部门是它，或副部门里含它。
                parts = []
                for i in ids:
                    parts.append("(t.dept_id = ? OR "
                                 "(','||COALESCE(t.extra_dept_ids,'')||',') LIKE ?)")
                    args += [i, f"%,{i},%"]
                where.append("(" + " OR ".join(parts) + ")")
        if group:
            # dept_group 存的是逗号连接的分组集合（一个部门可能属于多个分组），
            # 所以要用「成员判定」而不是等值判定。
            gs = multi(group)
            if gs:
                cl, ca = [], []
                for g in gs:
                    cl.append("(','||COALESCE(t.dept_group,'')||',') LIKE ?")
                    ca.append(f"%,{g},%")
                where.append("(" + " OR ".join(cl) + ")")
                args += ca
        if district:
            ds = multi(district)
            if ds:
                where.append(f"t.dept_id IN ({','.join('?' * len(ds))})")
                args += ds
        if category:
            cs = self._normalize("category", category)
            if cs:
                where.append(f"t.category IN ({','.join('?' * len(cs))})")
                args += cs
        if state:
            ss = self._normalize("state", state)
            if ss:
                where.append(f"t.state IN ({','.join('?' * len(ss))})")
                args += ss
        if mailbox:
            ms = multi(mailbox)
            if ms:
                where.append(f"t.mailbox_id IN ({','.join('?' * len(ms))})")
                args += ms
        if light:
            ls = self._normalize("light", light)
            if ls:
                where.append(f"t.light_code IN ({','.join('?' * len(ls))})")
                args += ls
        if date_from:
            where.append("COALESCE(t.pub_day,'') >= ?"); args.append(date_from)
        if date_to:
            where.append("COALESCE(t.pub_day,'') <= ?"); args.append(date_to)
        return where, args

    def search(self, q: str = "", scope: str = "all", *,
               dept: str = "", group: str = "", district: str = "",
               category: str = "", state: str = "", mailbox: str = "",
               light: str = "", date_from: str = "", date_to: str = "",
               limit: int = 20, offset: int = 0, sort: str = "time_desc",
               min_fts_chars: int = 3, op: str = "AND",
               tokenize: bool = False) -> Dict[str, Any]:
        """组合检索。返回 {total, items, mode, took_ms, scope, matched_in}。

        scope:
          thread —— 只搜标题与部门名
          post   —— 只搜楼层正文
          all    —— 两者并集，在**帖**这一层去重
        """
        t0 = time.time()
        where, args = self._thread_filters(
            dept, group, district, category, state, mailbox, light,
            date_from, date_to)
        wsql = (" AND ".join(where)) if where else "1=1"

        q = (q or "").strip()
        mode = "none"
        toks = _tokenize_for_search(q, tokenize) if q else []
        use_fts = False
        fts_expr = ""
        if q:
            if not toks:
                toks = [q]
            use_fts = all(len(t) >= min_fts_chars for t in toks)
            if use_fts:
                fts_expr = self._fts_quote(q, tokenize, op)
                mode = "fts"
            else:
                mode = "like"

        order = {
            "time_asc": "t.pub_ts ASC, t.tid ASC",
            "replies_desc": "t.replies DESC, t.pub_ts DESC",
            "views_desc": "t.views DESC, t.pub_ts DESC",
            "dept": "t.dept_name ASC, t.pub_ts DESC",
        }.get(sort, "t.pub_ts DESC, t.tid DESC")

        # ---- 线程语料（标题/部门） ----
        thread_ids: List[int] = []
        thread_total = 0
        if scope in ("thread", "all"):
            if not q:
                thread_total = self.conn.execute(
                    f"SELECT COUNT(*) FROM threads t WHERE {wsql}", args).fetchone()[0]
                thread_ids = [r[0] for r in self.conn.execute(
                    f"SELECT t.tid FROM threads t WHERE {wsql} ORDER BY {order} "
                    f"LIMIT ? OFFSET ?", args + [int(limit), int(offset)])]
            elif use_fts:
                # ⚠ MATCH 的左操作数必须写**表名**，不能写别名。
                #   写 `f MATCH ?`（f 是别名）会报 "no such column: f"。
                #   cacx 原版就是用表名形式，这里保持一致。
                sql = (f"SELECT t.tid FROM threads t "
                       f"JOIN threads_fts ON threads_fts.rowid=t.tid "
                       f"AND threads_fts MATCH ? "
                       f"WHERE {wsql} ORDER BY {order} LIMIT ? OFFSET ?")
                thread_ids = [r[0] for r in self.conn.execute(
                    sql, [fts_expr] + args + [int(limit), int(offset)])]
                thread_total = self.conn.execute(
                    f"SELECT COUNT(*) FROM threads t "
                    f"JOIN threads_fts ON threads_fts.rowid=t.tid "
                    f"AND threads_fts MATCH ? WHERE {wsql}",
                    [fts_expr] + args).fetchone()[0]
            else:
                like = f"%{toks[0]}%" if len(toks) == 1 else None
                cls, cargs = [], []
                for tk in toks:
                    lk = f"%{tk}%"
                    cls.append("(t.title LIKE ? OR t.dept_name LIKE ?)")
                    cargs += [lk, lk]
                if op.upper() == "OR":
                    extra = "(" + " OR ".join(cls) + ")"
                    cargs = []
                    for tk in toks:
                        lk = f"%{tk}%"
                        cargs += [lk, lk]
                else:
                    extra = " AND ".join(cls)
                sql = (f"SELECT t.tid FROM threads t WHERE ({extra}) AND {wsql} "
                       f"ORDER BY {order} LIMIT ? OFFSET ?")
                thread_ids = [r[0] for r in self.conn.execute(
                    sql, cargs + args + [int(limit), int(offset)])]
                thread_total = self.conn.execute(
                    f"SELECT COUNT(*) FROM threads t WHERE ({extra}) AND {wsql}",
                    cargs + args).fetchone()[0]

        # ---- 楼层语料（正文） ----
        post_hits: Dict[int, Dict] = {}
        post_total = 0
        if scope in ("post", "all"):
            if not q:
                post_total = self.conn.execute(
                    f"SELECT COUNT(DISTINCT p.tid) FROM posts p "
                    f"JOIN threads t ON t.tid=p.tid WHERE {wsql}", args).fetchone()[0]
                rows = self.conn.execute(
                    f"SELECT p.tid, p.pid, p.content FROM posts p "
                    f"JOIN threads t ON t.tid=p.tid WHERE {wsql} "
                    f"ORDER BY t.pub_ts DESC LIMIT ? OFFSET ?",
                    args + [int(limit), int(offset)]).fetchall()
                for r in rows:
                    post_hits[r["tid"]] = dict(r)
            elif use_fts:
                sql = (f"SELECT p.tid, p.pid, p.content FROM posts p "
                       f"JOIN posts_fts ON posts_fts.rowid=p.pid "
                       f"AND posts_fts MATCH ? "
                       f"WHERE {wsql} LIMIT 20000")
                rows = self.conn.execute(sql, [fts_expr] + args).fetchall()
                post_total = len({r["tid"] for r in rows})
                for r in rows:
                    post_hits.setdefault(r["tid"], dict(r))
            else:
                cls, cargs = [], []
                for tk in toks:
                    lk = f"%{tk}%"
                    cls.append("p.content LIKE ?")
                    cargs.append(lk)
                extra = ("(" + " OR ".join(cls) + ")") if op.upper() == "OR" \
                    else " AND ".join(cls)
                rows = self.conn.execute(
                    f"SELECT p.tid, p.pid, p.content FROM posts p "
                    f"WHERE {extra} AND {wsql} LIMIT 20000",
                    cargs + args).fetchall()
                post_total = len({r["tid"] for r in rows})
                for r in rows:
                    post_hits.setdefault(r["tid"], dict(r))

        # ---- 合并（帖级去重） ----
        tids = list(thread_ids)
        for tid in post_hits:
            if tid not in tids:
                tids.append(tid)
        if scope == "all" and q:
            # all 模式下两路各自取的是各自的前 limit 页，合并后总量不等于
            # 两者之和（可能有重叠、也可能有一方漏掉了排序靠后的），
            # 所以这里以「并集」为准重新排序分页。
            all_tids = set(thread_ids) | set(post_hits.keys())
            total = len(all_tids)
            tids = list(all_tids)
        elif scope == "post":
            total = post_total
        elif scope == "thread":
            total = thread_total
        else:
            total = thread_total

        if scope == "all" and q and tids:
            ph = ",".join("?" * len(tids))
            tids = [r[0] for r in self.conn.execute(
                f"SELECT t.tid FROM threads t WHERE t.tid IN ({ph}) ORDER BY {order}",
                tids)]
            tids = tids[int(offset): int(offset) + int(limit)]

        if tids:
            ph = ",".join("?" * len(tids))
            rows = self.conn.execute(
                f"SELECT t.tid, t.title, t.url, t.category, t.state, t.dept_id, "
                f"t.dept_name, t.dept_group, t.dept_district, t.mailbox_id, "
                f"t.light_code, t.light_name, t.author, t.pub_time, t.pub_day, "
                f"t.replies, t.views, t.detail_state, t.posts_n, t.chars, "
                f"t.first_post_id, t.last_post_id, t.pages_total "
                f"FROM threads t WHERE t.tid IN ({ph})", tids).fetchall()
            by_id = {r["tid"]: dict(r) for r in rows}
        else:
            by_id = {}

        items = []
        for tid in tids:
            it = by_id.get(tid)
            if not it:
                continue
            matched = []
            if tid in set(thread_ids):
                matched.append("title")
            if tid in post_hits:
                matched.append("post")
            it["matched_in"] = "+".join(matched) or "title"
            hit = post_hits.get(tid)
            if hit:
                it["snippet"] = self.make_snippet(hit.get("content", ""), toks)
                it["hit_pid"] = hit.get("pid")
            else:
                it["snippet"] = self.make_snippet(it.get("title", ""), toks)
                it["hit_pid"] = None
            items.append(it)

        return {
            "total": total,
            "items": items,
            "mode": mode,
            "took_ms": int((time.time() - t0) * 1000),
            "scope": scope,
            "matched_in": sorted({i["matched_in"] for i in items}),
        }

    @staticmethod
    def make_snippet(text: str, terms: List[str], width: int = 90) -> str:
        """截取第一个命中词周围的片段。

        必须在 Python 侧做：SQLite 的 LIKE 不给位置，中文也没法用 substr
        找多字节词的边界。宁可多读一点文本，也别让「片段」变成猜测。
        """
        if not text:
            return ""
        terms = [t for t in (terms or []) if t]
        if not terms:
            return text[:width].replace("\n", " ")
        low = text.lower()
        pos = -1
        for t in terms:
            p = low.find(t.lower())
            if p >= 0 and (pos < 0 or p < pos):
                pos = p
        if pos < 0:
            return text[:width].replace("\n", " ")
        start = max(0, pos - width // 3)
        end = min(len(text), start + width)
        frag = text[start:end].replace("\n", " ").replace("\r", "")
        return ("…" if start > 0 else "") + frag + ("…" if end < len(text) else "")

    # ---------------------------------------------------------------- 统计
    def stats(self) -> Dict[str, Any]:
        c = self.conn
        out: Dict[str, Any] = {}
        out["threads"] = c.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        # ★ 「楼层总数」只算**真实楼层**（pid>0）。
        #   pid<0 的是「站方限制」标记行（我们写进正文的那条说明），
        #   它不是楼层，算进去会让这个数虚高。
        out["posts"] = c.execute(
            "SELECT COUNT(*) FROM posts WHERE pid>0").fetchone()[0]
        out["posts_note"] = c.execute(
            "SELECT COUNT(*) FROM posts WHERE pid<0").fetchone()[0]
        out["threads_with_body"] = c.execute(
            "SELECT COUNT(*) FROM threads WHERE COALESCE(chars,0)>0 "
            "AND COALESCE(detail_state,'pending')='done'").fetchone()[0]
        out["detail_done"] = c.execute(
            "SELECT COUNT(*) FROM threads WHERE detail_state='done'").fetchone()[0]
        # ★ 站方限制（正文不公开）：单独计数，不混进「已采详情帖」
        out["detail_restricted"] = c.execute(
            "SELECT COUNT(*) FROM threads WHERE detail_state='restricted'").fetchone()[0]
        out["queue"] = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM detail_queue GROUP BY state")}
        out["depts"] = c.execute("SELECT COUNT(*) FROM depts").fetchone()[0]
        out["by_category"] = {r["category"]: r["n"] for r in c.execute(
            "SELECT category, COUNT(*) n FROM threads GROUP BY category ORDER BY n DESC")}
        out["by_state"] = {(r["state"] or "(站方未标)"): r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM threads GROUP BY state")}
        out["by_group"] = {}
        for r in c.execute(
                "SELECT dept_group, COUNT(*) n FROM threads "
                "WHERE COALESCE(dept_group,'')<>'' GROUP BY dept_group"):
            for g in (r["dept_group"] or "").split(","):
                if g:
                    out["by_group"][g] = out["by_group"].get(g, 0) + r["n"]
        out["by_district"] = {r["dept_district"]: r["n"] for r in c.execute(
            "SELECT dept_district, COUNT(*) n FROM threads "
            "WHERE COALESCE(dept_district,'')<>'' GROUP BY dept_district "
            "ORDER BY n DESC")}
        out["by_light"] = {r["light_code"] or "(未点亮)": r["n"] for r in c.execute(
            "SELECT light_code, COUNT(*) n FROM threads GROUP BY light_code")}
        out["warned"] = c.execute(
            "SELECT COUNT(*) FROM threads WHERE COALESCE(detail_warn,'')<>''").fetchone()[0]
        return out

