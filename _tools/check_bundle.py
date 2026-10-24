# -*- coding: utf-8 -*-
"""检查启动包的生成结果（不依赖跑着的服务，直接调 bundle 模块）。

用法：python _tools/check_bundle.py [输出目录]
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

from msyt import bundle            # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "_tmp_bundle")
os.makedirs(out, exist_ok=True)
data, name = bundle.build_bundle(
    "https://msyt.example.com", "TICKET-DEMO", PROG,
    helper_src=os.path.join(PROG, "msyt-helper.py"))
path = os.path.join(out, name)
with open(path, "wb") as f:
    f.write(data)
print("zip:", path, os.path.getsize(path), "bytes")

with zipfile.ZipFile(path) as z:
    names = z.namelist()
    print("条目:", len(names))
    for n in names:
        print("   ", n)
    cmd = z.read("启动采集.cmd").decode("utf-8", "replace")
    nonascii = sum(1 for b in cmd.encode("utf-8") if b > 127)
    print("\n启动采集.cmd：非 ASCII 字节 =", nonascii, "（必须是 0）")
    print("重复 '@echo off' =", cmd.lower().count("@echo off"), "（必须是 1）")
    print("空行数 =", sum(1 for l in cmd.splitlines() if not l.strip()))
    print("含凭据 =", ("password" in cmd.lower() and "--password" in cmd))
    print("--- 内容 ---")
    print(cmd)
    print("--- server.txt ---")
    print(z.read("server.txt").decode("utf-8", "replace"))
