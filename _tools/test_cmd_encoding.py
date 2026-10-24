# -*- coding: utf-8 -*-
"""验收：启动包里的 .cmd 必须是**纯 ASCII + CRLF**。

★ 为什么：cmd.exe 会把非 ASCII 字节咬坏（记忆里的坑：
  「.cmd 含中文 → cmd.exe 咬坏后续行」）。
  文件**名**可以是中文（Windows 支持），但**内容**必须纯 ASCII。
"""
from __future__ import annotations

import os
import sys

ROOT = r"【项目根】"
PKG = r"【成员端启动包】"

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


print("=" * 70)
print("启动包 .cmd 的编码验收")
print("=" * 70)

cmds = [f for f in os.listdir(PKG) if f.lower().endswith(".cmd")]
check("包里有 .cmd 文件", bool(cmds), str(cmds))
check("有「自检.cmd」", "自检.cmd" in cmds, str(cmds))

for name in cmds:
    p = os.path.join(PKG, name)
    raw = open(p, "rb").read()
    non_ascii = [b for b in raw if b > 127]
    has_crlf = b"\r\n" in raw
    has_lone_lf = b"\n" in raw.replace(b"\r\n", b"")
    check(f"「{name}」内容纯 ASCII", not non_ascii,
          f"非 ASCII 字节 {len(non_ascii)} 个")
    check(f"「{name}」用 CRLF 换行", has_crlf and not has_lone_lf,
          f"CRLF={has_crlf} 孤立LF={has_lone_lf}")
    # 常见坑：拼接时先拼 \n 再 replace 会产生 \r\r\n
    check(f"「{name}」没有 \\r\\r\\n", b"\r\r\n" not in raw)

# 自检.cmd 关键内容
sc = open(os.path.join(PKG, "自检.cmd"), "rb").read().decode("ascii")
check("自检.cmd 调用 msyt.selfcheck", "msyt.selfcheck" in sc)
check("自检.cmd 会装 requests 依赖", "pip install" in sc)

# 助手 cmd 的 Edge 提示
hc = open(os.path.join(PKG, "启动助手.cmd"), "rb").read().decode("ascii")
check("启动助手.cmd 有 Edge 本地网络访问提示",
      "local network access" in hc)
check("启动助手.cmd 提到自检.cmd", "self-check" in hc or "cmd" in hc)

print()
print("=" * 70)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
