# -*- coding: utf-8 -*-
"""bundle_path.py —— 找到本机**实际在跑**的成员端启动包目录。

★ 为什么要这个模块（2026-10-25）
  测试里原先把启动包路径**写死**成 `【成员端启动包】`。
  启动包一挪位置（这次搬到了 `2-作业` 同级），那批测试就集体
  `FileNotFoundError` —— 失败原因跟被测的东西完全无关，纯属噪音。

  → 统一走这里探测，按优先级找：
    ① 环境变量 `MSYT_BUNDLE_DIR`（要指哪指哪，最明确）
    ② **02 同级**（当前实际位置：`2-作业\\msyt-成员端启动包`）
    ③ 桌面（历史位置，留着兼容）
  判定标准：目录里要有 `msyt-helper.py` 才算「像样的启动包」。

★ 用法
    from bundle_path import find_bundle
    PKG = find_bundle()
    if not PKG:
        print("本机没有成员端启动包 → 跳过")
        sys.exit(0)
"""
from __future__ import annotations

import os

#: 启动包里必须有的文件（用来确认「这确实是个启动包」）
_MARKER = "msyt-helper.py"


def candidates() -> list[str]:
    """按优先级列出候选路径。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # here = <项目根>\\_tools  →  上两级是 2-作业
    job_dir = os.path.dirname(os.path.dirname(here))
    out = []
    env = os.environ.get("MSYT_BUNDLE_DIR")
    if env:
        out.append(env)
    out.append(os.path.join(job_dir, "msyt-成员端启动包"))   # ② 02 同级
    out.append(r"【成员端启动包】")                  # ③ 桌面（历史）
    return out


def find_bundle() -> str:
    """返回第一个**确实存在**的启动包目录；都没有就返回空串。"""
    for c in candidates():
        if c and os.path.isdir(c) and os.path.exists(os.path.join(c, _MARKER)):
            return c
    return ""


if __name__ == "__main__":
    got = find_bundle()
    print(f"找到：{got}" if got else "没找到成员端启动包")
    print("候选：")
    for c in candidates():
        mark = "✓" if os.path.isdir(c) else " "
        print(f"  {mark} {c}")
