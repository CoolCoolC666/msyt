# -*- coding: utf-8 -*-
"""扫全仓的 U+FFFD（UTF-8 替换字符 = 写坏的中文）。

★ 为什么要有：写文件时某个中文字符被写坏，会变成 U+FFFD（渲染成黑方块）。
  本机实测踩过两轮：① 发布仓里已有一处（app.js 的一条注释）
  ② 本轮新写脚本又坏了两处 → **每次写完带中文的源码都要扫一遍**。
★ 「故意写出 U+FFFD 当示例」的地方不算坏（下面 EXPECTED 列出来）。

用法：python _tools/scan_replacement_chars.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPL = chr(0xFFFD)

SKIP_DIRS = {".git", "__pycache__", "node_modules", "_备份", ".venv"}
SUFFIXES = {".py", ".js", ".md", ".html", ".css", ".csv", ".ps1", ".cmd",
           ".json", ".txt"}

#: 故意把 U+FFFD 写出来当示例的地方（不是坏字符）
EXPECTED = {
    "_tools/fix_replacement_chars.py": 1,
    "_tools/test_helper_pump.py": 1,
    "程序/msyt-helper.py": 1,
}


def main() -> int:
    bad = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            s = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = s.count(REPL)
        if not n:
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        exp = EXPECTED.get(rel, 0)
        if n > exp:
            bad.append((rel, n - exp))
    if bad:
        print("发现坏字符 %d 个文件：" % len(bad))
        for f, n in bad:
            print("   %-60s 多出 %d 个" % (f, n))
        return 1
    print("全仓干净：没有意外的 U+FFFD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())