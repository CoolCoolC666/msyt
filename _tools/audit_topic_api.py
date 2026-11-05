# -*- coding: utf-8 -*-
"""本机 API 冒烟：选题/任务命名规范（P2-3）。

★ **只读 + 权限反向验证**，不往真库写协作任务 ——
  真库 assist_tasks 现在是空的，随便写一条会变成成员看得见的假任务。

★ 自己签令牌而不是登录（沿用词表接口闸的做法，不动用户密码）。

★ 跑法：python _tools/audit_topic_api.py
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
    print("闸：选题命名 API（本机冒烟）")
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

    before = db.conn.execute("SELECT COUNT(*) FROM assist_tasks").fetchone()[0]

    # ---- ① 没令牌 → 401 ----
    print("\n--- ① 未登录 ---")
    check(call("/api/assist/tasks").get("_http") == 401, "任务列表：未登录 401")
    check(call("/api/assist/topic/preview").get("_http") == 401, "规范名预览：未登录 401")

    # ---- ② 预览 ----
    print("\n--- ② 规范名预览 ---")
    q = urlencode({"scope": "宁波公交·换乘", "keywords": "晚点,拥挤,换乘",
                   "audience": "市交通运输局", "spec": "周报·3000字"})
    d = call("/api/assist/topic/preview?" + q, tok=atok)
    want = "监测范围：宁波公交·换乘｜关键词：晚点/拥挤/换乘｜服务对象：市交通运输局｜报告规格：周报·3000字"
    check(d.get("title") == want, f"预览规范名：{d.get('title')}")
    d0 = call("/api/assist/topic/preview?" + urlencode({}), tok=atok)
    check(d0.get("title") == "", "四要素全空 → 空标题（不编名字）")

    # ---- ③ 成员不能建任务 ----
    print("\n--- ③ 权限 ---")
    if mtok:
        r = call("/api/assist/task", "POST",
                 {"title": "x", "dept_ids": ["726"], "total": 1}, tok=mtok)
        check(r.get("_http") == 403, f"成员建任务被拒（实得 {r.get('_http')}）")
    else:
        print("  （库里没有成员账号，跳过成员 403 检查）")

    # ---- ④ 任务列表与检索 ----
    print("\n--- ④ 列表与检索 ---")
    d = call("/api/assist/tasks", tok=atok)
    check(isinstance(d.get("items"), list), f"任务列表返回数组（{len(d.get('items') or [])} 条）")
    dq = call("/api/assist/tasks?q=" + urlencode({"q": "晚点"}), tok=atok)
    check(isinstance(dq.get("items"), list), "带 q 参数也能返回数组")

    # ---- ⑤ 非法参数被拒（不写库） ----
    print("\n--- ⑤ 参数校验 ---")
    r = call("/api/assist/task", "POST", {"title": "x", "dept_ids": [], "total": 1}, tok=atok)
    check(r.get("_http") == 400, f"没选部门 → 400（实得 {r.get('_http')}）")
    r = call("/api/assist/task", "POST",
             {"title": "x", "dept_ids": ["999999"], "total": 1}, tok=atok)
    check(r.get("_http") == 400, f"部门下没有待采帖 → 400（实得 {r.get('_http')}）")

    after = db.conn.execute("SELECT COUNT(*) FROM assist_tasks").fetchone()[0]
    check(before == after, f"冒烟**没往真库写协作任务**（{before} → {after}）")

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
