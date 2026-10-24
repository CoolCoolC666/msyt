# -*- coding: utf-8 -*-
"""
fix_text_encoding.py —— 文本文件编码 / 行尾规范化
================================================================
为什么需要它
------------
本项目反复踩到同一类坑：
  ① 编辑器写文件时**保留**了原有的 BOM，而我用 `encoding="utf-8"` 读回来时
     BOM 会变成正文字符 `\ufeff`；再写 `utf-8-sig` 就**叠成两个 BOM**，
     PowerShell 只剥一个，剩下的把 `param()` 块顶到语法错误。
  ② `cmd.exe` 解析批处理时，文件里有任何**非 ASCII 字节**都可能把后续行咬坏
     （实测 `chcp 65001` 把 `-ExecutionPolicy` 啃成了 `cutionPolicy`）。
  ③ Windows PowerShell 5.1 读**无 BOM** 的 UTF-8 `.ps1` 会按 ANSI 解码，中文全乱；
     而 `.py` / `.md` 带 BOM 又会被 Python 当成源码里的一个怪字符。

所以按扩展名定规矩（幂等，重复跑不会越叠越多）：

    .ps1  → UTF-8 **带 BOM** + CRLF   （Windows PowerShell 5.1 认中文）
    .cmd  → **纯 ASCII** + CRLF       （cmd.exe 只吃 ASCII）
    .py   → UTF-8 **无 BOM** + CRLF   （Python 源码不能带 BOM）
    .md   → UTF-8 **无 BOM** + CRLF

用法：
    python _tools\\fix_text_encoding.py --all
    python _tools\\fix_text_encoding.py --ps1 程序\\web.ps1
    python _tools\\fix_text_encoding.py --check          # 只体检不改动
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 扩展名 → (编码, 是否要 BOM, 行尾)
RULES: dict[str, tuple[str, bool, str]] = {
    ".ps1": ("utf-8", True, "\r\n"),
    ".cmd": ("ascii", False, "\r\n"),
    ".bat": ("ascii", False, "\r\n"),
    ".py": ("utf-8", False, "\r\n"),
    ".md": ("utf-8", False, "\r\n"),
    ".json": ("utf-8", False, "\r\n"),
    ".js": ("utf-8", False, "\r\n"),
    ".css": ("utf-8", False, "\r\n"),
    ".html": ("utf-8", False, "\r\n"),
    ".txt": ("utf-8", False, "\r\n"),
}

SKIP_DIRS = {"__pycache__", ".git", "data", "node_modules", ".venv"}


def norm_text(text: str) -> str:
    """剥掉所有前导 BOM 字符（重复运行不会越叠越多）。"""
    while text.startswith("\ufeff"):
        text = text[1:]
    return text


def process(path: Path, check: bool) -> str:
    ext = path.suffix.lower()
    if ext not in RULES:
        return "skip"
    enc, want_bom, nl = RULES[ext]

    raw = path.read_bytes()
    # 解码：先按规则编码解，失败再退回 utf-8（可能本来就是 utf-8）
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    text = norm_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)

    if enc == "ascii":
        # 非 ASCII 字符没法用 ASCII 表示 —— 报告出来，不静默丢字
        bad = [c for c in text if ord(c) > 127]
        if bad:
            return f"FAIL  非 ASCII 字符 {len(bad)} 个（如 {bad[0]!r}）→ cmd.exe 会解析错"
        data = text.encode("ascii")
    else:
        data = text.encode("utf-8")
        if want_bom:
            data = b"\xef\xbb\xbf" + data

    if data == raw:
        return "OK    已合规"
    if check:
        return "CHECK 需规范化"
    path.write_bytes(data)
    head = "BOM" if data[:3] == b"\xef\xbb\xbf" else "no-BOM"
    return f"FIXED {head} + {nl!r}  ({len(raw)} → {len(data)} 字节)"


def main() -> int:
    ap = argparse.ArgumentParser(description="文本编码/行尾规范化")
    ap.add_argument("--all", action="store_true", help="处理项目内所有受管文件")
    ap.add_argument("--check", action="store_true", help="只体检不改动")
    ap.add_argument("--ps1", nargs="*", default=[])
    ap.add_argument("--cmd", nargs="*", default=[])
    ap.add_argument("--py", nargs="*", default=[])
    ap.add_argument("--md", nargs="*", default=[])
    args = ap.parse_args()

    targets: list[Path] = []
    for group in (args.ps1, args.cmd, args.py, args.md):
        for f in group:
            targets.append(Path(f))
    if args.all:
        for ext in RULES:
            for f in ROOT.rglob(f"*{ext}"):
                if any(part in SKIP_DIRS for part in f.parts):
                    continue
                targets.append(f)

    if not targets:
        print("没指定文件。用 --all 处理整个项目。")
        return 0

    bad = 0
    for p in targets:
        if not p.exists():
            print(f"  [缺失] {p}")
            bad += 1
            continue
        res = process(p, args.check)
        print(f"  {res}  {p.relative_to(ROOT) if p.is_absolute() and ROOT in p.parents else p}")
        if res.startswith("FAIL"):
            bad += 1

    print(f"\n处理 {len(targets)} 个文件，问题 {bad} 个"
          f"{'（未改动，仅体检）' if args.check else ''}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
