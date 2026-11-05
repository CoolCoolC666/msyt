# -*- coding: utf-8 -*-
"""本机 API 冒烟：给「词表」接口跑一遍（不改任何账号密码）。

★ 为什么自己签令牌而不是登录
  `admin-账号.txt` 里是**初始密码**，用户后来在网页上改过 → 登录会 401。
  为了**不动用户的密码**，这里直接用服务端同一套 `make_token` + 库里的
  jwt_secret 签一个管理员令牌，只在本机测试用。

★ 跑法（项目「程序」目录下，路径有空格要加引号）：
    python _tools/audit_keyword_api.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

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
        return json.loads(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_detail": e.read().decode("utf-8", "replace")}


def main() -> int:
    print("=" * 62)
    print("闸：词表 API（本机冒烟）")
    print("=" * 62)
    cfg = Config.load()
    db = DB(cfg.db_path)
    sec = db.get_meta("jwt_secret")
    row = db.conn.execute(
        "SELECT email, role, node_id FROM members WHERE role='admin' LIMIT 1"
    ).fetchone()
    if not row or not sec:
        print("没有管理员账号或 jwt_secret，先跑一次 web 启动")
        return 1
    tok = make_token({"email": row["email"], "role": "admin",
                      "node": row["node_id"] or "admin"}, sec)

    # ---- ① 列表 ----
    print("\n--- ① 词表列表 ---")
    d = call("/api/keywords", tok=tok)
    items = d.get("items") or []
    check(len(items) > 0, f"词表有词（实得 {len(items)}）")
    check(all("hit_tids" in k for k in items), "每条带命中统计")
    check(d.get("kind_cn"), "带词类中文名")

    # ---- ② 加词 / 删词 ----
    print("\n--- ② 加词、删词 ---")
    w = "冒烟测试词%d" % int(time.time() % 100000)
    r = call("/api/keywords", "POST", {"word": w, "kind": "issue"}, tok)
    check(r.get("ok") is True, f"加词成功：{r.get('keyword', {}).get('word')}")
    kid = (r.get("keyword") or {}).get("id")
    r2 = call("/api/keywords", "POST", {"word": w, "kind": "core"}, tok)
    check((r2.get("keyword") or {}).get("kind") == "core", "同词再加 = 更新词类")
    r3 = call("/api/keywords/%d/enabled" % kid, "POST", {"enabled": False}, tok)
    check(r3.get("ok") is True, "停用成功")
    r4 = call("/api/keywords/%d" % kid, "DELETE", tok=tok)
    check(r4.get("ok") is True, "删除成功")

    # ---- ③ 非法词类被拒 ----
    print("\n--- ③ 校验 ---")
    r5 = call("/api/keywords", "POST", {"word": "x", "kind": "不存在的类"}, tok)
    check(r5.get("_http") == 400, f"非法词类被拒（实得 {r5.get('_http')}）")
    r6 = call("/api/keywords", "POST", {"word": "  ", "kind": "issue"}, tok)
    check(r6.get("_http") == 400, f"空词被拒（实得 {r6.get('_http')}）")

    # ---- ④ 扫描（后台任务） ----
    print("\n--- ④ 扫描全库（后台任务） ---")
    r7 = call("/api/keywords/scan", "POST", {}, tok)
    jid = (r7.get("job") or {}).get("id")
    check(bool(jid), f"拿到任务号：{jid}")
    if jid:
        job = {}
        for _ in range(200):
            time.sleep(3)
            job = call("/api/jobs/%s" % jid, tok=tok)
            if job.get("state") != "running":
                break
        check(job.get("state") == "done", f"扫描完成（state={job.get('state')}）")
        res = job.get("result") or {}
        check(int(res.get("scanned") or 0) > 0, f"扫了层数：{res.get('scanned')}")
        check(int(res.get("hits") or 0) > 0, f"命中条数：{res.get('hits')}")
        print("  用时 %ss" % job.get("elapsed"))

    # ---- ⑤ 命中列表 ----
    print("\n--- ⑤ 命中列表 ---")
    ks = call("/api/keywords", tok=tok).get("items") or []
    hit = [k for k in ks if k["hit_tids"] > 0]
    check(bool(hit), "有词命中到帖子")
    if hit:
        h = call("/api/keywords/%d/hits?limit=10&offset=0" % hit[0]["id"], tok=tok)
        check(h.get("total", 0) > 0, f"命中接口有数据（total={h.get('total')}）")
        check(bool(h.get("items")), "命中列表有条目")

    # ---- ⑥ ★反向验证：无令牌 / 成员令牌改不了词 ----
    print("\n--- ⑥ ★反向验证：接口层也要拦住非管理员 ---")
    r8 = call("/api/keywords")
    check(r8.get("_http") == 401, f"没令牌 → 401（实得 {r8.get('_http')}）")
    mem = db.conn.execute(
        "SELECT email, role, node_id FROM members WHERE role<>'admin' LIMIT 1"
    ).fetchone()
    if mem:
        mtok = make_token({"email": mem["email"], "role": mem["role"],
                           "node": mem["node_id"] or "member"}, sec)
        r9 = call("/api/keywords", "POST", {"word": "成员越权词", "kind": "issue"},
                  tok=mtok)
        check(r9.get("_http") == 403,
              f"成员加词 → 403（实得 {r9.get('_http')}）")
        r10 = call("/api/keywords/scan", "POST", {}, mtok)
        check(r10.get("_http") == 403,
              f"成员触发扫描 → 403（实得 {r10.get('_http')}）")
        r11 = call("/api/keywords", tok=mtok)
        check(r11.get("ok") is True, "成员**能看**词表")

    print("\n" + "=" * 62)
    if FAILED:
        print("失败 %d 项：" % len(FAILED))
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
