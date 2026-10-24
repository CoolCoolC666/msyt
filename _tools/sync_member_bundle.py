# -*- coding: utf-8 -*-
"""sync_member_bundle.py —— 把最新代码同步到成员端启动包目录。

为什么要有这个脚本
------------------
包是**每次下载现场生成**的（`msyt/bundle.py`）。但开发机上常常需要
「把这台机器上的最新代码，直接铺到某个已解压的包目录里」——
比如：助手进程跑的是旧代码、包里的凭证过期了、agent 改了行为要验证。

它做三件事：
  ① 用服务端**现生成的包**（拿到最新 msyt 源码 + 最新 .cmd + 新凭证）
  ② 解压覆盖到目标目录（先备份旧包）
  ③ 打印清单，方便肉眼核对

用法：
    python _tools\\sync_member_bundle.py --email test@example.com --password xxx
    python _tools\\sync_member_bundle.py --email ... --password ... --target "【成员端启动包】"
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PKG))

UA = "msyt-sync/1.1"


def post(base: str, path: str, payload: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("detail") or body
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}") from None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"请求失败：{e}") from None


def get(base: str, path: str, token: str = "", raw: bool = False):
    """GET。raw=True 时返回原始字节（下载 zip 用）。"""
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            b = r.read()
            return b if raw else json.loads(b.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("detail") or body
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}") from None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"请求失败：{e}") from None


def api_base() -> str:
    p = PKG / "data" / "agent-api-base.txt"
    try:
        v = p.read_text(encoding="utf-8").strip()
        if v:
            return v.rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--target", default=r"【成员端启动包】")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    # 1) 登录
    r = post(args.base, "/api/auth/login",
             {"email": args.email, "password": args.password})
    tok = r.get("token")
    member = r.get("member") or {}
    print(f"[1/4] 已登录 {member.get('email')}（{member.get('role')} / "
          f"{member.get('node_id')}）")

    # 2) ★ 直接下载**网页那个**包（/api/agent/bundle.zip）—— 2026-10-23 改
    #    原来这里走的是 /api/agent/ticket（一次性、30 分钟），
    #    而网页走 /api/agent/bundle.zip（30 天、可重复兑换）——
    #    **两条签发路径** → 工具生成的包凭证跟网页不一样，
    #    这正是用户说的「启动包没有根据账号更新」。
    #    现在只有一条路径：这个工具拿到的包 = 组员从网页下载到的包。
    data = get(args.base, "/api/agent/bundle.zip", token=tok, raw=True)
    print(f"[2/4] 已下载启动包（{len(data) / 1024:.0f} KB）")

    # 3) 从包里读出凭证与控制面地址（跟组员拿到的一模一样）
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        ticket = z.read("agent-ticket.txt").decode("utf-8").splitlines()[0].strip()
        server = z.read("server.txt").decode("utf-8").strip()
    print(f"[3/4] 凭证已就位（30 天有效、可重复兑换）· 控制面 {server}")

    # 4) 铺到目标目录
    # ★ 不再「rename 整个目录」（2026-10-23 改）：
    #   目标目录常被 Windows 资源管理器打开着，
    #   rename 整个目录会报 `WinError 32`（另一个程序正在使用此文件），
    #   整个同步就失败了 —— 而实际只需要**覆盖里面的文件**。
    #   改成：先复制一份做备份（复制不要求独占），再逐文件覆盖。
    target = Path(args.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.no_backup:
        backup = target.with_name(target.name + ".bak-" +
                                  time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copytree(str(target), str(backup))
            print(f"[4/4] 旧包已备份到 {backup}")
        except Exception as e:                        # noqa: BLE001
            print(f"[4/4] 备份跳过（{e}）—— 不影响铺新包")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(str(target))
    print(f"[4/4] 已铺到 {target}")

    # 清单
    print("\n包内清单：")
    for p in sorted(target.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(target)}  ({p.stat().st_size} B)")
    print(f"\n控制面地址：{server}")
    print("★ 助手会自动读包内 agent-ticket.txt 换令牌；"
          "这个凭证 30 天内有效、可反复用，助手重启也不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
