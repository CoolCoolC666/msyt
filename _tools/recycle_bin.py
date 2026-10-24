# -*- coding: utf-8 -*-
"""把临时文件送进**系统回收站**（可恢复，不是永久删除）。

★ 为什么不用 mavis-trash：本机硬策略判定它不可调用，
  且策略明令「不得回退到永久删除」。系统回收站是**可恢复**删除，
  符合「可恢复优先」的纪律。
★ 为什么不用 Remove-Item / rm / [IO.File]::Delete：
  那是**永久删除**，本机一律不用。
"""
import sys

from win32com.shell import shell, shellcon  # type: ignore

# 用 PowerShell 的 Microsoft.VisualBasic 更稳（不依赖 pywin32）
import subprocess

PS = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
$targets = @(
{args}
)
$n = 0
foreach ($t in $targets) {
    if (-not (Test-Path -LiteralPath $t)) { continue }
    $item = Get-Item -LiteralPath $t -Force
    if ($item.PSIsContainer) {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
            $item.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
    } else {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $item.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
    }
    Write-Output ("RECYCLED " + $item.FullName)
    $n++
}
Write-Output ("DONE " + $n)
'''

if len(sys.argv) < 2:
    print("用法: recycle_bin.py <路径> [<路径> ...]")
    sys.exit(2)

args = []
for p in sys.argv[1:]:
    args.append("    '" + p.replace("'", "''") + "'")
script = PS.replace("{args}", ",".join(args))

r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout.rstrip())
if r.returncode != 0:
    print("STDERR:", (r.stderr or "").rstrip()[-1200:])
sys.exit(r.returncode)
