# -*- coding: utf-8 -*-
"""
swap_docx.py —— 等 docx 的锁释放后，把暂存件覆盖回正式件
================================================================
为什么需要它
------------
WPS/Word 打开 docx 时会持有**独占锁**（同目录留下 `~$文件名.docx` 锁文件）。
这时我这边写不进去 —— 报 `PermissionError: [Errno 13] Permission denied`。
WPS 的 COM 接口在 PowerShell 7 里已经取不到活动实例，**救不了场**，
只能让用户在 WPS 里把那份文档关掉。

所以：先照常把新版生成到暂存件（本脚本的 `--stage` 已做），
再用本脚本**等锁释放 → 覆盖 → 清理**，用户只需在 WPS 里关一下文档。

用法：
    python _tools\\swap_docx.py                       # 交换 _staging_方案报告.docx → 方案报告.docx
    python _tools\\swap_docx.py --wait 120            # 最多等 120 秒
    python _tools\\swap_docx.py --file 分布式抓取协作方案
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REP = ROOT / "_报告"


def is_locked(path: Path) -> bool:
    """能不能以写模式打开 = 有没有被独占锁。"""
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return True


def lock_marker(path: Path) -> Path:
    """WPS/Word 留下的锁文件（~ + 文件名去扩展名 + .docx）。"""
    return path.with_name("~$" + path.stem + path.suffix)


def main() -> int:
    ap = argparse.ArgumentParser(description="等锁释放后覆盖 docx")
    ap.add_argument("--file", default="方案报告", help="报告名（不含扩展名）")
    ap.add_argument("--wait", type=int, default=0, help="最多等多少秒（0=不等，直接试）")
    args = ap.parse_args()

    final = REP / f"{args.file}.docx"
    stage = REP / f"_staging_{args.file}.docx"

    if not stage.exists():
        print(f"[!] 没有暂存件：{stage.name}")
        print(f"    先重新生成：python _tools\\md2docx.py \"_报告\\{args.file}.md\" "
              f"-o \"_报告\\_staging_{args.file}.docx\"")
        return 1

    waited = 0
    while is_locked(final):
        if waited >= args.wait:
            print(f"[!] 等待超时（{args.wait}s）：{final.name} 仍被占用。")
            print("    在 WPS/Word 里把这份文档关掉，再跑一次本脚本。")
            return 2
        if waited == 0:
            mk = lock_marker(final)
            print(f"[·] {final.name} 正被占用"
                  + (f"（锁文件 {mk.name} 存在，多半是 WPS/Word 开着）" if mk.exists() else ""))
            print(f"    请在 WPS/Word 里关掉它，最多等 {args.wait} 秒…")
        time.sleep(2)
        waited += 2
        sys.stdout.write(".")
        sys.stdout.flush()
    if waited:
        print()

    shutil.copyfile(stage, final)
    stage.unlink()
    print(f"[OK] 已覆盖：{final.name}（{final.stat().st_size:,} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
