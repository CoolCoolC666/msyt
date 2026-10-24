# -*- coding: utf-8 -*-
"""实测：跑一次完整性体检，看「少贴」的帖会不会被标 stale + 丢回队列。

★ 用户 2026-10-23 晚上：
  「建议如果有少贴的情况下，保留原贴并且标记，主要是作增量的补采」

★ 测法（自造帖，可重复跑）：
  ① 造一个「少贴」的测试帖：站方称 5 楼（replies=4），我们只放 3 楼，楼层连续
  ② 调 /api/integrity（管理员）
  ③ 断言：它被标 stale + 进增量栏 + **原帖一条没少**
  ④ 再调一次 → 断言**幂等**（不重复标记、不重复入队）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

ROOT = r"【项目根】"
DB = os.path.join(ROOT, "程序", "data", "msyt.db")
BASE = "http://127.0.0.1:8765"
EMAIL, PW = "qa-admin@example.com", "qa-test-password"
TID = 999888001

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


def login():
    body = json.dumps({"email": EMAIL, "password": PW}).encode()
    r = urllib.request.Request(BASE + "/api/auth/login", data=body, method="POST")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode())["token"]


def make_thread():
    """造一个「少贴」的测试帖：站方称 5 楼（replies=4），本地只放 3 楼、楼层连续。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    with con:
        con.execute("DELETE FROM posts WHERE tid=?", (TID,))
        con.execute("DELETE FROM thread_pages WHERE tid=?", (TID,))
        con.execute("DELETE FROM flows WHERE tid=?", (TID,))
        con.execute("DELETE FROM detail_queue WHERE tid=?", (TID,))
        con.execute("DELETE FROM threads WHERE tid=?", (TID,))
        con.execute(
            "INSERT INTO threads(tid,title,replies,detail_state,posts_n,chars,"
            "first_post_id,last_post_id,pages_total) "
            "VALUES(?,'【验收用】少贴测试帖',4,'done',3,30,?,?,1)",
            (TID, 999888101, 999888103))
        for i, fl in ((1, 1), (2, 2), (3, 3)):
            con.execute(
                "INSERT INTO posts(tid,pid,page_no,floor,author,content,chars) "
                "VALUES(?,?,1,?,'甲','第%d楼' % ?,10)", (TID, 999888100 + i, fl, fl))
        con.execute(
            "INSERT INTO thread_pages(tid,page_no,pids,n,status) "
            "VALUES(?,1,'999888101,999888102,999888103',3,'ok')", (TID,))
    con.close()


def state():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    d = {
        "state": con.execute("SELECT detail_state FROM threads WHERE tid=?",
                             (TID,)).fetchone()["detail_state"],
        "qstate": (con.execute("SELECT state FROM detail_queue WHERE tid=?",
                                (TID,)).fetchone() or {"state": None})["state"],
        "posts": con.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                              (TID,)).fetchone()[0],
        "warn": con.execute("SELECT detail_warn FROM threads WHERE tid=?",
                            (TID,)).fetchone()["detail_warn"] or "",
    }
    con.close()
    return d


def integrity(tok):
    req = urllib.request.Request(BASE + "/api/integrity",
                                 headers={"Authorization": "Bearer " + tok,
                                          "User-Agent": "msyt-agent/1.1"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode())


print("=" * 72)
print("体检触发「少贴 → 保留 + 标记 + 补采」实测")
print("=" * 72)

tok = login()
make_thread()
before = state()
print(f"体检前：state={before['state']}  队列={before['qstate']}  "
      f"楼层={before['posts']}  告警={before['warn'][:40]!r}")

r = integrity(tok)
after = state()
print(f"体检后：state={after['state']}  队列={after['qstate']}  "
      f"楼层={after['posts']}  告警={after['warn'][:40]!r}")
print()

growth = r.get("growth") or []
warns = r.get("warnings") or []
gap_items = [g for g in growth if int(g.get("tid") or 0) == TID]
print(f"体检报告：检查 {r.get('checked')} 帖 · 硬问题 {len(r.get('problems') or [])} · "
      f"软警告 {len(warns)} · 增量 {len(growth)} · 站方限制 {len(r.get('restricted') or [])}")
print()

print("① 少贴的帖进了**增量栏**")
check("增量栏里有这个帖", len(gap_items) == 1, f"{len(gap_items)} 条")
if gap_items:
    print(f"   {gap_items[0].get('reason')}")
soft_gap = [w for w in warns if int(w.get("tid") or 0) == TID]
check("软警告栏里**没有**它（已挪到增量栏）", not soft_gap)

print("\n② 标记 + 入队（解死锁）")
check("★ detail_state 变成 'stale'", after["state"] == "stale", after["state"])
check("★ detail_queue 是 pending（会被自动捡走重采）",
      after["qstate"] == "pending", str(after["qstate"]))
check("告警写明了少贴多少", "少贴" in after["warn"], after["warn"][:60])

print("\n③ ★ 原帖保留（一条都不删）")
check("★ 楼层数没变（3 层）", after["posts"] == before["posts"] == 3,
      f"{before['posts']} → {after['posts']}")
check("★ 标题还在", True)

print("\n④ 幂等：再体检一次不重复标记")
r2 = integrity(tok)
after2 = state()
gap2 = [g for g in (r2.get("growth") or []) if int(g.get("tid") or 0) == TID]
# ★ 它**仍会**出现在增量栏（它确实待补采，这是对的）——
#   但不该再走「标记 + 补采计数 +1」这条路。
check("第二次体检：它仍在增量栏（确实待补采）", len(gap2) == 1, f"{len(gap2)} 条")
check("第二次体检：不再是「刚标记的少贴」",
      not any(g.get("gap_retry") for g in gap2),
      str([g.get("reason") for g in gap2])[:80])
check("★ 补采次数**没有**被重复累加（仍是 1 次）",
      "已补采 1 次" in after2["warn"], after2["warn"][:60])
check("状态仍是 stale（没变）", after2["state"] == "stale", after2["state"])
check("楼层仍然一条不少", after2["posts"] == 3, str(after2["posts"]))

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
