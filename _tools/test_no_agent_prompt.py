# -*- coding: utf-8 -*-
"""验收：组员**没起 Agent** 就点「开始采集」时，提示是否说清楚了「先起 Agent」。

做法：把页面的 HELPER_URL 指到一个**没人监听的端口**，
      模拟「助手没起」；再点「开始采集」，看提示文案。
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765/"
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


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1400, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="load")
    pg.wait_for_timeout(1000)
    pg.fill("#lg-email", "test@example.com")
    pg.fill("#lg-pw", "test-password")
    pg.click("#lg-btn")
    pg.wait_for_timeout(2000)
    pg.click("#tabs button[data-tab='collect']")
    pg.wait_for_timeout(1500)

    # ★ 把助手地址指到一个没人监听的端口 = 模拟「助手没起」
    pg.evaluate("() => { window.HELPER_URL = 'http://127.0.0.1:59999'; }")
    # app.js 里 HELPER_URL 是 const —— 用 addInitScript 改不了，
    # 所以退而求其次：直接把 fetch 拦下来对 8766 报错
    pg.route("**/127.0.0.1:8766/**", lambda route: route.abort())
    pg.wait_for_timeout(3500)

    # 徽章应该显示「未运行」
    badge = pg.evaluate(
        "() => (document.querySelector('#agent-first-state')||{}).innerText || ''")
    check("徽章提示助手不可用", ("未运行" in badge) or ("没" in badge), badge.strip()[:90])

    # 选**一个**部门（别用「全选」：109 个源 × 500 页 = 54,500 次请求
    #   > 5000 上限会弹 confirm，Playwright 默认取消对话框 → 采集根本没启动）
    pg.evaluate("""() => {
        const cb = document.querySelector('#picker-list .picker-list input[type=checkbox]');
        if (cb) { cb.click(); }
    }""")
    pg.wait_for_timeout(400)
    # 万一还是弹了确认框，一律接受
    pg.on("dialog", lambda d: d.accept())
    pg.click("#btn-list")
    pg.wait_for_timeout(3000)

    # 找 toast（界面右下角的提示条）
    toast = pg.evaluate(
        "() => { const els = Array.from(document.querySelectorAll('body *'));"
        " const hit = els.filter(e => (e.className||'').toString().match(/toast/i));"
        " return hit.map(e => (e.innerText||'').trim()).filter(Boolean).join(' | '); }")
    check("点「开始采集」后有 toast 提示", bool(toast), toast[:150])
    check("★ 提示里明确说「起 Agent」",
          ("起 Agent" in toast) or ("启动本机" in toast) or ("还没起" in toast), toast[:150])

    # 助手消息区
    hmsg = pg.evaluate(
        "() => (document.querySelector('#helper-msg')||{}).innerText || ''")
    check("助手消息区也提示了", bool(hmsg.strip()), hmsg.strip()[:120])
    check("★ 助手消息里提到「先起 Agent」",
          ("起 Agent" in hmsg) or ("先起" in hmsg), hmsg.strip()[:120])

    # ★ 关键：不该真的发起采集（不能去刷站方）
    print("\n  ※ 检查是否误发采集请求：")
    bad = pg.evaluate("() => window.__sentList === true")
    check("没有真正发起服务器端采集", not bad)

    if errs:
        check("无页面异常", False, str(errs[:2]))
    else:
        check("无页面异常", True)
    b.close()

print()
print("=" * 66)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
