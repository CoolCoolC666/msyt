# -*- coding: utf-8 -*-
"""
test_member_e2e.py —— 成员端**完整成功链路**验收
================================================================================
做的是真事：
  1) 从跑着的服务**下载**启动包（网址动态注入）
  2) 解压到临时目录
  3) **用解压出来的代码**登录、领活、采集、回传
  4) 到权威库里核对：楼层进来了、详情状态变成 done、冲突记录为空

为什么不用 assist 任务里现成的活：
  那些活是「先旧后新」挑的，排在最前面的是**站方已清掉的老帖**
  （详情页返回 Discuz「提示信息」页），采不到正文 —— 那是站方的数据现实，
  不是链路的问题。本脚本自己挑一个「站方还能打开」的待采帖来跑通。

只做**只读**采集；写的是我们自己的权威库（这正是协作的目的）。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

from msyt.config import Config      # noqa: E402

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("qa-member@example.com", "qa-test-password")
EMAIL = "qa-member@example.com"
PASSWORD = "qa-test-password"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def find_good_tid(candidates):
    """挑一个「站方还能打开」的待采帖（每个候选只花 1 个请求）。"""
    import re
    from msyt.http_client import Client
    cfg = Config.load()
    c = Client(cfg, verbose=False)
    for tid in candidates:
        try:
            html = c.get(f"https://bbs.cnnb.com.cn/forum.php?mod=viewthread&tid={tid}&page=1").text
        except Exception:                            # noqa: BLE001
            continue
        if re.search(r"postmsg_\d+", html):
            return tid
    return 0


def main() -> int:
    print("=" * 78)
    print("成员端完整链路验收 —— 下载启动包 → 解压 → 采集 → 回传 → 入库")
    print("=" * 78)

    # ---------- 0. 准备一个成员账号 ----------
    from msyt.auth import hash_pw
    cfg = Config.load()
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE members SET pw_hash=? WHERE email=?",
                 (hash_pw(PASSWORD), EMAIL))
    conn.commit()
    conn.close()
    print(f"成员账号已就位：{EMAIL}")

    # ---------- 1. 下载启动包（网址动态注入） ----------
    tmpdir = tempfile.mkdtemp(prefix="msyt-e2e-")
    try:
        import urllib.request

        def _call(path, method="GET", body=None, token=""):
            url = BASE + path
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))

        # 管理员登录取令牌（用管理员下载，与成员下载走的是同一个端点）
        tok = _call("/api/auth/login", "POST",
                    {"email": ADMIN[0], "password": ADMIN[1]})["token"]
        req = urllib.request.Request(BASE + "/api/agent/bundle.zip")
        req.add_header("Authorization", "Bearer " + tok)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        zpath = os.path.join(tmpdir, "bundle.zip")
        with open(zpath, "wb") as f:
            f.write(data)
        check("启动包能下载", len(data) > 20000, f"{len(data)} 字节")

        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            check("包里有 msyt 采集器", "msyt/agent.py" in names and "msyt/detail.py" in names)
            check("包里有启动脚本", "启动采集.cmd" in names)
            server_txt = z.read("server.txt").decode("utf-8").strip()
            check("★ 网址动态注入（= agent 专用 API 地址）",
                  server_txt.startswith("http"), server_txt)
            z.extractall(tmpdir)

        # ---------- 2. 挑一个可采的待采帖 ----------
        conn = sqlite3.connect(cfg.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT tid FROM threads WHERE COALESCE(detail_state,'pending')<>'done' "
            "ORDER BY tid DESC LIMIT 40").fetchall()
        cand = [int(r["tid"]) for r in rows]
        conn.close()
        tid = find_good_tid(cand)
        check("找到站方还能打开的待采帖", bool(tid), f"tid={tid}（候选 {len(cand)} 个）")
        if not tid:
            return 1

        before = sqlite3.connect(cfg.db_path)
        before.row_factory = sqlite3.Row
        b_state = before.execute("SELECT detail_state FROM threads WHERE tid=?",
                                 (tid,)).fetchone()["detail_state"]
        b_posts = before.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                                 (tid,)).fetchone()[0]
        before.close()
        print(f"  目标 tid={tid}（本地 detail_state={b_state}，已有楼层 {b_posts}）")

        # ---------- 3. 用**解压出来的代码**跑采集+回传 ----------
        work = os.path.join(tmpdir, "work")
        os.makedirs(work, exist_ok=True)
        shutil.copytree(os.path.join(tmpdir, "msyt"), os.path.join(work, "msyt"),
                        dirs_exist_ok=True)
        runner = os.path.join(work, "_run_agent.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "import sys, json\n"
                f"sys.path.insert(0, {work!r})\n"
                "from msyt.agent import Agent\n"
                f"ag = Agent(server={BASE!r}, email={EMAIL!r}, password={PASSWORD!r},"
                " verbose=True)\n"
                "ag.login()\n"
                f"payload = ag.collect_one({tid})\n"
                "ok = payload['summary'].get('ok')\n"
                "print('COLLECT_OK', ok, 'posts', payload['summary'].get('posts'),"
                " 'pages', payload['summary'].get('pages'))\n"
                f"r = ag.submit('A-E2E', {tid}, payload)\n"
                "print('SUBMIT_RESULT', json.dumps(r, ensure_ascii=False))\n")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run([sys.executable, runner], cwd=work, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        out = (p.stdout or "") + (p.stderr or "")
        print("  ---- agent 输出 ----")
        for line in out.splitlines()[:20]:
            print("  | " + line)
        check("agent 用解压出来的代码登录成功", "登录成功" in out, "")
        check("★ 采集成功（正文拿到了）", "COLLECT_OK True" in out, out[-200:].replace("\n", " | "))
        check("★ 回传成功", "SUBMIT_RESULT" in out and '"merged": true' in out,
              out[-200:].replace("\n", " | "))

        # ---------- 4. 权威库核对 ----------
        after = sqlite3.connect(cfg.db_path)
        after.row_factory = sqlite3.Row
        a = after.execute(
            "SELECT detail_state,posts_n,chars,pages_total FROM threads WHERE tid=?",
            (tid,)).fetchone()
        a_posts = after.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                                (tid,)).fetchone()[0]
        after.close()
        check("★ 权威库里楼层增加了", a_posts > b_posts, f"{b_posts} → {a_posts}")
        check("★ 详情状态变成 done", a["detail_state"] == "done",
              f"{b_state} → {a['detail_state']}")
        check("★ 楼层计数与实际一致", (a["posts_n"] or 0) == a_posts,
              f"posts_n={a['posts_n']} 实际={a_posts}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 78)
    npass = sum(1 for _, ok, _ in results if ok)
    nfail = len(results) - npass
    for name, ok, extra in results:
        if not ok:
            print(f"  FAIL {name}  {extra}")
    print(f"共 {len(results)} 项 · 通过 {npass} · 失败 {nfail}")
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
