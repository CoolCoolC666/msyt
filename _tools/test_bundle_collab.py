# -*- coding: utf-8 -*-
"""验收：启动包由「**本地浏览器 + 我们的服务器**」协同生成。

★ 用户 2026-10-23：「msyt-成员端启动包需要根据登录的本地浏览器与我们的服务器协同生成」

要验证的：
  ① 浏览器告诉服务器「我是从哪儿访问的」（Host 头）
  ② 服务器告诉包「我部署在哪儿」（agent-api-base.txt）
  ③ 两边合成 → 包里带**主用 + 备用**两个地址
  ④ 规则：
       本地访问 → 主用=本地、备用=agentapi
       公网访问 → 主用=agentapi、备用=访问地址
  ⑤ agent 启动时**自己探**，主地址不通就换备用
  ⑥ 包里的凭证仍然绑定当前账号
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
sys.path.insert(0, os.path.join(ROOT, "程序"))
BASE = "http://127.0.0.1:8765"
EMAIL, PW = "test@example.com", "test-password"

# ★ 地址**探测**而不是写死（2026-10-26）：公网入口是隧道地址，会变
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net_probe import current_agent_api, current_public, current_endpoints  # noqa: E402
API_BASE = current_agent_api()       # 当前部署的公网入口
PUBLIC_HOST = current_public().split("//", 1)[-1].split("/", 1)[0]

OK = 0
FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  —— {extra}" if extra else ""))


def req(path, data=None, token=None, host=None, raw=False, method=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(BASE + path, data=body,
                               method=method or ("POST" if body else "GET"))
    if body:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    r.add_header("User-Agent", "msyt-agent/1.1")
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            b = resp.read()
            return resp.status, (b if raw else json.loads(b.decode("utf-8")))
    except urllib.error.HTTPError as e:
        try:
            b = e.read()
            return e.code, (b if raw else json.loads(b.decode("utf-8")))
        except Exception:
            return e.code, {}


def _hp(u):
    """地址的 host+port（**忽略协议**）—— 比较地址是否同一个时用。

    ★ 2026-10-26：浏览器走 https → origin 是 https://x，
      而地址池里记的可能是 http://x（回源/反代口径不同），
      按整串比较会把同一个地址当成两个。
    """
    s = (u or "").strip().rstrip("/").split("://", 1)[-1]
    return s.lower()


def read_pkg(data):
    """从 zip 字节里读出关键信息。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        out = {
            "names": names,
            "server": z.read("server.txt").decode("utf-8").strip(),
            "readme": z.read("说明.txt").decode("utf-8"),
            "ticket": z.read("agent-ticket.txt").decode("utf-8").splitlines()[0].strip(),
            "cmd": z.read("启动采集.cmd").decode("utf-8", "replace"),
            "helper_cmd": z.read("启动助手.cmd").decode("utf-8", "replace"),
        }
        out["has_alt_file"] = "server-alt.txt" in names
        out["alt"] = (z.read("server-alt.txt").decode("utf-8").strip()
                      if out["has_alt_file"] else "")
    return out


print("=" * 72)
print("启动包「本地浏览器 + 服务器」协同生成")
print("=" * 72)

st, d = req("/api/auth/login", {"email": EMAIL, "password": PW})
if st != 200:
    print(f"★ 登录失败 HTTP {st}")
    sys.exit(1)
tok = d["token"]
print(f"已登录：{EMAIL}\n")

# ---------------------------------------------------------------- ① 本地访问
print("① 从**本地浏览器**下载（Host: 127.0.0.1:8765）")
st, z1 = req("/api/agent/bundle.zip", token=tok, raw=True, host="127.0.0.1:8765")
check("包能下载", st == 200 and len(z1) > 10000, f"HTTP {st}")
p1 = read_pkg(z1)
print(f"   主用 = {p1['server']}")
print(f"   备用 = {p1['alt'] or '(无)'}")
check("★ 本地下载 → 主用是**本地地址**",
      p1["server"] == "http://127.0.0.1:8765", p1["server"])
check("★ 本地下载 → 备用是当前部署的公网入口",
      (p1["alt"] or "").strip() == API_BASE
      or API_BASE in (p1["alt"] or ""), p1["alt"])
check("包里有 server-alt.txt", p1["has_alt_file"])
check("启动采集.cmd 传了 --server-alt", "--server-alt" in p1["cmd"])
check("启动助手.cmd 传了 --server-alt", "--server-alt" in p1["helper_cmd"])
check("说明里写了主用/备用", "主用" in p1["readme"] and "备用" in p1["readme"])

# ---------------------------------------------------------------- ② 公网访问
print(f"\n② 从**公网浏览器**下载（Host: {PUBLIC_HOST}）")
st, z2 = req("/api/agent/bundle.zip", token=tok, raw=True,
             host=PUBLIC_HOST)
check("包能下载", st == 200, f"HTTP {st}")
p2 = read_pkg(z2)
print(f"   主用 = {p2['server']}")
print(f"   备用 = {p2['alt'] or '(无)'}")
check("★ 公网下载 → 主用是**下载者此刻访问的地址**（2026-10-26 订正）",
      PUBLIC_HOST in (p2["server"] or ""), p2["server"])
# ★ 原断言是「主用 = 当前部署的公网入口（agentapi 子域名）」，
#   理由是「Access 对它 bypass，agent 唯一能直连的」。
#   现已订正：那套 Access 早就不用了（改成隧道 + 站点访问口令门），
#   且 `.example-tld` 后缀境内按 SNI 封锁 —— 当主地址等于「每次先探一个死地址」。
#   现规则：主用 = 下载者此刻访问的地址；备用 = 地址池里活着的其它入口。
# ★ server-alt.txt 是**多行**的（一行一个地址）→ 按行判。
_alt_lines = [x.strip() for x in (p2["alt"] or "").splitlines() if x.strip()]
check("★ 公网下载 → 备用非空且**不含主地址自己**",
      bool(_alt_lines)
      and all(_hp(x) != _hp(p2["server"] or "") for x in _alt_lines),
      p2["alt"])
check("★ 公网下载 → 备用是当前部署的**其它**公网入口",
      bool(current_endpoints())
      and all(any(_hp(x) == _hp(e) for e in current_endpoints())
              for x in _alt_lines),
      f"alt={_alt_lines} / eps={current_endpoints()}")

# ---------------------------------------------------------------- ③ 差异
print("\n③ 两种来源生成的包**确实不同**")
check("本地包的 server.txt ≠ 公网包",
      p1["server"] != p2["server"], f"{p1['server']} vs {p2['server']}")
check("两种来源都带两个候选地址",
      bool(p1["alt"]) and bool(p2["alt"]))

# ---------------------------------------------------------------- ④ 凭证仍绑账号
print("\n④ 凭证仍绑定当前账号")
import sqlite3
DB = os.path.join(ROOT, "程序", "data", "msyt.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
for tag, tk in (("本地包", p1["ticket"]), ("公网包", p2["ticket"])):
    row = con.execute("SELECT email,repeatable FROM agent_tickets WHERE ticket=?",
                      (tk,)).fetchone()
    check(f"{tag}的凭证绑定本人", row and row["email"] == EMAIL,
          row["email"] if row else "查不到")
    check(f"{tag}的凭证可重复兑换", row and int(row["repeatable"] or 0) == 1)
con.close()

# ---------------------------------------------------------------- ⑤ agent 探测
print("\n⑤ agent 启动时自己探地址")
from msyt.agent import Agent                       # noqa: E402

# 5a 主地址通 → 用主地址
ag = Agent(server="http://127.0.0.1:8765", server_alt="http://127.0.0.1:59999",
           verbose=True)
picked = ag.pick_server()
check("主地址通 → 就用主地址", picked == "http://127.0.0.1:8765", picked)

# 5b 主地址不通 → 换备用
ag2 = Agent(server="http://127.0.0.1:59999", server_alt="http://127.0.0.1:8765",
            verbose=True)
picked2 = ag2.pick_server()
check("★ 主地址不通 → 自动换备用", picked2 == "http://127.0.0.1:8765", picked2)

# 5c 只有一个地址 → 不探
ag3 = Agent(server="http://127.0.0.1:8765", verbose=True)
check("只有一个地址时不做探测", ag3.pick_server() == "http://127.0.0.1:8765")

# 5d 选址不影响失联计数
ag4 = Agent(server="http://127.0.0.1:59999", server_alt="http://127.0.0.1:8765")
ag4.pick_server()
check("★ 选址阶段的失败**不计入失联计数**", ag4._fail_streak == 0,
      f"fail_streak={ag4._fail_streak}")

# ---------------------------------------------------------------- ⑥ CLI 参数
print("\n⑥ CLI / helper 支持新参数")
cli = io.open(os.path.join(ROOT, "程序", "msyt", "cli.py"), encoding="utf-8").read()
check("cli.py 有 --server-alt", '"--server-alt"' in cli)
helper = io.open(os.path.join(ROOT, "程序", "msyt-helper.py"), encoding="utf-8").read()
check("helper 有 --server-alt", '"--server-alt"' in helper)
check("helper 会把 --server-alt 传给 agent",
      '"--server-alt", STATE["server_alt"]' in helper)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
