# -*- coding: utf-8 -*-
"""probe_prompt.py —— 把站方「提示信息」页的**提示正文**挖出来。

为什么需要它
------------
详情页返回「提示信息」页时，只知道「拿不到正文」，但不知道**为什么**：
  · 站方要求登录才给看？
  · 请求被限流/风控？
  · 参数不对？
  · 帖子真的不存在？

提示页里通常有一句明确的话（Discuz 的 `alert_error` / `alert_info`），
把那句挖出来，才能判断这是「站方数据现实」还是「我们请求姿势不对」。

用法：
    python _tools\\probe_prompt.py 6820041
"""
from __future__ import annotations

import html as H
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PKG))

from msyt.config import Config, THREAD_URL     # noqa: E402
from msyt.http_client import Client            # noqa: E402


def dump(tid: int) -> None:
    cfg = Config.load()
    client = Client(cfg, verbose=False)
    url = THREAD_URL.replace("{tid}", str(tid)).replace("{page}", "1")
    r = client.get(url)
    raw = getattr(r, "text", "") or ""
    page = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")

    print(f"=== tid={tid} ===")
    print(f"URL: {url}")
    print(f"长度: {len(page)}")

    # Discuz 的提示框
    for pat, label in ((r'<div id="message".*?</div>', "message"),
                       (r'<div class="alert_error".*?</div>', "alert_error"),
                       (r'<div class="alert_info".*?</div>', "alert_info"),
                       (r'<div class="alert_warning".*?</div>', "alert_warning"),
                       (r'<div id="error_info".*?</div>', "error_info"),
                       (r'<div class="locked".*?</div>', "locked")):
        for m in re.finditer(pat, page, re.S | re.I):
            t = H.unescape(re.sub("<[^>]+>", " ", m.group(0)))
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                print(f"  [{label}] {t[:300]}")

    # 页面里的中文句子（抓提示语）
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    txt = H.unescape(re.sub("<[^>]+>", "\n", txt))
    lines = [re.sub(r"\s+", " ", x).strip() for x in txt.split("\n")]
    lines = [x for x in lines if x and len(x) > 6]
    print("  --- 页面中文行 ---")
    for x in lines[:25]:
        print(f"    {x[:160]}")


if __name__ == "__main__":
    for t in sys.argv[1:]:
        dump(int(t))
