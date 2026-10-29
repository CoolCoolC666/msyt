# site-watch.ps1 -- msyt site watcher: start / stop / status
# ============================================================================
# What it does:
#   Watches whether the upstream site (bbs.cnnb.com.cn) detail endpoint is
#   alive. The upstream may serve the whole forum domain behind a 302 to the
#   public board homepage (observed 2026-10-29 00:20 -> 11:xx).
#   While it is down, the "short-posts" backlog cannot be settled, because
#   every check needs a real upstream reply.
#   As soon as the site answers again, this runs the settle job automatically
#   and then exits.
#
# Rules honoured:
#   * Manual start / stop only -- NEVER registered as an autostart service.
#   * The .cmd files are thin ASCII shims; all logic lives here.
#   * Started as a DETACHED process (Start-Process), so it survives the
#     session that launched it (a background bash task gets killed when the
#     session is recycled -- that killed the first attempt on 2026-10-29).
#   * PID file based stop: never touches processes we did not start.
#
# Usage:
#   .\site-watch.ps1 start [-IntervalMinutes 10] [-MaxHours 12]
#   .\site-watch.ps1 stop
#   .\site-watch.ps1 status
# ============================================================================
param(
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'start',
    [int]$IntervalMinutes = 10,
    [int]$MaxHours = 12
)

$ErrorActionPreference = 'Continue'
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$proj    = Split-Path -Parent $root
$dataDir = Join-Path $root 'data'
$pidFile = Join-Path $dataDir 'site-watch.pid'
$watcher = Join-Path $proj '_tools\watch_site_and_resume.py'
$logFile = Join-Path $proj '_tools\_watch_site.log'

if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir -Force | Out-Null }

function Resolve-Python {
    foreach ($name in @('python', 'python3')) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c -and $c.Source) { return $c.Source }
    }
    # Fallback: the usual per-user install path (probe, do not hardcode blindly)
    $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    if (Test-Path $guess) { return $guess }
    return $null
}

function Get-WatchPid {
    if (-not (Test-Path $pidFile)) { return 0 }
    $raw = (Get-Content $pidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if (-not $raw) { return 0 }
    $p = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
    if ($p) { return [int]$raw }
    return 0
}

function Show-Status {
    $pid1 = Get-WatchPid
    if ($pid1) {
        Write-Host "site watcher: RUNNING (pid $pid1)"
    } else {
        Write-Host "site watcher: NOT running"
    }
    if (Test-Path $logFile) {
        Write-Host "--- last log lines ($logFile) ---"
        Get-Content $logFile -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "no log yet ($logFile)"
    }
}

switch ($Action) {
    'start' {
        $cur = Get-WatchPid
        if ($cur) {
            Write-Host "already running (pid $cur). Nothing to do."
            break
        }
        if (-not (Test-Path $watcher)) {
            Write-Host "watcher script missing: $watcher"
            exit 1
        }
        $py = Resolve-Python
        if (-not $py) {
            Write-Host "python not found in PATH."
            exit 1
        }
        $args = @($watcher, '--interval', ($IntervalMinutes * 60), '--max-hours', $MaxHours, '--workers', '3')
        $proc = Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $proj `
            -WindowStyle Hidden -PassThru
        Set-Content -Path $pidFile -Value $proc.Id -Encoding ASCII
        Write-Host "site watcher started (pid $($proc.Id))"
        Write-Host "  interval: $IntervalMinutes min   max hours: $MaxHours"
        Write-Host "  log     : $logFile"
    }
    'stop' {
        $cur = Get-WatchPid
        if (-not $cur) {
            Write-Host "not running."
            if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }
            break
        }
        Stop-Process -Id $cur -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }
        Write-Host "site watcher stopped (pid $cur)"
    }
    'status' { Show-Status }
    'restart' {
        & $MyInvocation.MyCommand.Path stop
        Start-Sleep -Milliseconds 800
        & $MyInvocation.MyCommand.Path start -IntervalMinutes $IntervalMinutes -MaxHours $MaxHours
    }
}
