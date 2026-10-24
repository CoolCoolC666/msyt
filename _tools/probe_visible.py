# -*- coding: utf-8 -*-
"""probe_visible.py —— 找出站方「详情可见」的 tid 边界。

为什么需要它
------------
站方对**历史帖子**只给「提示信息」页（页面里明写"历史帖子仅限作者本人查看"）。
也就是说：**不是所有帖子都能采到正文**，而且这是站方的策略，不是我们程序坏了。

但边界在哪？靠猜不行，必须实测。
协作池如果装满了不可见的帖子，成员采一百条全是空 → 进度永远为 0，
界面看上去就像"程序没在采"——这正是用户反馈"无法确认是否被采集"的根因。

做法：沿 tid 从新到旧逐个试，看详情页是不是真的有楼层锚点。

用法：
    python _tools\\probe_visible.py --from 8344000 --to 8200000 --step 20000
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PKG))

from msyt.config import Config, THREAD_URL     # noqa: E402
from msyt.http_client import Client            # noqa: E402


def visible(client: Client, tid: int) -> tuple[bool, str]:
    url = THREAD_URL.replace("{tid}", str(tid)).replace("{page}", "1")
    try:
        r = client.get(url)
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败 {e}"
    raw = getattr(r, "text", "") or ""
    page = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    n = len(re.findall(r'id="post_[0-9]+"', page))
    if n:
        return True, f"{n} 个楼层锚点"
    if "历史帖子" in page:
        return False, "站方限制：历史帖子仅限作者本人查看"
    if len(page) < 3000:
        return False, f"响应很短（{len(page)} 字节）"
    return False, f"无楼层锚点（{len(page)} 字节）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="hi", type=int, default=0,
                    help="起始 tid（含），0 = 不限")
    ap.add_argument("--to", dest="lo", type=int, default=0,
                    help="结束 tid（含），0 = 不限")
    ap.add_argument("--step", type=int, default=20000)
    ap.add_argument("--samples", type=int, default=25,
                    help="在区间内取多少个**库里真实存在**的 tid 来试")
    args = ap.parse_args()

    cfg = Config.load()
    client = Client(cfg, verbose=False)

    # ★ 必须用**库里真实存在的 tid**：随手编的 tid 会 404，
    #   那是「帖子不存在」，不是「帖子不可见」，两回事。
    import sqlite3
    db = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"
    c = sqlite3.connect(str(db))
    lo = args.lo or 0
    hi = args.hi or 10 ** 12
    rows = c.execute(
        "SELECT tid FROM threads WHERE tid BETWEEN ? AND ? ORDER BY tid DESC",
        (lo, hi)).fetchall()
    tids = [r[0] for r in rows]
    if not tids:
        print("该区间没有真实 tid")
        return 1
    step = max(1, len(tids) // max(1, args.samples))
    picked = tids[::step][:args.samples]
    print(f"区间 [{lo}, {hi}] 内真实 tid 共 {len(tids)} 个，"
          f"等距抽样 {len(picked)} 个试：")
    for tid in picked:
        ok, why = visible(client, tid)
        print(f"  tid={tid:<10} {'可见 ✅' if ok else '不可见 ❌'}  {why}")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
