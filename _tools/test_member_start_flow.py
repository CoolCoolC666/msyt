# -*- coding: utf-8 -*-
"""验收：成员点「开始采集」必须**有反应**（2026-10-24 用户实测反馈）。

★ 事故经过
  组员点「采集作业」里的「开始采集」→ **什么也没发生**。
  查下来是三件事叠在一起：
  ① 旧逻辑要求成员**先手动点一次「启动本机 agent」**，再回来点「开始采集」；
     agent 没跑时 `startLocalCollect` 直接 return —— 成员看到的就是「点了没反应」。
  ② 所有反馈都写在**上面那张「成员端工具」卡片**里（#helper-msg），
     而成员盯着的是**下面**的按钮 → 提示跑到别处 = 没反应。
  ③ 本机 agent 的**日志**也只在那张卡里；成员看的「任务运行情况」永远是空的
     （成员的采集不在服务器上跑）→ 看着像什么都没发生。

★ 这个测试盯住这几件事不许退化
"""
from __future__ import annotations

import os
import re
import sys

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
WEB = os.path.join(ROOT, "程序", "msyt", "web")
APP = os.path.join(WEB, "app.js")
HTML = os.path.join(WEB, "index.html")

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


app = open(APP, encoding="utf-8").read()
html = open(HTML, encoding="utf-8").read()
# 扫描代码时先剥掉注释（注释里举例写的代码会被误判）
code = re.sub(r"/\*.*?\*/", "", app, flags=re.S)
code = re.sub(r"(?m)^\s*//.*$", "", code)
code = re.sub(r"(?m)\s+//.*$", "", code)

print("=" * 72)
print("成员「开始采集」必须有反应")
print("=" * 72)

print("\n① 点一下就自动把本机 agent 起来（不用再手动点一次）")
m = re.search(r"async function startLocalCollect\(.*?\n\}", code, re.S)
check("能找到 startLocalCollect", bool(m))
body = m.group(0) if m else ""
check("会调 helperCall('/start'…)", "'/start'" in body)
check("★ 不再因为「agent 没跑」就 return（那是旧的毛病）",
      "if (!st.running)" not in body,
      "仍有 !st.running 的提前 return" if "if (!st.running)" in body else "")
check("把网页令牌递给助手（成员不用输密码）",
      "token: state.token" in body)

print("\n② 反馈写在**按钮旁边**，不是别处的卡片")
check("startLocalCollect 接受提示位参数", re.search(r"startLocalCollect\([^)]*msgEl", code) is not None)
check("列表采集按钮传了 #j-msg", "$('#j-msg')" in code)
check("详情采集按钮传了 #dd-msg", "$('#dd-msg')" in code)
check("页面上有 #j-msg", 'id="j-msg"' in html)
check("页面上有 #dd-msg", 'id="dd-msg"' in html)
check("提示用 ‹› 强调（不写 ** 免得显示成字面星号）",
      "‹" in body and "**你电脑**" not in body)

print("\n③ 本机 agent 的日志要显示在成员眼前")
check("页面上有「本机 agent」卡片", 'id="local-agent-card"' in html)
check("卡片里有状态行", 'id="local-agent-state"' in html)
check("卡片里有日志框", 'id="local-agent-log"' in html)
check("轮询时把状态/日志也写进这张卡",
      "$('#local-agent-log')" in code and "$('#local-agent-state')" in code)

print("\n④ agent 秒退（没活可采）也要说人话")
check("有 watchLocalStart（盯到 agent 退出）", "function watchLocalStart" in code)
check("★ 盯到退出为止（不能在第一次检查就 return）",
      re.search(r"if \(s\.running\)[\s\S]{0,200}?continue;", code) is not None)
check("能识别「已采完/没活可采」", "没活可采" in code or "已采完" in code)
check("能识别「中途出错」", "中途出错" in code)

print("\n⑤ 成员视角的「任务运行情况」空态要说清楚")
check("空态文案提到「本机 agent」",
      re.search(r"isMemberView\(\)[\s\S]{0,400}本机 agent", code) is not None)
check("「本机 agent」卡片只给成员看",
      "$('#local-agent-card').style.display = isAdmin ? 'none' : ''" in code)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
