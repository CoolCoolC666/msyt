# -*- coding: utf-8 -*-
"""
test_job_cleanup.py —— 「任务运行情况」的历史记录清理（2026-10-26 新增）
================================================================================
背景
----
回归里几十个脚本会起真实任务 → 全留在「任务运行情况」里刷屏，
管理员根本看不见真正在跑的那个。于是加了两个**管理员专属**的清理接口：

  · `DELETE /api/jobs/{jid}`  删一条**已结束**的任务（连同日志）
  · `POST  /api/jobs/clear`   一键清掉所有**已结束**的（running 的保留）

★ 口径：删的是「**任务运行记录**」，**不碰采集结果**（库里的数据一行不动）。

本脚本钉住的行为：
  ① 成员删 → 403（管理动作，成员没资格）
  ② 删运行中的 → 400（先停止再删）
  ③ 删不存在的 → 404
  ④ 删已结束 → 200，之后再删 → 404
  ⑤ 一键清理 → 只清已结束的，**running 的留下**

用法：python _tools/test_job_cleanup.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT + os.sep + "_tools")

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("test@example.com", "test-password")
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(method, path, body=None, token=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "msyt-agent/1.1")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def login(cred):
    st, body = call("POST", "/api/auth/login",
                    {"email": cred[0], "password": cred[1]})
    if st != 200:
        return ""
    return (json.loads(body) or {}).get("token", "")


def main() -> int:
    print("=" * 78)
    print("任务运行记录清理 —— 验收")
    print("=" * 78)

    admin = login(ADMIN)
    member = login(MEMBER)
    if not admin:
        print("  （管理员登录失败 → 环境不满足，跳过）")
        print("=" * 78)
        print("共 0 项 · 通过 0 · 失败 0")
        return 0
    check("管理员拿到令牌", bool(admin))
    check("成员拿到令牌", bool(member))

    # 起一条任务（详情采集，量小，跑得快）
    call("POST", "/api/jobs/detail", {"limit": 2}, admin)
    time.sleep(1)
    jobs = json.loads(call("GET", "/api/jobs", None, admin)[1] or "{}").get("items", [])
    check("任务已创建", len(jobs) >= 1, f"{len(jobs)} 条")

    running = next((j for j in jobs if j["state"] == "running"), None)
    if running:
        st, body = call("DELETE", f"/api/jobs/{running['id']}", None, admin)
        check("② 删**运行中**的任务 → 400（先停止再删）",
              st == 400, f"{st} {body[:50]}")
        # 停掉它，好拿到一条「已结束」的样本
        call("POST", f"/api/jobs/{running['id']}/stop", {}, admin)
        for _ in range(30):
            time.sleep(1)
            j = json.loads(call("GET", f"/api/jobs/{running['id']}", None, admin)[1] or "{}")
            if j.get("state") != "running":
                break
        check("任务已停下（state != running）", j.get("state") != "running",
              str(j.get("state")))

        st, body = call("DELETE", f"/api/jobs/{running['id']}", None, member)
        check("① 成员删别人的任务记录 → 403", st == 403, f"{st} {body[:50]}")

        st, body = call("DELETE", f"/api/jobs/{running['id']}", None, admin)
        check("④ 管理员删已结束的任务 → 200", st == 200, f"{st} {body[:50]}")

        st, _ = call("DELETE", f"/api/jobs/{running['id']}", None, admin)
        check("④ 再删一次 → 404（确实删掉了）", st == 404, f"status={st}")

    st, _ = call("DELETE", "/api/jobs/no-such-job", None, admin)
    check("③ 删不存在的任务 → 404", st == 404, f"status={st}")

    # ⑤ 一键清理：running 的必须留下
    call("POST", "/api/jobs/detail", {"limit": 40}, admin)
    time.sleep(1)
    jobs = json.loads(call("GET", "/api/jobs", None, admin)[1] or "{}").get("items", [])
    n_running = sum(1 for j in jobs if j["state"] == "running")
    st, body = call("POST", "/api/jobs/clear", {}, admin)
    ok = st == 200 and '"ok":true' in body.replace(" ", "")
    check("⑤ 一键清理接口可用", ok, f"{st} {body[:50]}")
    jobs2 = json.loads(call("GET", "/api/jobs", None, admin)[1] or "{}").get("items", [])
    n_running2 = sum(1 for j in jobs2 if j["state"] == "running")
    check("⑤ 清理后**运行中的任务还在**", n_running2 == n_running,
          f"清理前 running={n_running}，清理后={n_running2}")
    check("⑤ 清理后只剩运行中的任务",
          all(j["state"] == "running" for j in jobs2),
          str([j["state"] for j in jobs2]))

    # 收尾：把测试自己起的任务停掉，别给界面留垃圾
    for j in jobs2:
        if j["state"] == "running":
            call("POST", f"/api/jobs/{j['id']}/stop", {}, admin)

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
