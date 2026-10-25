# -*- coding: utf-8 -*-
"""test_assist_queue_sync.py —— 组员回传后队列行必须收掉（2026-10-25 修）

★ 背景（实测定位）
  `threads.detail_state` 与 `detail_queue.state` 是**两张表**，
  改前者**不会**自动改后者。
  组员回传路径（`assist.submit`）原来只写 `threads.detail_state='done'`，
  队列里那行永远停在 `'pending'` → 变成「注定白跑的孤儿活」：
    ① 白占产能（会被详情采集领走，发现已有正文，再采一遍）
    ② 列表采集一直以为它「还没采详情」
  实测库里积了 231 条这样的孤儿活，**全部**来自组员回传。

★ 本测试断言
  1. `db.finish_queue()` 能把 pending 行收成终态，且不碰已终态的行
  2. `assist.submit()` 成功后，队列那一行**已经**不是 pending
  3. 回传失败时队列行收成 failed（也不能留在 pending）
  4. 站方限制走 mark_restricted → 队列行收成 restricted
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

ROOT = r"【项目根】"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.db import DB          # noqa: E402
from msyt.assist import AssistService  # noqa: E402


PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def new_db() -> DB:
    tmp = os.path.join(tempfile.gettempdir(),
                       f"msyt_assist_q_{uuid.uuid4().hex[:8]}.db")
    return DB(tmp)


class FakeHub:
    def info(self, node, msg, task=""):
        pass

    def warn(self, node, msg, task=""):
        pass

    def error(self, node, msg, task=""):
        pass


def seed(db: DB, tid: int, state: str = "pending") -> None:
    """造一个帖 + 一条队列行（pending）。"""
    db.conn.execute(
        "INSERT OR REPLACE INTO threads(tid,title,detail_state,pub_ts) "
        "VALUES(?,?,?,?)", (tid, f"t{tid}", state, 1700000000))
    db.conn.execute(
        "INSERT OR REPLACE INTO detail_queue(tid,state,enqueued_at,updated_at) "
        "VALUES(?,?,?,?)", (tid, "pending", "2026-10-22 00:00:00",
                            "2026-10-22 00:00:00"))
    db.conn.commit()


def qstate(db: DB, tid: int) -> str:
    r = db.conn.execute("SELECT state FROM detail_queue WHERE tid=?",
                        (tid,)).fetchone()
    return r["state"] if r else ""


print("### ① db.finish_queue()")
db = new_db()
seed(db, 1001)
check("pending → done", db.finish_queue(1001, "done") == 1
      and qstate(db, 1001) == "done")
check("已终态的行不被改动（幂等）",
      db.finish_queue(1001, "done") == 0 and qstate(db, 1001) == "done")

seed(db, 1002)
check("pending → failed",
      db.finish_queue(1002, "failed", "boom") == 1
      and qstate(db, 1002) == "failed")

seed(db, 1003)
db.conn.execute("UPDATE detail_queue SET state='running' WHERE tid=1003")
db.conn.commit()
check("running → done（半截的活也要收）",
      db.finish_queue(1003, "done") == 1 and qstate(db, 1003) == "done")

print()
print("### ② 组员回传成功后队列行收成 done")
db2 = new_db()
svc = AssistService(db2, FakeHub())
seed(db2, 2001)
posts = [{"pid": 5001, "floor": 1, "author": "a", "post_time": "2026-06-21",
          "content": "楼主正文", "chars": 4, "page_no": 1}]
r = svc.submit("member:x", "", 2001, posts, [{"page_no": 1, "pids": [5001]}],
               {"ok": True, "pages": 1, "posts": 1, "chars": 4})
check("回传返回 ok", bool(r.get("ok")), str(r)[:80])
ts = db2.conn.execute("SELECT detail_state FROM threads WHERE tid=2001").fetchone()
check("threads.detail_state='done'", ts["detail_state"] == "done")
check("★ detail_queue 那行不再是 pending（这是修掉的 bug）",
      qstate(db2, 2001) == "done", f"实际={qstate(db2, 2001)!r}")

print()
print("### ③ 回传失败（ok=False）→ 队列行收成 failed，也不能留 pending")
db3 = new_db()
svc3 = AssistService(db3, FakeHub())
seed(db3, 2002)
posts2 = [{"pid": 6001, "floor": 1, "author": "a", "post_time": "2026-06-21",
           "content": "x", "chars": 1, "page_no": 1}]
svc3.submit("member:x", "", 2002, posts2, [{"page_no": 1, "pids": [6001]}],
            {"ok": False, "pages": 1, "posts": 1, "chars": 1})
check("failed 的帖队列行也是终态", qstate(db3, 2002) == "failed",
      f"实际={qstate(db3, 2002)!r}")

print()
print("### ④ 站方限制（restricted）→ 队列行收成 restricted")
db4 = new_db()
svc4 = AssistService(db4, FakeHub())
seed(db4, 2003)
r4 = svc4.submit("member:x", "", 2003, [], [], {"restricted": True,
                                                "reason": "历史帖子仅限作者本人查看"})
check("回传返回 restricted", bool(r4.get("restricted")), str(r4)[:80])
check("restricted 的帖队列行也是终态", qstate(db4, 2003) == "restricted",
      f"实际={qstate(db4, 2003)!r}")

print()
print("=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✓")

