# -*- coding: utf-8 -*-
"""验收：小助手端口能设置，且**下载的包与设置同步**。

★ 要验的三件事：
  ① 服务端改端口 → 新下载的包里就是那个端口（cmd 的 --port + helper-port.txt）
  ② 成员改本机端口 → 改包里的 helper-port.txt 就生效（不用改 cmd、不用重下包）
  ③ 端口对不上时，网页能发现并提醒
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.request
import zipfile

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("test@example.com", "test-password")

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


def login(email, pw):
    body = json.dumps({"email": email, "password": pw}).encode()
    req = urllib.request.Request(BASE + "/api/auth/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get(path, tok):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def post(path, tok, body):
    b = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=b, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def bundle(tok):
    req = urllib.request.Request(BASE + "/api/agent/bundle.zip")
    req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def main() -> int:
    print("=" * 72)
    print("小助手端口：可设置 + 与下载的包同步")
    print("=" * 72)
    adm = login(*ADMIN)["token"]
    mem = login(*MEMBER)["token"]

    print("\n① 接口与权限")
    d = get("/api/agent/helper-port", mem)
    check("成员能读端口", isinstance(d.get("port"), int), str(d))
    check("有默认值兜底", d.get("default") == 8766, str(d))
    try:
        post("/api/agent/helper-port", mem, {"port": 8791})
        check("成员改端口被拒", False, "居然成功了")
    except Exception as e:                             # noqa: BLE001
        check("成员改端口被拒（管理员专属）", getattr(e, "code", "") == 403,
              str(getattr(e, "code", e)))

    print("\n② 设成 8790 → 下的包要跟着变")
    r = post("/api/agent/helper-port", adm, {"port": 8790})
    check("管理员改成 8790", r.get("port") == 8790, str(r))
    check("返回值带『要重下包』的提醒",
          "重新下载" in (r.get("message") or ""), str(r.get("message"))[:60])
    try:
        z = bundle(mem)
        with zipfile.ZipFile(io.BytesIO(z)) as zz:
            names = zz.namelist()
            check("包里有 helper-port.txt", "helper-port.txt" in names, str(names[:6]))
            cmd = zz.read("启动助手.cmd").decode("ascii", "replace")
            check("启动助手.cmd 带 --port 8790", "--port 8790" in cmd,
                  [l for l in cmd.splitlines() if "msyt-helper" in l][:1])
            hp = zz.read("helper-port.txt").decode("utf-8", "replace")
            check("helper-port.txt 第一行是 8790", hp.strip().splitlines()[0] == "8790",
                  hp.strip().splitlines()[0])
            check("helper-port.txt 说明了怎么改", "改" in hp and "端口" in hp)
            readme = zz.read("说明.txt").decode("utf-8", "replace")
            check("说明.txt 里的端口是 8790", "127.0.0.1:8790" in readme)
            check("说明.txt 提到 helper-port.txt", "helper-port.txt" in readme)
            check("包里的 cmd 仍无 '->'（防闪退）", "->" not in cmd)
    finally:
        post("/api/agent/helper-port", adm, {"port": 8766})

    print("\n③ 改回 8766")
    d2 = get("/api/agent/helper-port", mem)
    check("端口已改回 8766", d2.get("port") == 8766, str(d2))
    z2 = bundle(mem)
    with zipfile.ZipFile(io.BytesIO(z2)) as zz:
        cmd2 = zz.read("启动助手.cmd").decode("ascii", "replace")
        check("新包里的 cmd 用 8766", "--port 8766" in cmd2,
              [l for l in cmd2.splitlines() if "msyt-helper" in l][:1])

    print("\n④ 成员本地改端口：helper-port.txt 优先于命令行")
    import tempfile
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "helper-port.txt"), "w", encoding="utf-8") as f:
        f.write("8801\n# 注释行\n")
    sys.path.insert(0, os.path.join(
        r"【项目根】",
        "程序"))
    from msyt import netdiag
    old = os.getcwd()
    os.chdir(tmp)
    try:
        p = netdiag.local_helper_port()
        check("读到本地 helper-port.txt 的 8801", p == 8801, str(p))
    finally:
        os.chdir(old)

    print()
    print("=" * 72)
    print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
