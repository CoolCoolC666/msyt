# -*- coding: utf-8 -*-
"""验收：启动包里的 .cmd 必须是**纯 ASCII + CRLF**，且**不含会被 cmd 误解析的重定向符**。

★ 坑 1（编码）：cmd.exe 会把非 ASCII 字节咬坏（记忆里的坑：
  「.cmd 含中文 → cmd.exe 咬坏后续行」）。
  文件**名**可以是中文（Windows 支持），但**内容**必须纯 ASCII。

★ 坑 2（重定向符，2026-10-24 实测踩到）：
  cmd.exe 把 **">" 当输出重定向符**，所以提示行里写 "->" 会出事：
    · `echo   -> run xxx`  → 提示语被写进一个叫 "run" 的**垃圾文件**，控制台上那行还消失
    · 行尾的 `->`（> 后面没文件名）→ cmd 报
      "The syntax of the command is incorrect." → **批处理中断、退出码 255、窗口秒关（闪退）**
  → .cmd 里只允许出现 `>nul` / `2>nul` / `2>&1` 这三种重定向。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
# ★ 启动包位置**不写死**（2026-10-25：它从桌面搬到了 02 同级）
from bundle_path import find_bundle
PKG = find_bundle()
if not PKG:
    print('本机没有成员端启动包 → 跳过')
    sys.exit(0)

OK = 0
FAIL = 0

#: .cmd 里**允许**的重定向（除此之外的 ">" 都是事故）
SAFE_REDIRECT = re.compile(r"2>&1|2>nul|>nul")


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  —— {extra}" if extra else ""))


print("=" * 70)
print("启动包 .cmd 的编码 + 重定向符验收")
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

    # ★ 重定向符：只允许 >nul / 2>nul / 2>&1
    txt = raw.decode("ascii", "replace")
    rest = SAFE_REDIRECT.sub("", txt)
    bad = [i + 1 for i, ln in enumerate(rest.splitlines()) if ">" in ln]
    check(f"「{name}」没有裸 '>'（防闪退）", not bad,
          f"可疑行号 {bad}" if bad else "")
    check(f"「{name}」没有 '->' 箭头写法", "->" not in txt)

# ---- 生成器本身也不能再吐出 "->" ----
gen = os.path.join(ROOT, "程序", "msyt", "bundle.py")
if os.path.exists(gen):
    g = open(gen, encoding="utf-8").read()
    # 只看**真正会进 .cmd 的字符串**，跳过注释（注释里可以举例写 "->"）
    bad_gen = [i + 1 for i, ln in enumerate(g.splitlines())
               if '"echo' in ln and "->" in ln and not ln.lstrip().startswith("#")]
    check("生成器 bundle.py 的 echo 行不含 '->'", not bad_gen,
          f"可疑行号 {bad_gen}" if bad_gen else "")

# 自检.cmd 关键内容
sc = open(os.path.join(PKG, "自检.cmd"), "rb").read().decode("ascii")
check("自检.cmd 调用 msyt.selfcheck", "msyt.selfcheck" in sc)
check("自检.cmd 会装 requests 依赖", "pip install" in sc)

# 助手 cmd 的 Edge 提示
hc = open(os.path.join(PKG, "启动助手.cmd"), "rb").read().decode("ascii")
check("启动助手.cmd 有 Edge 本地网络访问提示",
      "local network access" in hc)
check("启动助手.cmd 提到自检.cmd", "self-check" in hc or "cmd" in hc)

# ★ 包里不该有那三个垃圾文件（被 "->" 重定向出来的）
junk = [f for f in ("Edge", "or", "run") if os.path.exists(os.path.join(PKG, f))]
check("包里没有 '->' 重定向产生的垃圾文件", not junk, str(junk))

print()
print("=" * 70)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)

