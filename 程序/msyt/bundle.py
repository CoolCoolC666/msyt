# -*- coding: utf-8 -*-
"""
bundle.py —— 给成员端的「启动包」（实时生成，网址动态注入）
================================================================================
为什么要有这个包
----------------
成员机器上**没有** msyt 这个包（`python -m msyt` 只会报 No module named msyt），
所以「给成员一条命令让他自己敲」这条路走不通。

于是改成：**网站把命令以下载的形式传给组员，组员运行开启**。

包里有什么
----------
    msyt/                  采集器源码（与管理员机器上**完全一致**）
    启动采集.cmd            装好依赖 → 直接开跑
    启动助手.cmd            装好依赖 → 起本地助手（网页可一键启动）
    msyt-helper.py         本地助手（标准库单文件）
    说明.txt

★ **网址是动态的**
  包是**每次下载时现场生成**的，`--server` 用的是**agent 专用的 API 地址**
  （写在 `程序/data/agent-api-base.txt`；改域名/端口后改这里 + 重启即可，
  不用重新打包 —— 下次下载自动就是新的）。

  ★ 为什么 agent 要用**专用子域名**而不是网页那个：
    成员机器上的 agent 没有浏览器、没有 Cloudflare Access 的会话 Cookie，
    打网页那个域名会被 Access 挡下（返回登录页 HTML）。
    专用子域名只开放 `/api/agent/*`，两边互不影响。
  域名/端口变了不用重新打包 —— 下一次下载自动就是新的。

★ **密码不落成员磁盘**
  包里只有一张**一次性票据**（限时、用完即焚），agent 启动时用它换 JWT。
  票据是给「拿到包就能跑」用的，不是长期凭证。
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from typing import Optional, Tuple

#: 成员端要装的依赖（越少越好 —— 每个包都是一道坎）
HELPER_DEPS = ["requests>=2.28", "beautifulsoup4>=4.11"]

#: 说明文本（放在包里，中文；.cmd 里不放中文 —— 见 memory 里的 .cmd 编码坑）
README = """民生e点通 · 成员端协作采集启动包
================================================

【这是什么】
这是「网站实时生成」的成员端启动包。
你从网页上下载它 → 解压 → 运行里面的「启动采集.cmd」就能开始协助采集正文。

【怎么用（三步）】
  1) 先在浏览器里登录采集网站（你刚下载这个包的那个网页）
     —— 这一步很关键：网站登录成功，服务器才会给你签发**启动凭证**。
  2) 解压本包到任意目录
  3) 双击「启动采集.cmd」
       - 第一次运行会自动装两个依赖（requests、beautifulsoup4），需要联网
       - 然后**直接开跑**（包里的 `agent-ticket.txt` 就是启动凭证，
         **30 天内有效、可以反复用**）—— **不用输密码**
       - 这个凭证是**绑定你这个账号**的：只有你能用。
         管理员把你的账号停用后，它就立刻失效了。
       - 万一凭证过期了，它会转而问你邮箱和密码
         （输密码时会看到 `*` 一个一个冒出来，看得见就说明在输入）
  窗口里会实时显示领活与采集进度；**关掉窗口即停止**。
  ★ 这个包**下载一次就行**，不用反复回网页重新下载。
    换账号了才需要重新下载（新包里的凭证跟着新账号走）。

【不想用包里的凭证？用另一个文件】
  双击「启动助手.cmd」→ 它会在本机开一个「助手」（127.0.0.1:8766），
  之后回到网页点一下「启动本机 agent」就行，网页里还能看到本机 agent 的实时日志。

【为什么 agent 用的是另一个网址】
  包里的 `--server` 指向 `agentapi.…`（**专用子域名**），不是网页那个域名。
  原因：agent 没有浏览器、没有 Access 的会话 Cookie，打网页那个域名会被
  Cloudflare Access 挡下（返回的是登录页 HTML，不是数据）。
  专用子域名只开放 `/api/agent/*`，网页那边照旧受 Access 保护 —— 两边互不影响。

【包是不是旧的？】
  看包里的「版本.txt」，以及程序启动时第一行打印的 `msyt agent x.y.z`。
  要是网页上多了新功能而你的包没有 → 回网页**重新下载一次**即可
  （包是每次下载现场生成的，永远是最新的）。

【安全说明】
  · 这个包里**没有你的密码**，也没有长期凭证
  · 采集到的正文只存在内存里，回传成功立即释放，**不落盘**
  · 它只做只读采集：读列表页、读帖子正文，不发帖、不回复、不点赞

【为什么这样设计】
  站方的页面**一个 CORS 头都不给**，浏览器里的网页不能直接去采；
  必须由「成员自己机器上的程序」去请求，站方看到的才是多个分散的普通用户。

【出问题怎么办】
  · 提示缺少依赖 → 在本目录按住 Shift 右键 →「在此处打开 PowerShell」，执行：
        python -m pip install requests beautifulsoup4
  · 提示连不上服务器 → 检查地址是否对（见「server.txt」）
  · 想看它到底干了什么 → 窗口里就是实时日志
"""


def _cmd(name: str, lines: list) -> str:
    """拼一个 .cmd。

    ⚠ 纪律：.cmd **纯 ASCII + CRLF**，中文只放 .py / .txt。
       之前踩过：cmd 里放中文 + `chcp 65001` 放在中段，
       后续行会被咬坏（`-ExecutionPolicy` 变成 `cutionPolicy`）。

    ⚠ 拼装陷阱：先用 "\\n" 拼好，**最后一次性**换成 "\\r\\n"。
       先用 "\\r\\n" 拼再 replace("\\n" → "\\r\\n") 会得到 "\\r\\r\\n" ——
       每行之间多一个空行（cmd 能跑，但难看，而且 `if` 块会被空行截断）。
    """
    body = "\n".join(lines)
    return "@echo off\r\n" + body.replace("\n", "\r\n") + "\r\n"


def _code_build() -> str:
    """构建号 = agent.py 的 mtime（与 agent 自报的构建号同源，好让组员对比）。"""
    try:
        return str(int(os.path.getmtime(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py"))))
    except OSError:                                 # noqa: BLE001
        return "0"


def build_bundle(server: str, ticket: str, msyt_dir: str,
                 helper_src: str = "",
                 cf_id: str = "", cf_secret: str = "",
                 server_alt: str = "") -> Tuple[bytes, str]:
    """生成启动包。返回 (zip 字节, 文件名)。

    `server`：**主用**控制面地址（由调用方从请求里推导 ——
        即「下载者此刻访问的地址」）
    `server_alt`：**备用**控制面地址（服务器自己的部署配置）
    ★ 两个地址是「**本地浏览器 + 我们的服务器**协同」的结果（2026-10-23）：
      · 浏览器这边告诉服务器「我是从哪儿访问的」
      · 服务器这边告诉包「我部署在哪儿」
      agent 启动时**自己探一下**，主地址不通就换备用 ——
      组员不必为了换个地址重下包。
    `ticket`：绑定该账号的启动凭证（30 天、可重复兑换）
    `msyt_dir`：msyt 包所在目录
    `cf_id` / `cf_secret`：Cloudflare Access 的 **service token**。
        ★ 成员机器上的 agent 没有浏览器、没有 Access 的会话 Cookie，
          不带 service token 会被 Access 挡下（返回登录页 HTML）。
          放在包内的 `cf-access.txt`（**不进命令行** —— 命令行在进程列表里是公开的）。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # ---- 1. msyt 包源码 ----
        pkg = os.path.join(msyt_dir, "msyt")
        for root, dirs, files in os.walk(pkg):
            # ★ web/ 是**管理员的界面**，成员端用不上 —— 不塞进去（省体积、少困惑）
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", "web")]
            for fn in files:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, msyt_dir)
                z.write(full, rel)

        # ---- 2. 启动脚本 ----
        # 启动采集.cmd：不带任何凭据 → 运行时**交互式问**邮箱和密码
        z.writestr("启动采集.cmd", _cmd("run", [
            "setlocal",
            "cd /d \"%~dp0\"",
            "chcp 65001 >nul",
            "echo.",
            "echo === msyt agent (member side) ===",
            "echo.",
            "echo [1/3] checking python ...",
            "python --version >nul 2>nul || (echo Python not found. Install Python 3.10+ first. & pause & exit /b 1)",
            "echo [2/3] checking dependencies ...",
            "python -c \"import requests,bs4\" >nul 2>nul || (",
            "  echo     installing requests + beautifulsoup4 ...",
            "  python -m pip install --disable-pip-version-check -q requests beautifulsoup4",
            "  python -c \"import requests,bs4\" >nul 2>nul || (echo     install failed. Run: python -m pip install requests beautifulsoup4 & pause & exit /b 1)",
            ")",
            "echo [3/3] starting ...",
            "echo.",
            "echo     server = " + server,
            "echo     It will ask for your email and password next.",
            "echo     Use the same account you log in with on the website.",
            "echo.",
            "echo     [If it says ConnectionError / cannot reach the server]",
            # ★ 提示行里**绝不能出现 "->"**：cmd.exe 把 ">" 当输出重定向符！
            #   "echo   -> run xxx" 会把提示语写进一个叫 "run" 的文件（垃圾文件），
            #   而行尾的 "->"（> 后面没文件名）直接报
            #   "The syntax of the command is incorrect." → 批处理中断 → 窗口秒关（闪退）。
            "echo      - run the self-check file in this folder to see what is blocked.",
            "echo      - or try: python -m msyt agent --server " + server + " --diagnose",
            "echo.",
        ] + (["echo     alt    = " + server_alt,
              "echo     (the agent probes both and picks whichever answers)"]
             if server_alt else []) + [
            "python -m msyt agent --server " + server
            + ((" --server-alt " + server_alt) if server_alt else ""),
            "echo.",
            "echo agent stopped.",
            "pause",
        ]))

        # 启动助手.cmd：助手是**常驻**的。
        #   包里的凭证现在 30 天有效、可重复兑换（2026-10-23 改），
        #   所以助手第二次启动也没问题。
        #   助手自己按这个顺序找凭证：
        #     ① 网页点「启动本机 agent」时递过来的 JWT（7 天有效，够它跑）
        #     ② 包里的 agent-ticket.txt（30 天，可重复）
        z.writestr("启动助手.cmd", _cmd("helper", [
            "setlocal",
            "cd /d \"%~dp0\"",
            "chcp 65001 >nul",
            "python --version >nul 2>nul || (echo Python not found. & pause & exit /b 1)",
            "python -c \"import requests,bs4\" >nul 2>nul || (",
            "  python -m pip install --disable-pip-version-check -q requests beautifulsoup4",
            ")",
            "echo starting local helper on 127.0.0.1:8766 ...",
            "echo (go back to the web page and click the start button)",
            "echo.",
            "echo [If the web page says \"no local helper detected\"]",
            # ★ 同上：这里原来用的是 "->"，是启动助手.cmd 闪退的真凶
            #   （行尾那个 "->" 让 cmd 报语法错并中断批处理）。
            "echo   - Edge may be blocking \"local network access\".",
            "echo   - Click the icon at the LEFT of the address bar,",
            "echo      \"Allow this site to access your local network\".",
            "echo   - Or double-click the self-check file to see what is blocked.",
            "echo.",
            "python msyt-helper.py --server " + server
            + ((" --server-alt " + server_alt) if server_alt else ""),
            "pause",
        ]))

        # ---- 2b. 自检.cmd：双击就能诊断「到底哪一环不通」 ----
        #   ★ 为什么必须有（2026-10-23 晚上组员实测）：
        #     组员只看到一句 ConnectionError，不知道是 DNS / TCP / 代理 / 服务端。
        #     这个脚本逐项测一遍，最后给出**可执行的结论**。
        #     典型场景：「Edge 能打开网站，Python 却连不上」
        #     → Edge 读 Windows 系统代理，requests 不读 → 直连不通。
        z.writestr("自检.cmd", _cmd("selfcheck", [
            "setlocal",
            "cd /d \"%~dp0\"",
            "chcp 65001 >nul",
            "echo.",
            "echo === msyt self-check (connectivity) ===",
            "echo.",
            "python --version >nul 2>nul || (echo Python not found. Install Python 3.10+ first. & pause & exit /b 1)",
            "python -c \"import requests\" >nul 2>nul || (",
            "  python -m pip install --disable-pip-version-check -q requests",
            ")",
            "python -m msyt.selfcheck",
            "echo.",
            "pause",
        ]))

        # ---- 3. 本地助手 ----
        if helper_src and os.path.exists(helper_src):
            with open(helper_src, "rb") as f:
                z.writestr("msyt-helper.py", f.read())

        # ---- 4. 说明 ----
        z.writestr("说明.txt", README +
                   "\n\n控制面地址（下载时按「你此刻访问的地址 + 服务器部署配置」生成）：\n"
                   "    主用：" + server + "\n" +
                   ("    备用：" + server_alt + "\n"
                    "    （agent 启动时会自己探一下，主地址不通就换备用）\n"
                    if server_alt else ""))
        z.writestr("server.txt", server + "\n")
        if server_alt:
            z.writestr("server-alt.txt", server_alt + "\n")
        # ★ 启动凭证：网站登录后签发的凭证，**绑定到下载者的账号**。
        #   30 天内有效、**可以反复用**（2026-10-23 改：
        #   原来是一次性票据，30 分钟过期、用一次作废 ——
        #   启动包是「下载一次、长期使用」的东西，
        #   一次性的凭证让组员第二次运行、或晚点运行就完全没法用了）。
        #   ⚠ 放在**文件**里而不是命令行：命令行参数在进程列表里是公开的。
        z.writestr("agent-ticket.txt",
                   f"{ticket}\n"
                   f"# 这是网站登录后签发的启动凭证，**绑定你这个账号**。\n"
                   f"# 30 天内有效、可以反复用 —— 这个包下载一次就行，不用反复重下。\n"
                   f"# 管理员把你的账号停用后，它就立刻失效。\n")
        # Cloudflare Access 的 service token —— agent 用它过 Access 这一关
        # （它没有浏览器、没有 Access 会话 Cookie，不带就会被挡）
        if cf_id and cf_secret:
            z.writestr("cf-access.txt",
                       f"id={cf_id}\nsecret={cf_secret}\n"
                       f"# Cloudflare Access service token（msyt-agent）\n"
                       f"# 丢了不要紧：回网页重新下载启动包即可换新的\n")
        # 版本标识：包是旧的时一眼能看出来（agent 启动也会打印自己的版本号）
        z.writestr("版本.txt",
                   f"msyt 成员端启动包\n"
                   f"打包时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"控制面  ：{server}\n"
                   f"代码版本：{_code_build()}\n"
                   f"提示    ：包是旧的 → 回网页重新下载一次即可\n"
                   f"          （助手每次启动都会自动更新代码，通常不用管）\n")

    return buf.getvalue(), "msyt-成员端启动包.zip"


def build_helper(server: str, ticket: str, helper_src: str) -> bytes:
    """单独下载助手脚本（网址动态注入到文件里）。"""
    with open(helper_src, "r", encoding="utf-8") as f:
        src = f.read()
    src = src.replace("__MSYT_SERVER__", server).replace("__MSYT_TICKET__", ticket)
    return src.encode("utf-8")
