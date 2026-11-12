# -*- coding: utf-8 -*-
"""
test_dist_package.py —— 分发包验收闸
================================================================================
检查分发包：
  ① 结构齐全（msyt/ + data/msyt.db + 启动.cmd + requirements.txt + 说明.txt）
  ② **没有任何密钥信息**（口令哈希 / jwt_secret / 票据 / AI key / 隧道令牌 /
     真实成员邮箱 / 本机路径 / 我们的域名）
  ③ 依赖清单齐全（包内 import 的第三方模块都得在 requirements.txt 里）
  ④ 启动.cmd 是纯 ASCII + CRLF（双击不闪退）
  ⑤ 数据是全量（各业务表行数与**包内基线** manifest.json 一致；
     不跟活着的真库对账 —— 真库会随组员回传增长，快照追不上）
  ⑥ 数据库能开、查询正常、FTS 索引在

★ 反向验证：把包里的东西改坏（塞一条真邮箱 / 改 CRLF / 删依赖），
  同一批断言必须报红。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
PKG = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    PROJ.parent / "msyt-本地分发版")
REAL_DB = PROJ / "程序" / "data" / "msyt.db"

FAILS = []
CHECKS = [0]


def check(cond: bool, msg: str) -> None:
    CHECKS[0] += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL  " + msg)
    else:
        print("ok    " + msg)


#: **我们自己的**标识 → 包里绝对不许出现（密钥 / 成员身份 / 本机路径）
#: ★ 注意：站方公开数据里网友自己写的邮箱（如昵称就是 QQ 邮箱）**要留着**
#:   —— 那是研究数据本体，删了就违背「已有数据全量放入分发包」。
OURS = [
    ("admin@example.com", "管理员邮箱"),
    ("test@example.com", "成员邮箱"),
    ("member2@example.com", "成员邮箱"),
    ("member3@example.com", "成员邮箱"),
    ("member4@example.com", "成员邮箱"),
    ("qa-admin@example.com", "成员邮箱"),
    ("qa-member@example.com", "成员邮箱"),
    ("example-tld", "我们的公网域名后缀"),
    ("trycloudflare", "隧道地址"),
    ("sample-temp-password", "真实临时口令"),
    ("sample-temp-password", "真实临时口令"),
    ("sample-temp-password", "真实口令"),
    ("qa-test-password", "真实口令"),
    ("msyt@change-me", "真实管理员口令"),
    ("test-password", "真实口令"),
    ("Deskbox", "本机路径"),
    ("28.新学期-启动", "课程目录结构"),
    ("02 民生e网通", "课程目录结构"),
]


TEXT_EXT = {".py", ".js", ".html", ".css", ".txt", ".json", ".cmd", ".md", ".toml"}


def scan_text_files() -> None:
    """全包文本扫禁用词。"""
    hits = []
    for p in PKG.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat, why in OURS:
            if pat in txt:
                hits.append("%s 含 %s（%s）" % (p.relative_to(PKG), pat, why))
    check(not hits, "文本文件不含密钥/真实身份/本机路径" +
          ("" if not hits else "：" + "；".join(hits[:8])))


def scan_db() -> None:
    """数据库里不许有凭证。"""
    db = PKG / "data" / "msyt.db"
    check(db.exists(), "data/msyt.db 存在")
    if not db.exists():
        return
    c = sqlite3.connect("file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
    try:
        n_ticket = c.execute("SELECT COUNT(*) FROM agent_tickets").fetchone()[0]
        check(n_ticket == 0, "agent_tickets 已清空（票据=凭证）")
        n_jwt = c.execute(
            "SELECT COUNT(*) FROM meta WHERE key='jwt_secret'").fetchone()[0]
        check(n_jwt == 0, "meta 里没有 jwt_secret")
        n_state = c.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'agent_state:%'").fetchone()[0]
        check(n_state == 0, "meta 里没有 agent_state:*（含成员节点状态）")
        n_pw = c.execute(
            "SELECT COUNT(*) FROM members WHERE pw_hash<>''").fetchone()[0]
        check(n_pw == 0, "members 的口令哈希全部清空（谁都登不进原账号）")
        # 全库文本扫真实邮箱
        leak = []
        for t in [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]:
            cols = [r[1] for r in c.execute('PRAGMA table_info("%s")' % t)
                    if (r[2] or "").upper() == "TEXT"]
            for col in cols:
                try:
                    rows = c.execute('SELECT "%s" FROM "%s"' % (col, t)).fetchall()
                except sqlite3.Error:
                    continue
                for (v,) in rows:
                    if not isinstance(v, str):
                        continue
                    for pat, why in OURS:
                        if pat in v:
                            leak.append("%s.%s 含 %s（%s）" % (t, col, pat, why))
                            break
        check(not leak, "库内文本列没有真实邮箱/域名" +
              ("" if not leak else "：" + "、".join(sorted(set(leak))[:8])))
        # 数据是全量 —— 跟**包内基线**对账，不跟活着的真库对账
        # （真库会随组员回传/采集不断增长 → 快照永远追不上 → 闸随机变红）
        man = json.loads((PKG / "manifest.json").read_text(encoding="utf-8"))
        base = man["tables"]
        src = man.get("source_tables", {})
        for tbl, want in base.items():
            b = c.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()[0]
            check(b == want, "%s 行数与包内基线一致（%d == %d）" % (tbl, b, want))
            if tbl in src:
                check(src[tbl] >= want,
                      "%s 快照未超前于真库（基线 %d <= 真库 %d）" % (tbl, want, src[tbl]))
        # FTS 还在（检索功能依赖）
        try:
            n = c.execute("SELECT COUNT(*) FROM posts_fts").fetchone()[0]
            check(n > 0, "posts_fts 索引在（%d 行）" % n)
        except sqlite3.Error as e:
            check(False, "posts_fts 不可用：%s" % e)
    finally:
        c.close()


def check_layout() -> None:
    for rel in ("msyt/__init__.py", "msyt/cli.py", "msyt/webapp.py",
                "msyt/web/index.html", "msyt/web/app.js", "msyt/web/card.js",
                "msyt/web/style.css", "data/msyt.db", "启动.cmd",
                "requirements.txt", "说明.txt", "config.json",
                "manifest.json"):
        check((PKG / rel).exists(), "结构齐全：%s" % rel)
    check(not (PKG / "msyt" / "__pycache__").exists(), "没有 __pycache__")
    check(not (PKG / "msyt" / "web" / "_preview_bars.html").exists(),
          "没有闸产物 _preview_bars.html")


def check_cmd() -> None:
    p = PKG / "启动.cmd"
    if not p.exists():
        return
    raw = p.read_bytes()
    try:
        raw.decode("ascii")
        ok_ascii = True
    except UnicodeDecodeError:
        ok_ascii = False
    check(ok_ascii, "启动.cmd 是纯 ASCII（中文会乱码/闪退）")
    check(b"\r\n" in raw, "启动.cmd 用 CRLF 换行")
    check(raw.replace(b"\r\n", b"") .count(b"\n") == 0,
          "启动.cmd 没有裸 LF")
    txt = raw.decode("ascii", "replace")
    check("->" not in txt, "启动.cmd 里没有 ->（cmd 会当重定向）")
    check("pip install" in txt, "启动.cmd 会自动装依赖")
    check("-m msyt web" in txt, "启动.cmd 会启动本地 web")


def check_deps() -> None:
    """包内 import 的第三方模块，必须都在 requirements.txt 里。"""
    req = (PKG / "requirements.txt").read_text(encoding="utf-8").lower()
    std = set(sys.stdlib_module_names)
    local = {"msyt", "webapp", "card", "anno", "ai", "db", "detail", "listpage",
             "agent", "assist", "auth", "bundle", "cli", "config", "doctor",
             "hub", "http_client", "netdiag", "parsers", "parallel", "pinyin",
             "selfcheck", "sources", "summarize", "tags", "taxonomy", "textutil",
             "topic"}
    need = set()
    for p in (PKG / "msyt").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)",
                             src, re.M):
            name = m.group(1)
            if name in std or name in local or name == "__future__":
                continue
            need.add(name)
    alias = {"bs4": "beautifulsoup4", "jwt": "pyjwt"}
    for name in sorted(need):
        pkgname = alias.get(name, name)
        check(pkgname.lower() in req,
              "依赖清单含 %s（包内 import %s）" % (pkgname, name))


def check_config() -> None:
    p = PKG / "config.json"
    if not p.exists():
        return
    import json
    d = json.loads(p.read_text(encoding="utf-8"))
    check(d.get("admin_email") == "local@msyt.local",
          "config.json 的管理员是本地新账号 local@msyt.local")
    check(d.get("auto_approve_registration") is True,
          "config.json 放开自助注册（组员自己注册就能用）")


def main() -> int:
    print("分发包闸：%s" % PKG)
    check_layout()
    scan_text_files()
    scan_db()
    check_cmd()
    check_deps()
    check_config()
    print("-" * 60)
    # ★ 汇总行格式必须跟 run_all_tests.py 认的一致
    #   （`共 N 项 · 通过 N · 失败 N`）—— 否则回归器认不出 → 误判成 FAIL。
    print("共 %d 项 · 通过 %d · 失败 %d"
          % (CHECKS[0], CHECKS[0] - len(FAILS), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
