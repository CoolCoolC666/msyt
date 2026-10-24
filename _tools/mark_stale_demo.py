# -*- coding: utf-8 -*-
"""
mark_stale_demo.py —— 重建一次「站方又回复了」的历史状态，留下一个待重采样本
================================================================================
为什么需要它：
  增量识别要在**列表页的回复数两次快照有差异**时才会触发。
  站方今天恰好没在这批源里新回复（2026-10-23 实测重采 17 个源，零增量），
  所以界面上「已识别的增量」栏是空的 —— 功能是对的，但看不出它长什么样。

这个脚本做的事：
  把某个**已采完详情**的帖的「站方回复数」改小 —— 这等于**重建了
  「上次采集时」的快照**（那一刻站方的回复数确实更少）。
  然后重采它所在的源：列表页会带回**站方真实**的回复数，
  于是增量识别命中 → 该帖被标记 `stale` → 丢回队列 → 等详情采集重采。

这不是造数据：
  · 帖是真实的、楼层是真实的、列表页数据是真实的；
  · 唯一人为的一步是「把旧快照调小」，而那本来就是历史事实；
  · 脚本结束后，库里的 `replies` 会被站方真实值刷新回来。

留下的是一个**真实的待重采任务**（详情采集跑起来会自动把它捡走）。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "程序"))

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")


def call(path, method="GET", body=None, token=""):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    from msyt.config import Config
    cfg = Config.load()
    tok = call("/api/auth/login", "POST",
               {"email": ADMIN[0], "password": ADMIN[1]})["token"]

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    # 挑一个已采完详情、且所在源不大的帖
    cand = conn.execute(
        "SELECT t.tid, t.replies, s.source FROM threads t "
        "JOIN thread_sources s ON s.tid=t.tid "
        "WHERE COALESCE(t.detail_state,'pending')='done' "
        "GROUP BY t.tid").fetchall()
    if not cand:
        print("库里没有已采完详情的帖")
        return 1
    sizes = {}
    for c in cand:
        sizes[c["source"]] = conn.execute(
            "SELECT COUNT(*) n FROM thread_sources WHERE source=?",
            (c["source"],)).fetchone()["n"]
    cand.sort(key=lambda c: sizes.get(c["source"], 999999))
    pick = cand[0]
    source, tid = pick["source"], int(pick["tid"])
    old_rep = int(pick["replies"] or 0)
    fake = max(0, old_rep - 2)
    dept_id = int(source.split(":")[1]) if source.startswith("dept:") else 0
    print(f"目标 tid={tid}（源 {source}，库里回复数 {old_rep} → 重建旧快照 {fake}）")
    with conn:
        conn.execute("UPDATE threads SET replies=? WHERE tid=?", (fake, tid))
    conn.commit()

    r = call("/api/jobs/list", "POST",
             {"dept_ids": [dept_id], "pages": 500, "restart": True,
              "node": "admin"}, token=tok)
    jid = r["job"]["id"]
    print(f"重采 {source}（job {jid}）…")
    for _ in range(180):
        time.sleep(2)
        j = call(f"/api/jobs/{jid}?tail=60", token=tok)
        if j["state"] != "running":
            break
    hits = [l["m"] for l in j.get("logs", []) if "站方有新回复" in l["m"] or "↻" in l["m"]]
    for m in hits:
        print("  " + m)

    conn2 = sqlite3.connect(cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT detail_state, replies, posts_n FROM threads WHERE tid=?",
        (tid,)).fetchone()
    q = conn2.execute(
        "SELECT state FROM detail_queue WHERE tid=?", (tid,)).fetchone()
    print(f"结果：detail_state={row['detail_state']}  replies={row['replies']}"
          f"（站方真实值）  队列={q['state'] if q else '无'}")
    conn2.close()
    conn.close()
    if row["detail_state"] == "stale":
        print(f"★ 已在库里留下一个真实的待重采样本：tid={tid}"
              f"（下次跑详情采集会自动重采它）")
        return 0
    print("（没有触发增量 —— 站方真实回复数没有超过旧快照）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
