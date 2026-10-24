# -*- coding: utf-8 -*-
"""验收：启动包里的凭证**按账号 + 可反复用**。

★ 用户 2026-10-23：「msyt-成员端启动包没有根据账号进行更新！」
  病根：包里的凭证是**一次性票据**（30 分钟过期、用一次作废），
        组员第二次运行、或晚点运行，就没凭证了。

本测试验证：
  ① 包里的凭证绑定的是**下载者本人**的账号
  ② 同一个凭证**能反复兑换**（这是核心）
  ③ 有效期是 30 天（不是 30 分钟）
  ④ 网页签发的一次性票据**仍然用一次作废**（安全闸没丢）
  ⑤ 账号被停用 → 兑换失败（安全闸没丢）
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = r"【项目根】"
DB = os.path.join(ROOT, "程序", "data", "msyt.db")
BASE = "http://127.0.0.1:8765"
AGENT_API = "https://agentapi.example.com"

MEMBER = ("test@example.com", "test-password")

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


def req(url, data=None, token=None, raw=False):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(url, data=body,
                               method="POST" if body else "GET")
    if body:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    # ★ 必须带 agent 的 UA：Cloudflare 的浏览器完整性检查会拦掉
    #   `Python-urllib` 这种默认 UA（返回 error code: 1010）。
    r.add_header("User-Agent", "msyt-agent/1.1")
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


print("=" * 70)
print("启动包凭证：按账号 + 可反复用")
print("=" * 70)

st, d = req(BASE + "/api/auth/login", {"email": MEMBER[0], "password": MEMBER[1]})
if st != 200:
    print(f"★ 登录失败：HTTP {st} {d}")
    sys.exit(1)
tok = d["token"]
print(f"已登录：{MEMBER[0]}\n")

# ---------------------------------------------------------------- ① 下载包
print("① 下载启动包")
st, zbytes = req(BASE + "/api/agent/bundle.zip", token=tok, raw=True)
check("包能下载", st == 200 and len(zbytes) > 10000,
      f"HTTP {st} {len(zbytes) if isinstance(zbytes, bytes) else 0} 字节")

zp = io.BytesIO(zbytes)
with zipfile.ZipFile(zp) as z:
    names = z.namelist()
    ticket_txt = z.read("agent-ticket.txt").decode("utf-8")
    readme = z.read("说明.txt").decode("utf-8")
ticket = ticket_txt.splitlines()[0].strip()
check("包里有 agent-ticket.txt", "agent-ticket.txt" in names)
check("包里有说明.txt", "说明.txt" in names)
check("包里有 msyt 采集器", "msyt/agent.py" in names)
check("说明里写了「30 天」", "30 天" in readme)
check("说明里写了「可以反复用」", "反复用" in readme)
check("说明里没有「用一次作废」了", "用一次作废" not in readme)

# ---------------------------------------------------------------- ② 绑定账号
print("\n② 凭证绑定的是下载者本人")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
row = con.execute("SELECT email,node_id,expires_at,used_at,repeatable FROM agent_tickets "
                  "WHERE ticket=?", (ticket,)).fetchone()
check("库里能找到这张凭证", row is not None)
if row:
    check("绑定的是下载者本人", row["email"] == MEMBER[0], f"email={row['email']}")
    check("repeatable = 1（可重复兑换）", int(row["repeatable"] or 0) == 1,
          f"repeatable={row['repeatable']}")
    ttl = int(row["expires_at"]) - int(time.time())
    check("有效期 ≈30 天（不是 30 分钟）", ttl > 25 * 86400,
          f"剩余 {ttl // 86400} 天 {ttl % 86400 // 3600} 小时")

# ---------------------------------------------------------------- ③ 反复兑换
print("\n③ ★ 同一个凭证能反复兑换（核心）")
st1, r1 = req(AGENT_API + "/api/agent/redeem", {"ticket": ticket})
check("第 1 次兑换成功", st1 == 200, f"HTTP {st1}")
check("第 1 次拿到 JWT", bool(r1.get("token")) if isinstance(r1, dict) else False)
check("第 1 次拿到的是本人身份",
      isinstance(r1, dict) and (r1.get("member") or {}).get("email") == MEMBER[0],
      str((r1.get("member") or {}).get("email")) if isinstance(r1, dict) else "")

st2, r2 = req(AGENT_API + "/api/agent/redeem", {"ticket": ticket})
check("★ 第 2 次兑换**也成功**（原来会 401「票据已用过」）", st2 == 200,
      f"HTTP {st2} {str(r2)[:120]}")
check("第 2 次拿到的还是本人", isinstance(r2, dict)
      and (r2.get("member") or {}).get("email") == MEMBER[0])

st3, r3 = req(AGENT_API + "/api/agent/redeem", {"ticket": ticket})
check("第 3 次兑换仍然成功", st3 == 200, f"HTTP {st3}")

# ---------------------------------------------------------------- ④ 一次性票据仍作废
print("\n④ 安全闸：网页签发的一次性票据仍然用一次作废")
st, tk = req(BASE + "/api/agent/ticket", {}, token=tok)
one_shot = tk.get("ticket") if isinstance(tk, dict) else ""
check("网页能签发一次性票据", bool(one_shot), str(one_shot)[:16])
os_row = con.execute("SELECT repeatable,expires_at FROM agent_tickets WHERE ticket=?",
                      (one_shot,)).fetchone()
check("一次性票据 repeatable=0", os_row and int(os_row["repeatable"] or 0) == 0)
if os_row:
    ttl1 = int(os_row["expires_at"]) - int(time.time())
    check("一次性票据仍是 30 分钟", ttl1 <= 1800 and ttl1 > 1500, f"{ttl1} 秒")
s1, _ = req(AGENT_API + "/api/agent/redeem", {"ticket": one_shot})
check("第 1 次兑换成功", s1 == 200, f"HTTP {s1}")
s2, r2b = req(AGENT_API + "/api/agent/redeem", {"ticket": one_shot})
check("第 2 次兑换被拒（一次性票据用一次作废）", s2 == 401, f"HTTP {s2}")

# ---------------------------------------------------------------- ⑤ 账号停用
print("\n⑤ 安全闸：账号被停用 → 兑换失败")
con2 = sqlite3.connect(DB)
con2.row_factory = sqlite3.Row
st, tk2 = req(BASE + "/api/agent/ticket", {}, token=tok)
t2 = tk2.get("ticket") if isinstance(tk2, dict) else ""
orig = con2.execute("SELECT status FROM members WHERE email=?", (MEMBER[0],)).fetchone()
try:
    con2.execute("UPDATE members SET status='disabled' WHERE email=?", (MEMBER[0],))
    con2.commit()
    s3, r3b = req(AGENT_API + "/api/agent/redeem", {"ticket": t2})
    check("账号停用后兑换被拒", s3 == 403, f"HTTP {s3} {str(r3b)[:80]}")
    s4, _ = req(AGENT_API + "/api/agent/redeem", {"ticket": ticket})
    check("★ 停用后连包用凭证也失效了", s4 == 403, f"HTTP {s4}")
finally:
    con2.execute("UPDATE members SET status=? WHERE email=?",
                 (orig["status"], MEMBER[0]))
    con2.commit()
    con2.close()
    # 恢复后应能再用
    s5, _ = req(AGENT_API + "/api/agent/redeem", {"ticket": ticket})
    check("恢复账号后又能兑换了", s5 == 200, f"HTTP {s5}")

con.close()

print()
print("=" * 70)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
