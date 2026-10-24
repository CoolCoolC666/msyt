# web.ps1 —— msyt 本地 Web 前端：启动 / 停止 / 状态
# ============================================================================
# 遵循既有约定：
#   · **手动启停，不注册开机自启**（注册自启会让每次开机都挂一个常驻进程）
#   · 薄壳（.cmd）只负责调用本脚本，真正的逻辑在这里
#   · 启动前先探测**后端是否真的能用**（不只看端口在不在），避免"以为起来了其实没起"
#   · 停止按 PID 文件走
#
# ⚠️ 踩过的坑（留档）：
#   ① cmd.exe 解析批处理时，文件里有任何非 ASCII 字节都可能把后续行咬坏
#      → 所有 .cmd 一律纯 ASCII + CRLF；中文提示只放在本 .ps1 里
#   ② Windows PowerShell 5.1 读无 BOM 的 UTF-8 .ps1 会按 ANSI 解码，中文全乱
#      → 本文件存成 UTF-8 **带 BOM**
#   ③ 等待循环用「端口在监听」判断过 → 端口在监听 ≠ 服务能用
#      → 现在直接探 /api/health
#
# 用法：
#   .\web.ps1 start [-Port 8765] [-NoBrowser]
#   .\web.ps1 stop
#   .\web.ps1 status
#
# ---- 公网（Cloudflare 隧道）----
#   默认**自动**：读 data/tunnel-domain.txt，里面有域名就一并把公网拉起来
#   （成员端就是从这个公网地址连进来的 —— 启动器必须自带这一步，
#     否则双击启动后公网是断的，组员连不上）。
#   显式控制：
#     -TunnelDomain xxx  用指定域名（覆盖配置文件）
#     -NoTunnel          只开本地，**不暴露公网**
# ============================================================================
param(
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'start',
    [int]$Port = 8765,
    [string]$BindHost = '127.0.0.1',
    [switch]$NoBrowser,
    # ---- Cloudflare 隧道（把小组成员接到这个站上）----
    [switch]$Tunnel,                       # 强制拉起隧道（域名取自配置/参数）
    [string]$TunnelDomain = '',            # 显式指定公网域名（覆盖配置文件）
    [switch]$NoTunnel                      # ★ 只要本地，**不开公网**
)

$ErrorActionPreference = 'Continue'
$root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir   = Join-Path $root 'data'
$pidFile   = Join-Path $dataDir 'web.pid'
$tunPid    = Join-Path $dataDir 'web.tunnel.pid'
$outLog    = Join-Path $dataDir 'web.out.log'
$errLog    = Join-Path $dataDir 'web.err.log'
$cfExe     = Join-Path $env:LOCALAPPDATA 'Programs\cloudflared.exe'
#: ★ 公网域名配置（谁公网地址是"对"的，只写这一处）
$domainFile = Join-Path $dataDir 'tunnel-domain.txt'
#: ★ 隧道协议（2026-10-23 加）：'auto' | 'quic' | 'http2'
#:   实测踩过：QUIC（UDP）到 Cloudflare 边缘会「no recent network activity」超时，
#:   表现是公网一直 502，而本地服务好好的。
#:   改用 http2（走 TCP）通常立刻就通 —— 所以默认 'auto'：
#:   先试 quic，不行自动换 http2。
$protocolFile = Join-Path $dataDir 'tunnel-protocol.txt'

# ---------------------------------------------------------------- 公网域名
function Get-TunnelDomain {
    <#
      读 data/tunnel-domain.txt —— 里面是这台机器对外的域名。
      ★ 为什么要做成**配置**而不是写死在脚本里：
        域名会变（换域名、换机器、测试域名），
        写死就意味着每次换域名都要改脚本、还要记得改薄壳。
      ★ 为什么启动器**必须**读它：
        成员端是从公网地址连进来的，
        启动器不带这一步 → 双击启动后公网是断的 → 组员连不上。
    #>
    if ($TunnelDomain) { return $TunnelDomain.Trim() }
    try {
        if (Test-Path $domainFile) {
            $t = (Get-Content $domainFile -Raw -ErrorAction SilentlyContinue).Trim()
            if ($t) { return ($t -split '\s+')[0] }
        }
    } catch { }
    return ''
}

function Get-RespBody($resp) {
    <#
      把「异常里的响应」读成字符串。
      ★ 为什么不用 GetResponseStream：
        Windows PowerShell 5.1 的 HttpWebResponse 有它；
        **PowerShell 7** 的异常里放的是 HttpResponseMessage（**没有**那个方法），
        要用 Content.ReadAsStringAsync()。两套都得能用。
    #>
    if (-not $resp) { return '' }
    try {
        if ($resp.PSObject.Methods.Name -contains 'GetResponseStream') {
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
            $b = $sr.ReadToEnd(); $sr.Close(); return $b
        }
    } catch { }
    try {
        if ($null -ne $resp.Content) {
            if ($resp.Content -is [string]) { return [string]$resp.Content }
            return $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }
    } catch { }
    return ''
}

function Test-TunnelReachable([string]$Domain, [int]$TimeoutSec = 8) {
    <#
      隧道**真的通**了吗？——判据要打在一个「请求会真的转发到源站」的地方。

      ★ ★ 为什么**不能**打主域名
        主域名挂着 Cloudflare Access（匿名访问会拿到**登录页**，
        状态码 200、内容是 Access 的 HTML）——
        哪怕源站根本没连上，边缘也会照样返回 200。
        实测踩过两次：隧道早断了，脚本却报「隧道已连接」；
        我自己的探针也栽在同一个坑里。

      ★ 正确判据：打 **agentapi 子域名**
        它在 Access 里是 **bypass** 的 → 请求**一定转发到源站**；
        我们的程序对它只开放 /api/agent/*，所以
        /api/agent/me 会返回 **401**（未带令牌）、其它路径 **403** ——
        这两个状态码**只可能来自我们的程序**（Cloudflare 自己出错是 5xx/530）。
        拿到 401/403 ⇒ 请求确实到达了源站 ⇒ 隧道通。
    #>
    if (-not $Domain) { return $false }
    $apiBase = ''
    try {
        $af = Join-Path $dataDir 'agent-api-base.txt'
        if (Test-Path $af) { $apiBase = (Get-Content $af -Raw).Trim() }
    } catch { }
    if (-not $apiBase) { $apiBase = "https://agentapi.$Domain" }
    $url = "$($apiBase.TrimEnd('/'))/api/agent/me"
    $code = 0
    $body = ''
    try {
        $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        $code = [int]$r.StatusCode
        $body = [string]$r.Content
    } catch {
        $resp = $null
        try { $resp = $_.Exception.Response } catch { }
        if (-not $resp) { return $false }   # 连不上（超时/拒绝）→ 不通
        try { $code = [int]$resp.StatusCode } catch { }
        $body = Get-RespBody $resp
    }
    # 401/403 是我们程序的正常反应（未带令牌 / 该域名只开放 agent 路径）
    if ($code -eq 401 -or $code -eq 403) { return $true }
    # 兜底：正文是我们自己的 JSON（带 detail）也算通
    return ($body -match '"detail"')
}

function Test-TunnelAlive {
    <# 隧道是不是已经在跑了（避免重复起一个 cloudflared 抢同一个 token） #>
    if (-not (Test-Path $tunPid)) { return $false }
    $tid = 0
    try { $tid = [int](Get-Content $tunPid -Raw).Trim() } catch { return $false }
    if (-not $tid) { return $false }
    return (Test-Alive $tid)
}

function Write-Info($m)  { Write-Host $m -ForegroundColor Cyan }
function Write-Ok($m)    { Write-Host $m -ForegroundColor Green }
function Write-Warn($m)  { Write-Host $m -ForegroundColor Yellow }
function Write-Err($m)   { Write-Host $m -ForegroundColor Red }

# ---------------------------------------------------------------- 端口占用者
function Get-PortOwner([int]$Port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
             Select-Object -First 1
        if ($c) { return [int]$c.OwningProcess }
    } catch { }
    return 0
}

function Test-Alive([int]$ProcId) {
    if (-not $ProcId) { return $false }
    try { return $null -ne (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) }
    catch { return $false }
}

# ---------------------------------------------------------------- 后端真的能用吗
function Test-ApiReady([int]$Port, [int]$TimeoutSec = 2) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec $TimeoutSec
        return ($h.ok -eq $true)
    } catch { return $false }
}

function Get-ApiInfo([int]$Port) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 5 }
    catch { return $null }
}

# ------------------------------------------------------------- Cloudflare 隧道
function Start-Cloudflared([int]$WebPort, [string]$Domain) {
    # ★ 已经在跑就别再起一个：
    #   同一个 tunnel token 起两个 cloudflared 会互相抢（连接被顶掉），
    #   表现是"公网时好时坏"。双击启动器时最容易踩（服务本来就在跑）。
    if (Test-TunnelAlive) {
        Write-Ok "隧道已在运行。"
        if ($Domain) { Write-Host "  公网地址 : https://$Domain/" -ForegroundColor Green }
        return 0
    }
    $exe = $cfExe
    if (-not (Test-Path $exe)) {
        $c = Get-Command cloudflared -ErrorAction SilentlyContinue
        if ($c) { $exe = $c.Source }
        else {
            Write-Err "找不到 cloudflared（$cfExe）。装好后再带 -Tunnel 启动。"
            return 0
        }
    }
    $tokenFile = Join-Path $dataDir 'cloudflared-tunnel.token'
    if (-not (Test-Path $tokenFile)) {
        Write-Err "缺少隧道 token：$tokenFile"
        return 0
    }
    $token = (Get-Content $tokenFile -Raw).Trim()
    $tunLog = Join-Path $dataDir 'cloudflared.log'
    $tunErr = Join-Path $dataDir 'cloudflared.err.log'

    # ★ 协议（2026-10-23）：默认 auto —— 先试 quic，不通自动换 http2。
    #   实测踩过：QUIC（UDP）到 Cloudflare 边缘「no recent network activity」超时，
    #   表现是公网一直 502 而本地服务好好的 —— 换成 http2（走 TCP）立刻就通。
    $proto = 'auto'
    if (Test-Path $protocolFile) {
        try {
            $v = (Get-Content $protocolFile -Raw).Trim().ToLower()
            if ($v -in @('quic', 'http2')) { $proto = $v }
        } catch { }
    }

    # 起一个隧道并等它就绪；成功返回 $true
    $tryOne = {
        param([string]$Protocol)
        $argsList = @('tunnel', 'run', '--token', $token)
        if ($Protocol) { $argsList += @('--protocol', $Protocol) }
        Write-Info "拉起 Cloudflare 隧道（协议 $Protocol）…"
        $proc = Start-Process -FilePath $exe `
                              -ArgumentList $argsList `
                              -WorkingDirectory $root `
                              -RedirectStandardOutput $tunLog `
                              -RedirectStandardError  $tunErr `
                              -WindowStyle Hidden -PassThru
        $proc.Id | Out-File -FilePath $tunPid -Encoding ascii

        # 等隧道真的连上（cloudflared 把 "Registered tunnel connection" 打在 stderr）
        $ready = $false
        for ($i = 0; $i -lt 24; $i++) {
            Start-Sleep -Milliseconds 500
            if (-not (Test-Alive $proc.Id)) { break }
            $txt = ""
            if (Test-Path $tunErr) { $txt += (Get-Content $tunErr -Raw -ErrorAction SilentlyContinue) }
            if (Test-Path $tunLog) { $txt += (Get-Content $tunLog -Raw -ErrorAction SilentlyContinue) }
            if ($txt -match 'Registered tunnel connection') { $ready = $true; break }
        }
        # ★ 判据要"探真实效果"，不能只看日志，也不能只看主域名：
        #   ① QUIC 连边缘节点偶尔超时，会走退避重试，日志里迟迟没有
        #      "Registered tunnel connection"，但隧道其实已经能转发公网请求了。
        #   ② 主域名挂着 Access，匿名访问**永远**拿到登录页（200）——
        #      拿它当判据会把"根本没通"误报成"已连接"（实测踩过）。
        #   所以最终判据 = **打 agentapi 子域名，拿到我们自己的 JSON 401**
        #   （那个域名 Access bypass，请求一定转发到源站）。
        if (-not $ready) {
            for ($i = 0; $i -lt 24; $i++) {
                Start-Sleep -Milliseconds 500
                if (-not (Test-Alive $proc.Id)) { break }
                if (Test-TunnelReachable -Domain $Domain) { $ready = $true; break }
            }
        }
        return @($ready, $proc.Id)
    }

    if ($proto -eq 'auto') {
        # 先 quic，不通就换 http2（TCP）—— UDP 被限时 http2 常常能通
        $r = & $tryOne 'quic'
        if (-not $r[0]) {
            Write-Warn "QUIC 没能连上 Cloudflare 边缘，自动改用 http2（走 TCP）重试…"
            try {
                if ($r[1]) { Stop-Process -Id ([int]$r[1]) -Force -ErrorAction SilentlyContinue }
            } catch { }
            Start-Sleep -Seconds 1
            $r = & $tryOne 'http2'
        }
        $ok = [bool]$r[0]
    } else {
        $r = & $tryOne $proto
        $ok = [bool]$r[0]
    }

    if (-not $ok) {
        Write-Warn "隧道 12 秒内没就绪（本地服务本身已可用，可以稍后重试隧道）。"
        Write-Host "  隧道日志: $tunErr" -ForegroundColor DarkGray
        return 0
    }
    Write-Ok "隧道已连接。"
    if ($Domain) {
        Write-Host "  公网地址 : https://$Domain/" -ForegroundColor Green
        Write-Host "            （组员从这个地址进来；agent 走 agentapi 子域名）" -ForegroundColor DarkGray
    }
    return 0
}

function Stop-Cloudflared() {
    if (-not (Test-Path $tunPid)) { return }
    $tid = 0
    try { $tid = [int](Get-Content $tunPid -Raw).Trim() } catch { }
    if (Test-Alive $tid) {
        Write-Info "停止隧道 PID $tid …"
        try { Stop-Process -Id $tid -Force -ErrorAction Stop } catch { }
    }
    if (Test-Path $tunPid) { Remove-Item $tunPid -Force -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------------ 启动
function Start-Web {
    if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir -Force | Out-Null }

    # ★ 公网要不要开：**自动**（读 data/tunnel-domain.txt）
    #   双击「启动-web.cmd」时既没有 -Tunnel 也没有 -NoTunnel → 走自动：
    #   配置文件里有域名就一并把公网拉起来 —— 成员端就是从公网连进来的，
    #   启动器不自带这一步，组员就连不上。
    $domain = ''
    $wantTunnel = $false
    if (-not $NoTunnel) {
        $domain = Get-TunnelDomain
        if ($domain) { $wantTunnel = $true }
        elseif ($Tunnel) {
            Write-Warn "没有配置公网域名（$domainFile），本次只开本地。"
            Write-Host "  想开公网：把域名写进那个文件，或用 -TunnelDomain 指定。" -ForegroundColor DarkGray
        }
    }
    if ($TunnelDomain) { $domain = $TunnelDomain.Trim() }
    if ($Tunnel) { $wantTunnel = $true }

    # ① 已经在跑？（探 API，不只看端口）
    if (Test-ApiReady -Port $Port) {
        Write-Ok "服务已在运行： http://$BindHost`:$Port/"
        $h = Get-ApiInfo -Port $Port
        if ($h) {
            Write-Host ("  帖 {0} / 楼层 {1} / FTS {2} / 库 {3}" -f `
                $h.threads, $h.posts, $h.fts, (Split-Path $h.db_path -Leaf))
        }
        if ($wantTunnel) { Start-Cloudflared -WebPort $Port -Domain $domain }
        if (-not $NoBrowser) { Start-Process "http://$BindHost`:$Port/" }
        return 0
    }

    # ② 端口被别人占了？（不是我们的服务）
    $owner = Get-PortOwner -Port $Port
    if ($owner) {
        $p = Get-Process -Id $owner -ErrorAction SilentlyContinue
        $nm = if ($p) { $p.ProcessName } else { '?' }
        Write-Warn "端口 $Port 被占用（PID $owner / $nm），但 /api/health 无响应。"
        Write-Err "换个端口再来： .\web.ps1 start -Port 8766"
        return 1
    }

    # ③ 启动
    Write-Info "启动中… http://$BindHost`:$Port/"
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { Write-Err "找不到 python，请先确认 Python 在 PATH 里。"; return 1 }

    $cliArgs = @('-m', 'msyt', 'web', '--host', $BindHost, '--port', "$Port")
    $proc = Start-Process -FilePath $py.Source `
                          -ArgumentList $cliArgs `
                          -WorkingDirectory $root `
                          -RedirectStandardOutput $outLog `
                          -RedirectStandardError  $errLog `
                          -WindowStyle Hidden `
                          -PassThru
    $proc.Id | Out-File -FilePath $pidFile -Encoding ascii

    # ④ 等**后端可用**（探 /api/health），不是等端口
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-Alive $proc.Id)) { break }        # 进程死了就别等了
        if (Test-ApiReady -Port $Port -TimeoutSec 2) { $ok = $true; break }
    }

    if (-not $ok) {
        Write-Err "启动失败：后端在 30 秒内没有就绪。"
        if (Test-Path $errLog) {
            Write-Host "---- stderr 末尾 ----" -ForegroundColor DarkGray
            Get-Content $errLog -Tail 25 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
        }
        if (Test-Path $outLog) {
            Write-Host "---- stdout 末尾 ----" -ForegroundColor DarkGray
            Get-Content $outLog -Tail 15 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
        }
        return 1
    }

    Write-Ok "已启动（PID $($proc.Id)）。"
    Write-Host "  地址   : http://$BindHost`:$Port/"
    if ($wantTunnel) { Start-Cloudflared -WebPort $Port -Domain $domain }
    Write-Host "  数据库 : $dataDir\msyt.db"
    Write-Host "  日志   : $outLog"
    Write-Host "  停止   : .\停止-web.cmd" -ForegroundColor DarkGray
    if ($wantTunnel -and $domain) {
        Write-Host "  公网   : https://$domain/" -ForegroundColor Green
        Write-Host "            （组员从这个地址进来；agent 走 agentapi 子域名）" -ForegroundColor DarkGray
    }
    if (-not $NoBrowser) { Start-Process "http://$BindHost`:$Port/" }
    return 0
}

# ------------------------------------------------------------------------ 停止
function Stop-Web {
    $procId = 0
    if (Test-Path $pidFile) {
        try { $procId = [int](Get-Content $pidFile -Raw).Trim() } catch { }
    }
    if (-not (Test-Alive $procId)) {
        $procId = Get-PortOwner -Port $Port
    }
    if (-not $procId) {
        Write-Warn "没找到在跑的服务（端口 $Port 无监听）。"
        if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }
        return 0
    }
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    $name = if ($p) { $p.ProcessName } else { '?' }
    Write-Info "停止 PID $procId ($name) …"
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 600
        if (Test-Alive $procId) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
        Write-Ok "已停止。"
    } catch {
        Write-Err "停止失败：$($_.Exception.Message)"
        return 1
    }
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }
    Stop-Cloudflared
    return 0
}

# ------------------------------------------------------------------------ 状态
function Show-Status {
    $h = Get-ApiInfo -Port $Port
    if ($h) {
        $owner = Get-PortOwner -Port $Port
        Write-Ok "运行中：PID $owner  http://127.0.0.1:$Port/"
        Write-Host ("  帖 {0} / 楼层 {1} / FTS {2} / 库 {3}" -f `
            $h.threads, $h.posts, $h.fts, (Split-Path $h.db_path -Leaf))
    } elseif (Get-PortOwner -Port $Port) {
        Write-Warn "端口 $Port 有监听，但 /api/health 无响应（服务异常）。"
    } else {
        Write-Warn "未运行（端口 $Port 无监听）。"
    }
    # ---- 公网（隧道）状态 ----
    $domain = Get-TunnelDomain
    if (Test-TunnelAlive) {
        Write-Ok "公网隧道：运行中"
        if ($domain) {
            Write-Host "  公网地址 : https://$domain/" -ForegroundColor Green
            if (Test-TunnelReachable -Domain $domain) {
                Write-Host "  可达性   : 通（已打到源站）" -ForegroundColor DarkGray
            } else {
                Write-Host "  可达性   : **不通**（进程在，但请求打不到源站）" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Warn "公网隧道：未运行"
        if ($domain) {
            Write-Host "  目标地址 : https://$domain/（未通）" -ForegroundColor DarkGray
            Write-Host "  启动它   : .\启动-web.cmd（会自动拉起隧道）" -ForegroundColor DarkGray
        }
    }
    return 0
}

# ------------------------------------------------------------------------ 分派
switch ($Action) {
    'start'   { exit (Start-Web) }
    'stop'    { exit (Stop-Web) }
    'status'  { exit (Show-Status) }
    'restart' { Stop-Web | Out-Null; Start-Sleep -Milliseconds 800; exit (Start-Web) }
}
