# -*- coding: utf-8 -*-
"""check_progress.py —— 一眼看清协作进度（谁在线、采了多少、失败/受限各多少）。

用法：
    set MSYT_TEST_PW=xxx
    python _tools\\check_progress.py --email test@example.com
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request

BASE = "http://127.0.0.1:8765"


def post(path: str, payload: dict, tok: str = "") -> dict:
    h = {"Content-Type": "application/json", "User-Agent": "msyt-check/1.0"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                               method="POST", headers=h)
    with urllib.request.urlopen(r, timeout=20) as x:
        return json.loads(x.read())


def get(path: str, tok: str) -> dict:
    r = urllib.request.Request(BASE + path,
                               headers={"Authorization": "Bearer " + tok,
                                        "User-Agent": "msyt-check/1.0"})
    with urllib.request.urlopen(r, timeout=20) as x:
        return json.loads(x.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="test@example.com")
    ap.add_argument("--password", default=os.environ.get("MSYT_TEST_PW", ""))
    args = ap.parse_args()
    tok = post("/api/auth/login",
               {"email": args.email, "password": args.password})["token"]

    print("=== 在线节点 ===")
    for x in get("/api/assist/online", tok)["items"]:
        print(f"  {x['node']:<26} 在线={x['online']!s:<5} 已完成={x['done']:<5} "
              f"进行中={x['taken']:<5} 失败={x['failed']:<5} 站方限制={x['restricted']}")
    print("\n=== 协作任务 ===")
    for t in get("/api/assist/tasks", tok)["items"]:
        print(f"  {t['code']}  {t['state']:<8} 完成 {t['done']}/{t['total']}  "
              f"进行中 {t['inflight']}  待领 {t['pending']}  "
              f"失败 {t['failed']}  站方限制 {t.get('restricted', 0)}")
        for n, v in (t.get("by_node_detail") or {}).items():
            print(f"      {n:<26} 完成 {v['done']} / 已领 {v['taken']} / "
                  f"失败 {v['failed']} / 受限 {v.get('restricted', 0)}")
    st = get("/api/stats", tok)
    print("\n=== 总览 ===")
    print(f"  帖子总数 {st['threads']}  楼层总数 {st['posts']}  "
          f"已采详情帖 {st['detail_done']}  站方限制帖 {st.get('detail_restricted', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
