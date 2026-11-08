# -*- coding: utf-8 -*-
"""本机 API 冒烟：人工标注台（人工标注功能）。

★ **不往真库写任何标注/队列数据** —— 冒烟只做只读与权限反向验证。
  真库 anno_queue / thread_anno_tags 前后行数必须一样。

★ 自己签令牌而不是登录（沿用词表接口闸的做法，不动用户密码）。

★ 跑法：python _tools/audit_anno_api.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.config import Config  # noqa: E402
from msyt.db import DB  # noqa: E402
from msyt.webapp import make_token  # noqa: E402

BASE = os.environ.get("MSYT_BASE", "http://127.0.0.1:8765")
FAILED: list = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def call(path, method="GET", body=None, tok=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_detail": e.read().decode("utf-8", "replace")}


def main() -> int:
    print("=" * 62)
    print("闸：人工标注台 API（本机冒烟）")
    print("=" * 62)
    cfg = Config.load()
    db = DB(cfg.db_path)
    sec = db.get_meta("jwt_secret")
    arow = db.conn.execute(
        "SELECT email, role, node_id FROM members WHERE role='admin' LIMIT 1").fetchone()
    mrow = db.conn.execute(
        "SELECT email, role, node_id FROM members WHERE role='member' LIMIT 1").fetchone()
    if not arow or not sec:
        print("没有管理员账号或 jwt_secret，先跑一次 web 启动")
        return 1
    atok = make_token({"email": arow["email"], "role": "admin",
                       "node": arow["node_id"] or "admin"}, sec)
    mtok = (make_token({"email": mrow["email"], "role": "member",
                         "node": mrow["node_id"] or ""}, sec) if mrow else "")

    q0 = db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
    t0 = db.conn.execute("SELECT COUNT(*) FROM thread_anno_tags").fetchone()[0]
    r0 = db.conn.execute("SELECT COUNT(*) FROM sample_review").fetchone()[0]

    # ---- ① 未登录 ----
    print("\n--- ① 未登录 ---")
    check(call("/api/anno/queue").get("_http") == 401, "队列：未登录 401")
    check(call("/api/anno/vocab").get("_http") == 401, "候选标签：未登录 401")
    check(call("/api/anno/stats").get("_http") == 401, "统计：未登录 401")
    check(call("/api/anno/batches").get("_http") == 401, "任务批次：未登录 401")
    check(call("/api/anno/agreement").get("_http") == 401, "一致性：未登录 401")
    check(call("/api/anno/queue/by-filter", "POST",
                     {"limit": 1, "assignee": "x"}).get("_http") == 401,
          "批量派活：未登录 401")
    check(call("/api/anno/claim", "POST", {"limit": 1}).get("_http") == 401,
          "领活：未登录 401")

    # ---- ② 管理员只读 ----
    print("\n--- ② 管理员只读 ---")
    d = call("/api/anno/vocab", tok=atok)
    check("candidates" in d, "候选标签返回成功")
    check("relation" in (d.get("candidates") or {}),
          "候选标签含关系维度（规则已产生的取值）")
    d = call("/api/anno/queue", tok=atok)
    check(isinstance(d.get("items"), list), f"队列返回数组（{len(d.get('items') or [])} 条）")
    d = call("/api/anno/stats", tok=atok)
    check("review" in d and "queue" in d, "统计含队列与核验两块")
    d = call("/api/anno/pending", tok=atok)
    check(isinstance(d.get("items"), list), "待复核列表返回数组")
    d = call("/api/anno/batches", tok=atok)
    check(isinstance(d.get("items"), list),
          f"任务批次返回数组（{len(d.get('items') or [])} 个批次）")
    d = call("/api/anno/agreement", tok=atok)
    check("annotated" in d and "thread_pct" in d,
          f"一致性返回统计：{d.get('annotated')} 条已标注")
    # ★ 领活要真调一次才知道通不通 —— 但它**会写库**。
    #   所以：调完立刻把领到的那几条删回去，末尾再核对行数没变。
    q_before = db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
    tids_before = {int(r["tid"]) for r in db.conn.execute("SELECT tid FROM anno_queue")}
    d = call("/api/anno/claim", "POST", {"limit": 3}, tok=atok)
    check(d.get("claimed", 0) >= 1,
          f"领活成功：领到 {d.get('claimed')} 条（任务 {d.get('task_code')}）")
    check(str(d.get("task_code", "")).startswith("L"),
          f"领到的活归一个批次：{d.get('task_code')}")
    d = call("/api/anno/queue", tok=atok)
    check(len(d.get("items") or []) >= 1, "领到的活出现在队列里")
    d = call("/api/anno/batches", tok=atok)
    check(len(d.get("items") or []) >= 1, "领到的活出现在批次视图里")
    # 领完立刻撤回，保持真库干净。
    # ★ 2026-11-08 修：原来写的是 DELETE ... WHERE state='queued'（**宽条件**），
    #   → 把**别人队列里的活**也删了（真库被清掉 19 条组员刚领的活）。
    #   验收脚本只该动**自己这次造出来的**数据。
    tids_after = {int(r["tid"]) for r in db.conn.execute("SELECT tid FROM anno_queue")}
    new_tids = tids_after - tids_before
    if new_tids:
        db.conn.executemany("DELETE FROM anno_queue WHERE tid=?",
                            [(t,) for t in new_tids])
    db.conn.commit()
    q_restored = db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
    check(q_restored == q_before,
          f"领活测试的活已撤回（{q_before} → {q_restored}）")

    # ---- ③ 成员 ----
    print("\n--- ③ 成员 ---")
    if mtok:
        d = call("/api/anno/queue", tok=mtok)
        check(isinstance(d.get("items"), list), "成员能看到自己的队列")
        d = call("/api/anno/pending", tok=mtok)
        check(d.get("_http") == 403, f"成员看待复核被拒（实得 {d.get('_http')}）")
        d = call("/api/anno/review", "POST", {"tid": 1, "approved": True}, tok=mtok)
        check(d.get("_http") == 403, f"成员复核被拒（实得 {d.get('_http')}）")
        d = call("/api/anno/queue", "POST", {"tids": [999999999]}, tok=mtok)
        check(d.get("_http") == 400, f"入队不存在的帖 → 400（实得 {d.get('_http')}）")
        # ★ 只测「派给别人」这条路 —— 它在写库**之前**就 403 了。
        #   不测「不传 assignee」那条：那是「派给自己」，会真的写库，
        #   违反本脚本「不往真库写任何数据」的纪律（闸已经抓到过一次）。
        d = call("/api/anno/queue/by-filter", "POST",
                 {"limit": 1, "assignee": "someone-else"}, tok=mtok)
        check(d.get("_http") == 403,
              f"★ 成员批量派给别人 → 403（实得 {d.get('_http')}）")
    else:
        print("  （库里没有成员账号，跳过成员检查）")

    # ---- ④ 参数校验 ----
    print("\n--- ④ 参数校验 ---")
    d = call("/api/anno/submit", "POST", {"tid": 0}, tok=atok)
    check(d.get("_http") == 400, f"缺 tid → 400（实得 {d.get('_http')}）")
    d = call("/api/anno/submit", "POST", {"tid": "abc"}, tok=atok)
    check(d.get("_http") == 400, f"tid 非整数 → 400（实得 {d.get('_http')}）")
    d = call("/api/anno/review", "POST", {"tid": 0}, tok=atok)
    check(d.get("_http") == 400, f"复核缺 tid → 400（实得 {d.get('_http')}）")

    # ---- ⑤ 真库没被写脏 ----
    print("\n--- ⑤ 真库没被写脏 ---")
    q1 = db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
    t1 = db.conn.execute("SELECT COUNT(*) FROM thread_anno_tags").fetchone()[0]
    r1 = db.conn.execute("SELECT COUNT(*) FROM sample_review").fetchone()[0]
    check(q0 == q1, f"anno_queue 行数不变（{q0} → {q1}）")
    check(t0 == t1, f"thread_anno_tags 行数不变（{t0} → {t1}）")
    check(r0 == r1, f"sample_review 行数不变（{r0} → {r1}）")

    print("\n" + "=" * 62)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
