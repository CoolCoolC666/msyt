# -*- coding: utf-8 -*-
"""
test_auth_switch.py —— 账号系统开关闸
================================================================================
用户 2026-11-13 口径：「分发版不要账号系统了，仅仅有 admin 界面就可以了，
另外账号系统作为可以开关的设施」。

所以「账号系统」从一个**硬前提**降级成**一个开关**：
  · 默认**开**（config.json 里没这一项）→ 行为与以前完全一致：登录、注册、
    改密码、成员管理、两道权限（界面层 + 接口层）
  · 关掉（config.json 里 "auth_enabled": false）→ 免登录、全部按管理员、
    不建账号、不写口令文件、界面不弹登录框

闸的判据：
  ① 默认开着（不开这个开关时，登录流程照旧）
  ② 关掉后：未带令牌也能过 require_auth / require_admin
  ③ 关掉后：ensure_admin **不建账号、不写口令文件**
  ④ 关掉后：/api/auth/config 如实报 auth_enabled=false（前端据此免登录）
  ⑤ 前端：关掉时跳过登录框、藏「改密码/退出登录」；开着时照旧
  ⑥ ★ 真反向验证：把开关逻辑改坏 → 同一批断言必须报红
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SRC = PROJ / "程序"

FAILS = []
CHECKS = [0]


def check(cond: bool, msg: str) -> None:
    CHECKS[0] += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL  " + msg)
    else:
        print("ok    " + msg)


def _fresh_webapp(cfg_json: dict):
    """载入 webapp 并把 Config 打桩成「读给定 extra、用临时库」。

    ★ 为什么要打桩而不是真写 config.json：
      Config.load() 读的是**真项目根**下的 config.json —— 真写会改到
      正在跑的那台服务器的配置（而且那儿默认没有这个文件）。
      打桩只换掉「配置从哪来」和「库写到哪」，被测逻辑一行不改。
    """
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="msyt_switch_"))
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    for m in list(sys.modules):
        if m == "msyt" or m.startswith("msyt."):
            del sys.modules[m]
    from msyt.config import Config as _RealConfig
    webapp = importlib.import_module("msyt.webapp")

    class _Cfg(_RealConfig):
        @classmethod
        def load(cls, path=None):
            c = cls()
            c.db_path = str(tmp / "data" / "msyt.db")
            c.extra = dict(cfg_json)
            return c

    webapp.Config = _Cfg
    return webapp, tmp


class _Req:
    headers: dict = {}


def test_default_on():
    """默认开着：不写 auth_enabled → 走原来的登录流程。"""
    webapp, tmp = _fresh_webapp({"admin_email": "a@example.com"})
    try:
        check(webapp._auth_enabled() is True,
              "默认：没写 auth_enabled 时账号系统是**开**的")
        check(webapp.api_auth_config()["auth_enabled"] is True,
              "默认：/api/auth/config 如实报 auth_enabled=true")
        raised = []
        try:
            webapp.require_auth(_Req())
        except Exception as e:                       # noqa: BLE001
            raised.append(e)
        check(bool(raised) and "401" in str(raised[0]),
              "默认：未登录请求被 401 挡住（登录流程没被破坏）")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_off():
    """关掉：免登录、全部按管理员、不建账号。"""
    webapp, tmp = _fresh_webapp({"admin_email": "a@example.com",
                                  "auth_enabled": False})
    try:
        check(webapp._auth_enabled() is False, "关：_auth_enabled() 返回 False")
        check(webapp.api_auth_config()["auth_enabled"] is False,
              "关：/api/auth/config 如实报 auth_enabled=false")
        m = webapp.require_auth(_Req())
        check(m.get("role") == "admin",
              "关：未带令牌也能过 require_auth，且按**管理员**")
        a = webapp.require_admin(_Req())
        check(a.get("role") == "admin", "关：require_admin 也直接放行")

        import sqlite3
        cfg = webapp._cfg()
        cfg.ensure_dirs()
        conn = webapp.get_db(cfg).conn
        conn.execute("CREATE TABLE IF NOT EXISTS members("
                     "id INTEGER PRIMARY KEY, email TEXT, pw_hash TEXT,"
                     " role TEXT, node_id TEXT, status TEXT)")
        conn.commit()
        db = sqlite3.connect(str(tmp / "data" / "msyt.db"))
        n0 = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        db.close()
        pw_file = tmp / "data" / "admin-账号.txt"
        webapp.ensure_admin(cfg)
        db = sqlite3.connect(str(tmp / "data" / "msyt.db"))
        n1 = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        db.close()
        check(n1 == n0, "关：ensure_admin 不建账号（members 行数不变 %d）" % n1)
        check(not pw_file.exists(), "关：不写 admin-账号.txt（磁盘上没有口令文件）")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_frontend():
    """前端：关掉时跳过登录框；开着时照旧弹。"""
    js = (SRC / "msyt" / "web" / "app.js").read_text(encoding="utf-8")
    check("auth_enabled === false" in js,
          "前端：读到 auth_enabled=false 时跳过登录框")
    check("state.authEnabled = false;" in js,
          "前端：记住 authEnabled 状态")
    check("if (state.me) { return; }" in js,
          "前端：免登录时不会再被弹一次登录框")
    check("state.authEnabled === false" in js,
          "前端：免登录时藏「改密码/退出登录」")


def test_reverse():
    """★ 真反向验证：改坏开关 → 断言必须报红。"""
    p = SRC / "msyt" / "webapp.py"
    src = p.read_text(encoding="utf-8")
    # ★ 替换必须保持**语法合法**，否则模块导入就炸了 —— 那样报红是「炸」，
    #   不是「断言红」，等于没验到开关逻辑。
    needle = 'get("auth_enabled", True)'
    repl = 'get("auth_enabled", False)'   # 语义反了：默认变成关
    if needle not in src:
        check(False, "反向验证：没找到开关那一行（源码变了？）")
        return
    try:
        p.write_text(src.replace(needle, repl), encoding="utf-8")
        webapp, tmp = _fresh_webapp({"admin_email": "a@example.com"})
        try:
            red = (webapp._auth_enabled() is not True)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        check(red, "反向验证：把默认值反成 False → 「默认应开着」断言报红")
    finally:
        p.write_text(src, encoding="utf-8")


if __name__ == "__main__":
    test_default_on()
    test_off()
    test_frontend()
    test_reverse()
    print("-" * 60)
    if FAILS:
        print("FAIL %d / %d" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("PASS 全部 %d 项" % CHECKS[0])
