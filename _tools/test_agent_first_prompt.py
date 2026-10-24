# -*- coding: utf-8 -*-
"""验收：组员进「采集与协作 → 采集作业」时，是否**先看到「起 Agent」的提醒**。

（用户 2026-10-23：「组员网站运行这个记得请组员先起Agent」）

分别用两种角色登录并检查：
  成员 → 必须看到「先起 Agent」引导卡 + 本机 agent 面板在**最前面**
  管理员 → 不该看到引导卡（他在服务器上采，不需要这层提醒）
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765/"
MEMBER = ("test@example.com", "test-password")
ADMIN = ("qa-admin@example.com", "qa-test-password")

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


def run_as(p, who, email, pw, expect_guide):
    print(f"\n{'=' * 66}\n{who}（{email}）\n{'=' * 66}")
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1400, "height": 1100})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="load")
    pg.wait_for_timeout(1200)

    # 登录
    pg.fill("#lg-email", email)
    pg.fill("#lg-pw", pw)
    pg.click("#lg-btn")
    pg.wait_for_timeout(2500)

    # 隐藏登录框 = 登录成功
    login_hidden = pg.evaluate(
        "() => { const e=document.querySelector('#login');"
        " return e ? e.classList.contains('hidden') : null; }")
    check("登录成功", login_hidden is True, f"login hidden={login_hidden}")

    # 进「采集与协作」
    pg.click("#tabs button[data-tab='collect']")
    pg.wait_for_timeout(1200)

    # 引导卡可见性
    guide = pg.evaluate(
        "() => { const e=document.querySelector('#agent-first-card');"
        " if(!e) return null;"
        " const s=getComputedStyle(e);"
        " return {display:s.display, visible: s.display!=='none' && e.offsetHeight>0,"
        " text:(e.innerText||'').slice(0,80)}; }")
    check("引导卡在 DOM 里", guide is not None)
    if expect_guide:
        check("★ 成员**能看到**「先起 Agent」引导卡",
              guide and guide["visible"], str(guide))
    else:
        check("管理员**看不到**引导卡（他在服务器上采）",
              guide and not guide["visible"], str(guide))

    # 本机 agent 面板是否在「采集作业」子页签的最前面
    order = pg.evaluate(
        "() => { const sub=document.querySelector('#sub-jobs');"
        " if(!sub) return null;"
        " return Array.from(sub.children).map(c=>c.id||c.className); }")
    check("「采集作业」子页签有内容", order is not None, str(order)[:120])
    if order:
        # agent-first 应该在 toolkit-card 之前
        try:
            ig = order.index("agent-first-card")
            it = order.index("toolkit-card")
            check("引导卡在 agent 面板**之前**", ig < it, f"agent-first@{ig} toolkit@{it}")
        except ValueError:
            check("引导卡在 agent 面板**之前**", False, f"order={order}")

    # 本机 agent 状态徽章有内容
    pg.wait_for_timeout(2500)
    st = pg.evaluate(
        "() => { const e=document.querySelector('#agent-first-state');"
        " return e ? (e.innerText||'').trim() : null; }")
    check("状态徽章有内容", bool(st), str(st))

    # 「开始采集」按钮存在
    has_btn = pg.evaluate("() => !!document.querySelector('#btn-list')")
    check("有「开始采集」按钮", has_btn)

    if errs:
        check("无页面异常", False, str(errs[:3]))
    else:
        check("无页面异常", True)

    b.close()


with sync_playwright() as p:
    run_as(p, "成员视角", MEMBER[0], MEMBER[1], expect_guide=True)
    run_as(p, "管理员视角", ADMIN[0], ADMIN[1], expect_guide=False)

print()
print("=" * 66)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
