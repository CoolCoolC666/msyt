# msyt 公网隧道守护（Cloudflare quick tunnel）
#
# ★ 为什么需要这个（2026-10-26）：
#   境内按 SNI 封锁了一批域名后缀（实测 `example-tld` / `blocked-tld.example` 全被 RST），
#   命名的隧道 + 自有域名这条路在境内走不通。
#   Cloudflare 托管型后缀里 **`trycloudflare.com` 是通的** ——
#   而 quick tunnel 每次启动会给一个**随机地址**，所以要有人盯着它：
#   · 隧道掉了自动拉起
#   · 地址变了自动写回配置文件（组员重启 agent 即可，不必重下包）
#   · 每条隧道都做**探活**，不通的当场报出来
#
# ★ 一个**必须**注意的坑（踩过）：
#   `~/.cloudflared/config.yml` 是 8 月的旧配置（里面钉着另一个 tunnel id
#   和 `ingress`）。cloudflared 启动 quick tunnel 时会**读它**，
#   结果隧道**连上了但没挂 hostname**，Cloudflare 边缘一律回 404。
#   → 所以这里**必须**用一份**空配置**把默认配置顶掉（--config 指向空文件）。
#
# 用法：
#   pwsh -File tunnel.ps1 start    # 前台守护（Ctrl+C 停）
#   pwsh -File tunnel.ps1 status   # 看当前地址与在线情况
#   pwsh -File tunnel.ps1 stop     # 停掉全部隧道
param(
  [ValidateSet('start', 'stop', 'status')]
  [string]$Action = 'status',
  [int]$Count = 2,
  [int]$Port = 8765,
  [int]$ProbeEverySec = 20,
  [int]$HeartbeatSec = 60
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$here = $PSScriptRoot
$dataDir = Join-Path $here 'data'
$projectRoot = Split-Path $here -Parent
$workRoot = Split-Path $projectRoot -Parent
$memberPkg = Join-Path $workRoot 'msyt-成员端启动包'

$pidFile = Join-Path $dataDir 'tunnel.pids.json'
$endpointsFile = Join-Path $dataDir 'agent-endpoints.json'
$apiBaseFile = Join-Path $dataDir 'agent-api-base.txt'
$altFile = Join-Path $memberPkg 'server-alt.txt'
$emptyCfg = Join-Path $env:TEMP 'msyt-cf-empty.yml'
$logDir = Join-Path $dataDir 'tunnel'

function Log([string]$msg) {
  $ts = (Get-Date).ToString('HH:mm:ss')
  Write-Host ("[{0}] {1}" -f $ts, $msg)
}

function Find-Cloudflared {
  $c = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $p = Join-Path $env:LOCALAPPDATA 'Programs\cloudflared.exe'
  if (Test-Path $p) { return $p }
  return $null
}

# ★ 空配置：把 ~/.cloudflared/config.yml 顶掉（否则 quick tunnel 404）
function Ensure-EmptyConfig {
  if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
  # 写一个**只有注释**的 yaml —— cloudflared 认它，但里面没有 tunnel/ingress
  "## msyt quick tunnel：故意留空，避免读到 ~/.cloudflared/config.yml 的旧 ingress`r`n" |
    Set-Content -Path $emptyCfg -Encoding UTF8
}

function Test-Backend {
  try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/health" -f $Port) `
      -UseBasicParsing -TimeoutSec 8
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

function Get-TunnelPids {
  if (-not (Test-Path $pidFile)) { return @() }
  try {
    $d = Get-Content $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
    return @($d)
  } catch { return @() }
}

function Save-TunnelPids($items) {
  $arr = @($items | ForEach-Object {
      [ordered]@{ idx = $_.idx; pid = $_.pid; url = $_.url; log = $_.log }
    })
  ($arr | ConvertTo-Json -Depth 4) | Set-Content -Path $pidFile -Encoding UTF8
}

function Write-Endpoints($urls) {
  $clean = @($urls | Where-Object { $_ } | ForEach-Object { $_.Trim().TrimEnd('/') } | Select-Object -Unique)
  if ($clean.Count -eq 0) { return }
  $now = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')

  # ① 服务端认的主地址（会被盖进 agent 票据 / 下载页）
  $clean[0] | Set-Content -Path $apiBaseFile -Encoding UTF8

  # ② 地址池（给 GET /api/agent/endpoints 用）
  $obj = [ordered]@{
    endpoints   = @($clean)
    updated_at  = $now
    source      = 'cloudflared quick tunnel'
    note        = 'quick tunnel 每次重启地址会变；组员重启 agent 即可重新选址'
  }
  ($obj | ConvertTo-Json -Depth 4) | Set-Content -Path $endpointsFile -Encoding UTF8

  # ③ 成员启动包里的备用地址（每行一个，agent 自己读这个文件）
  if (Test-Path $memberPkg) {
    $clean | Set-Content -Path $altFile -Encoding UTF8
  }
}

function Stop-AllTunnels {
  $items = Get-TunnelPids
  foreach ($it in $items) {
    try { Stop-Process -Id ([int]$it.pid) -Force -ErrorAction SilentlyContinue } catch { }
  }
  # 兜底：命令行里带我们这个端口的 cloudflared 一并收掉
  Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*127.0.0.1:$Port*" } |
    ForEach-Object {
      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
  if (Test-Path $pidFile) {
    Rename-Item $pidFile ($pidFile + '.off') -Force -ErrorAction SilentlyContinue
  }
  Log '全部隧道已停止'
}

function Start-OneTunnel([int]$idx, [string]$exe) {
  if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $log = Join-Path $logDir ("tunnel-{0}-{1}.log" -f $idx, $stamp)
  $args = @('tunnel', '--url', ("http://127.0.0.1:{0}" -f $Port), '--no-autoupdate',
    '--config', ('"{0}"' -f $emptyCfg), '--protocol', 'http2')
  $p = Start-Process -FilePath $exe -ArgumentList $args -RedirectStandardOutput $log `
    -RedirectStandardError $log.Replace('.log', '.err.log') -WindowStyle Hidden -PassThru

  # 等地址横幅（最多 60s；创建 API 偶发超时，重试几次）
  for ($t = 0; $t -lt 90; $t++) {
    Start-Sleep -Seconds 1
    if ($p.HasExited) { return $null }
    $hit = $null
    foreach ($f in @($log.Replace('.log', '.err.log'), $log)) {
      if (Test-Path $f) {
        $m = Select-String -Path $f -Pattern '\|  https://[a-z0-9-]+\.trycloudflare\.com' `
          -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $hit = $m; break }
      }
    }
    if ($hit) {
      $url = [regex]::Match($hit.Line, 'https://[a-z0-9-]+\.trycloudflare\.com').Value
      return [pscustomobject]@{ idx = $idx; pid = $p.Id; url = $url; log = $log }
    }
  }
  return $null
}

function Test-Url([string]$url) {
  try {
    $r = Invoke-WebRequest -Uri ($url + '/api/health') -UseBasicParsing -TimeoutSec 15 `
      -Headers @{ 'User-Agent' = 'msyt-agent/1.1' }
    return [int]$r.StatusCode
  } catch {
    if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
    return 0
  }
}

# ---------------------------------------------------------------- 动作分发
switch ($Action) {

  'stop' {
    Stop-AllTunnels
    break
  }

  'status' {
    $items = Get-TunnelPids
    if ($items.Count -eq 0) {
      Log '当前没有在跑的隧道（用「启动-隧道.cmd」拉起）'
    } else {
      foreach ($it in $items) {
        $alive = $null -ne (Get-Process -Id ([int]$it.pid) -ErrorAction SilentlyContinue)
        $code = if ($alive) { Test-Url $it.url } else { 0 }
        Log ("#{0} pid={1} {2}  探活={3}  {4}" -f $it.idx, $it.pid,
          $(if ($alive) { '存活' } else { '已退出' }), $code, $it.url)
      }
    }
    if (Test-Path $endpointsFile) { Log ("地址池文件：" + $endpointsFile) }
    break
  }

  'start' {
    $exe = Find-Cloudflared
    if (-not $exe) {
      Log '✗ 找不到 cloudflared（先装好并确保在 PATH 里）'
      exit 1
    }
    Log ("cloudflared = " + $exe)

    # ★ 先探后端：源站没起，隧道起来也是 502
    if (-not (Test-Backend)) {
      Log ("✗ 后端 127.0.0.1:{0} 没响应 —— 先把 msyt 服务起起来" -f $Port)
      exit 1
    }
    Log ("后端 127.0.0.1:{0} 活着" -f $Port)

    Ensure-EmptyConfig
    Log ("空配置已就位：" + $emptyCfg)

    $items = @()
    for ($i = 1; $i -le $Count; $i++) {
      Log ("拉起 quick tunnel #{0} …" -f $i)
      $t = $null
      for ($try = 1; $try -le 3; $try++) {
        $t = Start-OneTunnel -idx $i -exe $exe
        if ($t) { break }
        Log ("  第 {0} 次失败（Cloudflare 创建接口偶发超时），5 秒后重试" -f $try)
        Start-Sleep -Seconds 5
      }
      if ($t) {
        $items += $t
        Log ("✔ 隧道 #{0} 已就绪 {1}" -f $t.idx, $t.url)
        $code = Test-Url $t.url
        Log ("  探活 /api/health → {0}{1}" -f $code, $(if ($code -eq 200) { '' } else { '  ⚠ 不通' }))
      } else {
        Log ("✗ 隧道 #{0} 拉不起来" -f $i)
      }
    }
    Save-TunnelPids $items
    if ($items.Count -gt 0) {
      Write-Endpoints ($items | ForEach-Object { $_.url })
      Log ('地址已写入：')
      Log ('  · ' + $apiBaseFile)
      Log ('  · ' + $endpointsFile)
      Log ('  · ' + $altFile)
      Log ('★ 当前公网地址（发给组员 / 填进 server-alt.txt）：')
      foreach ($it in $items) { Log ('   ' + $it.url) }
    }

    Log '进入守护（Ctrl+C 停止）…'
    $lastHb = Get-Date
    try {
      while ($true) {
        Start-Sleep -Seconds 5
        $changed = $false

        # 掉线的拉起来
        $alive = @()
        foreach ($it in $items) {
          if ($null -ne (Get-Process -Id ([int]$it.pid) -ErrorAction SilentlyContinue)) {
            $alive += $it
          } else {
            Log ("✗ 隧道 #{0} 掉了，重新拉起 …" -f $it.idx)
            $nt = $null
            for ($try = 1; $try -le 3; $try++) {
              $nt = Start-OneTunnel -idx $it.idx -exe $exe
              if ($nt) { break }
              Start-Sleep -Seconds 5
            }
            if ($nt) {
              Log ("✔ 隧道 #{0} 新地址 {1}" -f $nt.idx, $nt.url)
              $alive += $nt
              $changed = $true
            } else {
              Log ("✗ 隧道 #{0} 拉不起来" -f $it.idx)
            }
          }
        }
        if ($changed) {
          $items = $alive
          Save-TunnelPids $items
          Write-Endpoints ($items | ForEach-Object { $_.url })
          Log '★ 地址已更新（组员重启 agent 即可）'
        }

        # 心跳：汇总一行，别刷屏
        if (((Get-Date) - $lastHb).TotalSeconds -ge $HeartbeatSec) {
          $lastHb = Get-Date
          $parts = @()
          foreach ($it in $items) {
            $c = Test-Url $it.url
            $parts += ("#{0} {1}" -f $it.idx, $(if ($c -eq 200) { '200' } else { $c }))
          }
          Log ("心跳：" + ($parts -join ' | ') + "  在线 {0}/{1}" -f $items.Count, $Count)
        }
      }
    } catch {
      Log '守护结束'
    }
    Stop-AllTunnels
    break
  }
}
