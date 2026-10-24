# -*- coding: utf-8 -*-
"""清理源文件里的 U+FFFD（UTF-8 替换字符）。

写文件时如果某个中文字符被写坏了，它会变成 U+FFFD（渲染成黑方块）。
这里按上下文把**成串**的 U+FFFD 还原成正确的字。

用法：python _tools/fix_replacement_chars.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (相对路径, 含乱码的片段, 应还原成的片段)
FIXES = [
    (os.path.join("_tools", "test_drift.py"),
     "被新回复顶上来", "被新回复顶上来", "的"),
    (os.path.join("_报告", "协作待办_2026-10-23.md"),
     "有一处 UTF-8 替换字符", "有一处 UTF-8 替换字符", ""),
]

REPL = "�"


def main() -> int:
    n = 0
    for rel, _probe, _unused, _ch in FIXES:
        pass
    # 逐文件处理：把成串的 U+FFFD 交给一张「上下文 → 正确字」的映射
    rules = [
        (os.path.join("_tools", "test_drift.py"),
         "被新回复顶上来" + REPL * 2 + "老帖",
         "被新回复顶上来的老帖"),
        (os.path.join("_报告", "协作待办_2026-10-23.md"),
         "替换字符（" + REPL * 3 + "领活）",
         "替换字符（U+FFFD，渲染成黑方块）"),
    ]
    for rel, bad, good in rules:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            print("跳过（不存在）:", rel)
            continue
        with open(p, "r", encoding="utf-8") as f:
            t = f.read()
        if REPL not in t:
            print("无需处理:", rel)
            continue
        t2 = t.replace(bad, good)
        # 兜底：把剩下的孤立 U+FFFD 删掉（避免残留）
        t2 = re.sub(REPL + "+", "", t2)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(t2)
        print(f"已修复: {rel}")
        n += 1

    # 复查全库
    bad = []
    for root, dirs, files in os.walk(ROOT):
        if any(x in root for x in ("\\data\\", "\\.git\\", "__pycache__")):
            continue
        for fn in files:
            if not fn.endswith((".py", ".js", ".html", ".css", ".md", ".txt")):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    if REPL in f.read():
                        bad.append(p.replace(ROOT, ""))
            except Exception:
                pass
    print(f"处理 {n} 个文件；剩余含 U+FFFD 的文件：{len(bad)}")
    for b in bad:
        print("   ", b)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
