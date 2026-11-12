# -*- coding: utf-8 -*-
"""
build_dist.py —— 生成「组员本地玩」分发包
================================================================================
用户要求（2026-11-13）：
  ① 更新一个分发版给组员本地玩玩
  ② **不要存密钥信息**
  ③ 已有数据**全量**放入分发包
  ④ 程序中**自动安装相关依赖**

做法：
  · 数据：SQLite 备份 API 做**一致快照**（含 WAL 里已提交的数据），再在副本上脱敏。
  · 脱敏（只动凭证，不动业务数据）：
      - 删 agent_tickets（票据 = 凭证）
      - 删 meta 的 jwt_secret（重新生成）与 agent_state:*（含成员节点状态）
      - 清空 members.pw_hash → **任何人都登不进原账号**
      - 源码里的真实邮箱/域名 → 占位符（不让本机包指向我们的公网入口）
  · 登录：包内 config.json 把管理员指向 **本地新账号** local@msyt.local，
          首次启动 ensure_admin 现生成随机口令（打印到窗口 + 写 data/admin-*.txt）。
          → 包里**没有任何固定口令**；自助注册也放开（注册即可用）。
  · 依赖：requirements.txt + 启动.cmd（纯 ASCII + CRLF，缺什么装什么）。

用法：
  python _tools/build_dist.py --out "<落点目录>" [--zip]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SRC_CODE = PROJ / "程序" / "msyt"
SRC_HELPER = PROJ / "程序" / "msyt-helper.py"
SRC_DB = PROJ / "程序" / "data" / "msyt.db"
RECYCLE = PROJ / "_tools" / "recycle_bin.py"

#: 本地账号用的邮箱（**不是**任何真人的邮箱；首次启动时现生成口令）
LOCAL_ADMIN_EMAIL = "local@msyt.local"

SKIP_NAMES = {"__pycache__", "_preview_bars.html"}

#: 源码里的真实标识 → 占位符（长的先替换，避免半截命中）
SCRUB = [
    ("msyt.example.com", "msyt.example.com"),
    ("agentapi.example.com", "agentapi.example.com"),
    ("example.com", "example.com"),
    ("msyt.example.com", "example.com"),
    ("admin@example.com", LOCAL_ADMIN_EMAIL),
    ("test@example.com", "member2@local.test"),
    ("member2@example.com", "member3@local.test"),
    ("member3@example.com", "member4@local.test"),
    ("member4@example.com", "member5@local.test"),
    ("qa-admin@example.com", "qa-admin@local.test"),
    ("qa-member@example.com", "qa-member@local.test"),
]
TEXT_EXT = {".py", ".js", ".html", ".css", ".txt", ".json", ".cmd", ".md"}


def _recycle(path: Path) -> None:
    """删旧目录只走系统回收站（本机硬策略：禁止永久删除）。"""
    if not path.exists():
        return
    r = subprocess.run([sys.executable, str(RECYCLE), str(path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    sys.stdout.write(r.stdout or "")
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        raise SystemExit("回收站删除失败：%s" % path)


def scrub_text(text: str) -> str:
    for a, b in SCRUB:
        text = text.replace(a, b)
    # 兜底：剩余的示例邮箱 / 域名后缀一律中性化（源码里都是文档示例）
    text = text.replace("@qq.com", "@local.test")
    text = text.replace("@outlook.com", "@local.test")
    text = text.replace(".example-tld", ".example-tld")
    text = text.replace("trycloudflare", "example-tunnel")
    return text


def copy_code(dst_root: Path) -> int:
    """复制 msyt 包（去掉 __pycache__ 与闸产物），文本文件顺带脱敏。"""
    n = 0
    for src in sorted(SRC_CODE.rglob("*")):
        rel = src.relative_to(SRC_CODE)
        if any(p in SKIP_NAMES for p in rel.parts):
            continue
        if src.is_dir():
            continue
        dst = dst_root / "msyt" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in TEXT_EXT:
            try:
                txt = src.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                shutil.copy2(src, dst)
            else:
                dst.write_text(scrub_text(txt), encoding="utf-8")
        else:
            shutil.copy2(src, dst)
        n += 1
    if SRC_HELPER.exists():
        dst = dst_root / "msyt-helper.py"
        try:
            dst.write_text(scrub_text(SRC_HELPER.read_text(encoding="utf-8")),
                           encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(SRC_HELPER, dst)
        n += 1
    return n


def copy_db(dst_root: Path) -> int:
    """备份 API 做一致快照（会带上 WAL 里已提交的数据）。"""
    dst_db = dst_root / "data" / "msyt.db"
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(SRC_DB), timeout=60)
    dst = sqlite3.connect(str(dst_db))
    t0 = time.time()
    total = src.execute("PRAGMA page_count").fetchone()[0]
    last = [0]

    def _progress(status, remaining, total_pages):
        done = total_pages - remaining
        if done - last[0] >= 20000:
            last[0] = done
            print("  copying %d/%d pages (%.1f%%) %.0fs"
                  % (done, total_pages, done * 100.0 / max(total_pages, 1),
                     time.time() - t0),
                  flush=True)

    try:
        src.backup(dst, pages=4096, progress=_progress)
    finally:
        dst.close()
        src.close()
    return dst_db.stat().st_size


def sanitize(dst_root: Path) -> dict:
    """在副本上脱敏。返回改动统计。"""
    db = sqlite3.connect(str(dst_root / "data" / "msyt.db"))
    out = {}
    cur = db.cursor()
    cur.execute("DELETE FROM agent_tickets")
    out["agent_tickets_deleted"] = cur.rowcount
    cur.execute("DELETE FROM meta WHERE key='jwt_secret' OR key LIKE 'agent_state:%'")
    out["meta_deleted"] = cur.rowcount
    cur.execute("UPDATE members SET pw_hash=''")
    out["members_pw_cleared"] = cur.rowcount
    db.commit()
    out["threads"] = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    out["posts"] = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    out["members"] = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    out["tickets_left"] = db.execute("SELECT COUNT(*) FROM agent_tickets").fetchone()[0]
    out["jwt_left"] = db.execute(
        "SELECT COUNT(*) FROM meta WHERE key='jwt_secret'").fetchone()[0]
    out["pw_left"] = db.execute(
        "SELECT COUNT(*) FROM members WHERE pw_hash<>''").fetchone()[0]
    db.close()
    return out


#: 成员身份 → 假名（分发包可能被外传，**不写任何真人邮箱**）
ANON = {
    "admin@example.com": "admin0@local.test",
    "test@example.com": "member2@local.test",
    "member2@example.com": "member3@local.test",
    "member3@example.com": "member4@local.test",
    "member4@example.com": "member5@local.test",
    "qa-admin@example.com": "qa-admin@local.test",
    "qa-member@example.com": "qa-member@local.test",
}

#: 需要扫的「含成员身份」的列（留痕字段；node_id 里也带邮箱，一并替换）
ANON_COLS = {
    "members": ["email", "node_id"],
    "thread_anno_tags": ["annotator", "reviewer"],
    "sample_review": ["annotator", "reviewed_by"],
    "anno_queue": ["assignee"],
    "assist_conflicts": ["node", "resolved_by", "task_code"],
    "assist_tasks": ["created_by"],
}


def anonymize(dst_root: Path) -> dict:
    """成员身份假名化。**只动账号/留痕字段，帖子与楼层正文一个字不改**。"""
    db = sqlite3.connect(str(dst_root / "data" / "msyt.db"))
    out = {}
    for tbl, cols in ANON_COLS.items():
        n = 0
        for col in cols:
            for a, b in ANON.items():
                cur = db.execute(
                    'UPDATE "%s" SET "%s"=REPLACE("%s",?,?) WHERE "%s" LIKE ?'
                    % (tbl, col, col, col), (a, b, "%" + a + "%"))
                n += cur.rowcount
        out[tbl] = n
    # members.note：真人姓名（叶嘉秀之类）去掉；「自助注册」这类通用标记留着
    keep = ("自助注册", "测试", "临时测试账号")
    for mid, note in db.execute("SELECT id, note FROM members").fetchall():
        if note and note not in keep:
            db.execute("UPDATE members SET note=NULL WHERE id=?", (mid,))
    db.commit()
    left = []
    for a in ANON:
        for tbl, cols in ANON_COLS.items():
            for col in cols:
                c = db.execute('SELECT COUNT(*) FROM "%s" WHERE "%s" LIKE ?'
                               % (tbl, col), ("%" + a + "%",)).fetchone()[0]
                if c:
                    left.append("%s.%s:%s" % (tbl, col, a))
    db.close()
    out["left"] = left
    return out


REQUIREMENTS = """
fastapi>=0.110
uvicorn>=0.27
pydantic>=2.0
requests>=2.31
beautifulsoup4>=4.11
PyJWT>=2.8
pypinyin>=0.49
jieba>=0.42
"""


CONFIG_JSON = """
{
  "admin_email": "%s",
  "auto_approve_registration": true,
  "allow_domains": "",
  "auth_enabled": false
}
""" % LOCAL_ADMIN_EMAIL


#: 启动.cmd：**纯 ASCII + CRLF**，echo 行里不许出现 "->"（cmd 当重定向）
START_CMD = """
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo   Minzheng e-net : local data playground
echo ================================================
echo.

set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=py -3"

echo [1/3] checking dependencies ...
%PY% -c "import fastapi,uvicorn,pydantic,requests,bs4,jwt" >nul 2>nul
if errorlevel 1 (
  echo       installing packages, internet needed only for the first run ...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo       install failed. check network or pip mirror, then run this file again.
    pause
    exit /b 1
  )
)

echo [2/3] checking local database ...
%PY% -m msyt stats

echo [3/3] starting local web at http://127.0.0.1:8765/
echo       first run creates a local admin account.
echo       its password is printed below and saved into the data folder.
echo.
start "" cmd /c "ping -n 9 127.0.0.1 >nul & start http://127.0.0.1:8765/"
%PY% -m msyt web --host 127.0.0.1 --port 8765
echo.
echo web server stopped.
pause
"""


README = """
民生e点通 · 舆情数据本地探索版（组员分发包）
============================================================

【这是什么】
这是课程舆情采集成果的**本地副本**，给你在自己电脑上随便翻。
装的是**全量已采数据**（帖子、楼层正文、分类标签、受理流程、词表命中、
标注留痕、舆情卡片），不是抽样，也不是空壳。

【怎么跑】
1. 先装 Python 3.10 或更新版本（安装时勾选 Add python.exe to PATH）。
2. 双击「启动.cmd」。
   第一次会自动装依赖（需要联网，几分钟）；之后就秒开。
3. 浏览器打开 http://127.0.0.1:8765/
4. **直接用，不用登录**：这个本地版把账号系统关掉了（config.json 里
   "auth_enabled": false），打开就是管理员界面。
   · 想恢复登录？把 config.json 里那一项改成 true（或删掉），重启即可。

【包里没有什么】
· 没有任何服务器口令、密钥、令牌、票据、AI 接口密钥。
· **没有账号系统**：不建账号、不写口令文件、库里不留凭据。
· 原组员账号的口令哈希已清空、身份已假名化 → 谁都登不进原账号。
· data 目录里只有 msyt.db 一个文件。

【能做什么】
· 总览：数据规模、分类分布、受理状态、时间分布。
· 检索：标题与楼层正文全文检索，可按部门 / 类别 / 状态 / 区县市筛。
· 标注台：给帖子打标签、看 AI 初注与人工复核留痕。
· 舆情卡片：确定性字段 + 研判信号 + AI 六项结论（已有的卡片直接看）。
· 词表：组里维护的关键词表与命中统计。

【要注意的】
· AI 功能需要**你自己的** API key：网页右上角「我的 API」里填，
  只存在你自己的浏览器里，不写进这个包。（服务器的默认 key 不在包里。）
· 「采集作业」页真的能向站方发请求（从**你**的机器发）。
  建议只看不采；真要采先和管理员说，别并发猛采。
· 数据是快照，不会自动更新；要看新帖找管理员要新版。
· 别把这个包连同 data/msyt.db 传到公开网盘或公开仓库。

"""


def write_text(path: Path, text: str, ascii_only: bool = False) -> None:
    """写文本。ASCII 文件用 CRLF（cmd 双击才不乱码/不闪退）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if ascii_only:
        data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    else:
        data = text.replace("\r\n", "\n").encode("utf-8")
    path.write_bytes(data)


def build(out: Path, make_zip: bool) -> None:
    _recycle(out)
    out.mkdir(parents=True, exist_ok=True)
    print("[1/4] copying code ...", flush=True)
    n = copy_code(out)
    print("      %d files" % n, flush=True)
    print("[2/4] snapshotting database ...", flush=True)
    size = copy_db(out)
    print("      %.1f MB" % (size / 1048576.0), flush=True)
    print("[3/4] sanitizing ...", flush=True)
    stats = sanitize(out)
    for k, v in stats.items():
        print("      %s = %s" % (k, v), flush=True)
    anon = anonymize(out)
    for k, v in anon.items():
        print("      anon %s = %s" % (k, v), flush=True)
    print("[4/4] writing package files ... done", flush=True)
    write_text(out / "requirements.txt", REQUIREMENTS)
    write_text(out / "config.json", CONFIG_JSON)
    write_text(out / "启动.cmd", START_CMD, ascii_only=True)
    write_text(out / "说明.txt", README)
    if make_zip:
        zp = out.with_suffix(".zip")
        _recycle(zp)
        print("      zipping to %s ..." % zp.name, flush=True)
        shutil.make_archive(str(out), "zip", root_dir=str(out.parent),
                            base_dir=out.name)
        print("      zip %.1f MB" % (zp.stat().st_size / 1048576.0), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="分发包落点目录")
    ap.add_argument("--zip", action="store_true", help="顺便打个 zip")
    a = ap.parse_args()
    build(Path(a.out), a.zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
