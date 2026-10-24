# -*- coding: utf-8 -*-
"""
rebuild_report_docx.py —— 重建报告 docx，并在锁释放后覆盖回正式件
================================================================
一条命令走完：md → docx（暂存） → 等锁 → 覆盖 → 自检。

为什么要「等锁」这一步
----------------------
WPS/Word 打开 docx 时持有**独占锁**（同目录留 `~$文件名.docx`）。
这时写不进去（`PermissionError: [Errno 13]`），
WPS 的 COM 接口在 PowerShell 7 里也取不到活动实例，**救不了场**，
只能让用户先把那份文档关掉 —— 所以这里轮询等锁，而不是硬撞。

用法：
    python _tools\\rebuild_report_docx.py                 # 重建 + 等待 + 自检
    python _tools\\rebuild_report_docx.py --wait 300      # 多等一会儿
    python _tools\\rebuild_report_docx.py --file 分布式抓取协作方案
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REP = ROOT / "_报告"
TOOLS = ROOT / "_tools"


def run(cmd: list[str]) -> int:
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0 and r.stderr:
        print(r.stderr.rstrip()[-1500:], file=sys.stderr)
    return r.returncode


def is_locked(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description="重建报告 docx")
    ap.add_argument("--file", default="方案报告", help="报告名（不含扩展名）")
    ap.add_argument("--wait", type=int, default=120, help="等锁释放的最长秒数")
    args = ap.parse_args()

    md = REP / f"{args.file}.md"
    final = REP / f"{args.file}.docx"
    stage = REP / f"_staging_{args.file}.docx"

    if not md.exists():
        print(f"[!] 找不到源文件：{md}")
        return 1

    print(f"[1/3] 生成 docx（暂存）… {md.name}")
    if run([sys.executable, str(TOOLS / "md2docx.py"), str(md), "-o", str(stage)]) != 0:
        print("[!] 生成失败")
        return 1
    if not stage.exists():
        print("[!] 暂存件没生成")
        return 1

    waited = 0
    if is_locked(final):
        mk = final.with_name("~$" + final.stem + final.suffix)
        print(f"[2/3] {final.name} 正被占用"
              + (f"（{mk.name} 存在，多半是 WPS/Word 开着）" if mk.exists() else ""))
        print(f"      请在 WPS/Word 里把它关掉，最多等 {args.wait} 秒…")
        while is_locked(final) and waited < args.wait:
            time.sleep(2)
            waited += 2
            sys.stdout.write(".")
            sys.stdout.flush()
        print()
        if is_locked(final):
            print(f"[!] 等了 {waited}s 仍被占用。")
            print(f"    暂存件已留在：{stage.name}")
            print(f"    关掉 WPS 里的文档后，重新跑一次本脚本即可。")
            return 2
    print(f"[2/3] 覆盖正式件（等待 {waited}s）")
    final.write_bytes(stage.read_bytes())
    stage.unlink()

    print("[3/3] 自检…")
    rc = run([sys.executable, str(TOOLS / "verify_report_docx.py"), args.file])
    if rc == 0:
        print(f"\n[OK] {final.name} 已更新并通过验收")
        return 0
    print(f"\n[!] {final.name} 已覆盖，但自检未通过（rc={rc}）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
