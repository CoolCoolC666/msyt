# -*- coding: utf-8 -*-
"""
test_growth_real.py —— 增量识别的**真实链路**实验
================================================================================
做法：
  ① 挑一个已经采过列表的部门源
  ② 把库里某个 tid 的「站方回复数」**人为改小**（模拟站方后来又回复了）
  ③ 用 `restart=true` 重采这个源 —— 列表页会带回**站方真实**的回复数
  ④ 检验：那个 tid 是否被自动标记为 `stale`（待重采）并丢回队列

为什么可以人为改小：
  增量识别的判据是「列表页给的回复数 vs 库里的回复数」，
  人为把库里的改小，就等价于「站方多了一条回复」——
  检测代码走的是**真实列表页数据 + 真实代码路径**。

⚠ 只改 replies 一个字段（它只用于显示与增量检测），采集后会自己恢复。
⚠ 结束时把该 tid 的 stale 状态清掉（恢复成 done），不留痕。
"""
import os
import sqlite3
import sys
import time
import urllib.request
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")
SOURCE = "dept:1098"          # 宁波银行：只有 5 页，重采便宜
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(path, method="GET", body=None, token=""):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    from msyt.config import Config
    cfg = Config.load()
    print("=" * 78)
    print("增量识别 —— 真实列表页链路实验")
    print("=" * 78)

    tok = call("/api/auth/login", "POST",
               {"email": ADMIN[0], "password": ADMIN[1]})["token"]

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    # ① 挑一个**本地已采完详情**的帖（增量识别只对 done 的帖生效），
    #    并且它所属的源要小（重采便宜）
    cand = conn.execute(
        "SELECT t.tid, t.replies, s.source FROM threads t "
        "JOIN thread_sources s ON s.tid=t.tid "
        "WHERE COALESCE(t.detail_state,'pending')='done' "
        "GROUP BY t.tid ORDER BY t.tid DESC").fetchall()
    if not cand:
        print("  （库里还没有采完详情的帖，跳过）")
        return 0
    # 优先挑源里帖子少的（重采便宜）
    sizes = {}
    for c in cand:
        sizes[c["source"]] = conn.execute(
            "SELECT COUNT(*) n FROM thread_sources WHERE source=?",
            (c["source"],)).fetchone()["n"]
    cand.sort(key=lambda c: sizes.get(c["source"], 999999))
    pick = cand[0]
    SOURCE = pick["source"]
    tid = int(pick["tid"])
    old_rep = int(pick["replies"] or 0)
    dept_id = int(SOURCE.split(":")[1]) if SOURCE.startswith("dept:") else 0
    if not dept_id:
        print(f"  （源 {SOURCE} 不是 dept 源，跳过）")
        return 0
    # ② 人为把库里的「站方回复数」**改小** → 等价于站方后来多回了
    fake = max(0, old_rep - 3)
    print(f"  目标 tid={tid}（源 {SOURCE}，{sizes.get(SOURCE)} 帖）")
    print(f"  库里回复数 {old_rep} → 人为改成 {fake}，"
          f"列表页真实值应约 {old_rep} → 差 {old_rep - fake} 条应被判为增量")

    with conn:
        conn.execute("UPDATE threads SET replies=? WHERE tid=?",
                     (fake, tid))
    conn.commit()

    try:
        # ③ 重采这个源（restart=true → 从头采，不看断点）
        r = call("/api/jobs/list", "POST",
                 {"dept_ids": [dept_id], "pages": 500, "restart": True,
                  "node": "admin"}, token=tok)
        jid = r["job"]["id"]
        print(f"  已启动列表采集 {jid}（源 {SOURCE}）…")
        for _ in range(120):
            time.sleep(2)
            j = call(f"/api/jobs/{jid}?tail=50", token=tok)
            if j["state"] != "running":
                break
        logs = [l["m"] for l in j.get("logs", [])]
        hit = [m for m in logs if "站方有新回复" in m or "detail_growth" in m]
        print("  ---- 采集日志里的增量提示 ----")
        for m in logs[-8:]:
            print("  | " + m)

        conn2 = sqlite3.connect(cfg.db_path)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(
            "SELECT detail_state, replies FROM threads WHERE tid=?", (tid,)).fetchone()
        q = conn2.execute(
            "SELECT state FROM detail_queue WHERE tid=?", (tid,)).fetchone()
        conn2.close()

        check("★ 采集时打出了增量提示", bool(hit), (hit[0][:80] if hit else "没有"))
        check("★ 该帖被标记为 stale（待重采）", row["detail_state"] == "stale",
              row["detail_state"])
        check("★ 丢回队列（pending，等详情采集捡走）",
              bool(q) and q["state"] == "pending",
              str(dict(q)) if q else "没有队列行")
        check("★ 库里的回复数被站方真实值刷新回来",
              int(row["replies"] or 0) > fake,
              f"replies={row['replies']}（人为改小的是 {fake}）")
    finally:
        # ④ 收尾：恢复成 done，清掉队列行，不留痕
        with conn:
            conn.execute(
                "UPDATE threads SET detail_state='done', replies=? WHERE tid=?",
                (old_rep, tid))
            conn.execute("DELETE FROM detail_queue WHERE tid=?", (tid,))
        conn.commit()
        conn.close()
        print(f"  （tid={tid} 已恢复为 done，队列行已清）")

    print("\n" + "=" * 78)
    npass = sum(1 for _, ok, _ in results if ok)
    nfail = len(results) - npass
    for name, ok, extra in results:
        if not ok:
            print(f"  FAIL {name}  {extra}")
    print(f"共 {len(results)} 项 · 通过 {npass} · 失败 {nfail}")
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
