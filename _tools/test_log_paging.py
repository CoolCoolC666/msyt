# -*- coding: utf-8 -*-
"""验收：日志**不能无限膨胀**，也不能每 3 秒整块重绘（2026-10-24 用户反馈卡顿）。

★ 用户原话：「在采集作业日志过多的情况下具有一定的卡顿问题，
  需要对于日志做一定的简略工作，例如仅仅显示500条，分页日志等等措施」

★ 原来的毛病
  ① 本机 agent 日志：**每 3 秒**把整块日志 `innerHTML` 重写一遍（还写两份）；
  ② 采集日志（SSE）：每来一条就 appendChild + removeChild(firstChild)，DOM 里常驻 600 个节点；
  ③ 任务日志：一次 400 行整块重绘。
  → 日志一多，DOM 节点多 + 反复重建 = 卡。

★ 现在的做法
  · 统一的日志视图组件：内存留 2000 行、**DOM 最多渲染 500 行**、**分页**（每页 100 行）
  · 状态条与正文**分开**更新（状态条每 3 秒在变，不该连带重绘 100 行正文）
  · 正文内容没变就不重绘（签名比对）
  · 页面不可见时**暂停轮询**
"""
from __future__ import annotations

import os
import re
import sys

ROOT = r"【项目根】"
PKG = os.path.join(ROOT, "程序")

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


app = open(os.path.join(PKG, "msyt", "web", "app.js"), encoding="utf-8").read()
helper = open(os.path.join(PKG, "msyt-helper.py"), encoding="utf-8").read()
css = open(os.path.join(PKG, "msyt", "web", "style.css"), encoding="utf-8").read()

print("=" * 72)
print("日志分页与性能验收")
print("=" * 72)

print("\n① 统一的日志视图组件")
check("有 makeLogView", "function makeLogView" in app)
check("DOM 上限 500 行", re.search(r"LOG_DOM_MAX\s*=\s*500", app) is not None)
check("每页 100 行", re.search(r"LOG_PAGE_SIZE\s*=\s*100", app) is not None)
check("内存保留 2000 行", re.search(r"LOG_KEEP\s*=\s*2000", app) is not None)
check("有分页控件（上一页/下一页/跳到最新）",
      "‹ 上一页" in app and "下一页 ›" in app and "跳到最新" in app)
check("翻到旧页就不自动跳回最新", "follow = false" in app)

print("\n② 三个日志框都换成日志视图")
check("本机 agent 日志（两个框共用）", "makeLogView(['#helper-log', '#local-agent-log']" in app)
check("任务运行情况日志", "makeLogView('#job-log'" in app)
check("采集日志（SSE）", "makeLogView('#stream-log'" in app)

print("\n③ 不再整块重绘")
check("本机 agent 日志用 set()", "logHelper.set(lines, head)" in app)
check("任务日志用 set()", "logJob.set(lines," in app)
check("采集日志用 append()", "logStream.append(" in app)
check("旧的「整块 innerHTML 重写日志」写法没了",
      "box.innerHTML = head + (lines.length" not in app)
check("旧的「每来一条就 appendChild+removeChild」写法没了",
      "box.appendChild(line)" not in app and "removeChild(box.firstChild)" not in app)
check("状态条与正文分开（head 单独一个框）", "log-head" in app)
check("正文没变就不重绘（签名比对）", "if (s === sig)" in app)

print("\n④ 页面不可见时不轮询（不做无用功）")
check("本机 agent 轮询有可见性判断", "if (document.hidden) return;" in app)
check("任务轮询有可见性判断", app.count("if (document.hidden) return;") >= 2,
      f"{app.count('if (document.hidden) return;')} 处")

print("\n⑤ 助手一次给 500 行，缓冲留 2000")
check("助手的日志缓冲是 2000", "deque(maxlen=2000)" in helper)
check("/status 一次返回 500 行", "list(STATE[\"log\"])[-500:]" in helper)

print("\n⑥ 样式")
check("有 .log-pager 样式", ".log-pager" in css)
check("有 .log-head 样式", ".log-head" in css)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
