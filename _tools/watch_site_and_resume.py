"""守护：站方详情入口恢复后，自动续跑「站方计数缺陷」归位。

为什么需要它（2026-10-29 实测）
------------------------------------------------------------------
2026-10-29 00:20 前后，站方 `bbs.cnnb.com.cn` **整域**在 nginx 层 302 到
`http://www.cnnb.com.cn/luntan/zw/zw.html?t=ms...`（留言板首页）。
实测范围：
  · `/forum.php?mod=viewthread&tid=...`（详情页）
  · `/forum.php?mod=forumdisplay&fid=1176`（列表页）
  · `/ajax_getpost.php`（楼层正文）
  · `/plugin.php?id=xt_mvthread:thread`（移动端插件）
  · `/index.php`、`/robots.txt`、`/`   ← 连 robots.txt 都 302
→ 是**整域导流**，不是逐帖、不是频率、不是缺 cookie、不是 UA/TLS 指纹
   （真实 Chrome 打开同样落到留言板首页；站方首页自己给出的帖链接也全部走不通）。

所以这是**站方侧故障/改版**，不是我们的问题。
归位脚本对实测失败的帖一律「不动」（不下结论），剩余 3079 条保持 `stale`，
既没被误标、也没丢数据 —— 缺的只是「等它好了再跑一遍」。

本脚本做的事：
  1. 每 ``--interval`` 分钟探一次活（只打**一个**帖的详情页，低频）
  2. 探到能解析出 token → 认为站方恢复 → 调 `fix_count_mismatch.py --apply`
     续跑剩余归位（它会自己选 `detail_state='stale' AND detail_warn LIKE '%少贴%'`
     的集合，已归位的不在集合里，不会重复处理）
  3. 连续失败则**退避**（10 → 20 → 30 → 60 分钟封顶），不给站方添乱
  4. 最多守护 ``--max-hours`` 小时（默认 12），超时退出，日志留痕

★ 探活判据必须是「**能解析出 token**」，不是「HTTP 200」。
  站方故障时返回的也是 200（留言板首页），只看状态码会**误判为已恢复**。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PROJ = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(PROJ, '程序'))

from msyt.config import Config, THREAD_URL            # noqa: E402
from msyt.db import DB                                # noqa: E402
from msyt.http_client import Client                   # noqa: E402
from msyt.parsers import parse_thread_page            # noqa: E402

DBP = os.path.join(PROJ, '程序', 'data', 'msyt.db')
LOG = os.path.join(PROJ, '_tools', '_watch_site.log')
FIXER = os.path.join(PROJ, '_tools', 'fix_count_mismatch.py')


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def pending_count(db) -> int:
    return db.conn.execute(
        "SELECT COUNT(*) FROM threads "
        "WHERE detail_state='stale' AND detail_warn LIKE '%少贴%'").fetchone()[0]


def sample_tid(db):
    r = db.conn.execute(
        "SELECT tid FROM threads "
        "WHERE detail_state='stale' AND detail_warn LIKE '%少贴%' "
        "ORDER BY RANDOM() LIMIT 1").fetchone()
    return int(r["tid"]) if r else None


def probe_alive(client) -> tuple[bool, str]:
    """探活：能解析出 token 才算活。"""
    db = DB(DBP)
    tid = sample_tid(db)
    db.close()
    if tid is None:
        return False, "库里没有待归位的帖了（可能已全部处理完）"
    url = THREAD_URL.format(tid=tid, page=1)
    try:
        r = client.get(url, referer=url)
    except Exception as e:                              # noqa: BLE001
        return False, f"请求异常：{type(e).__name__}: {e}"
    try:
        html = r.content.decode('gb18030', errors='replace')
    except Exception:                                  # noqa: BLE001
        html = r.text
    tp = parse_thread_page(html, tid=tid, page_no=1)
    if tp.token:
        return True, f"tid={tid} 拿到 token（站方详情入口已恢复）"
    return False, f"tid={tid} 仍拿不到 token（HTTP {r.status_code}，落地 {r.url}）"


def run_fixer(workers: int) -> int:
    cmd = [sys.executable, FIXER, '--apply', '--workers', str(workers), '--no-backup']
    log(f'站方已恢复 → 续跑归位：{" ".join(cmd)}')
    p = subprocess.run(cmd, cwd=PROJ, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    tail = (p.stdout or '').strip().splitlines()[-12:]
    for ln in tail:
        log('  | ' + ln)
    if p.returncode != 0:
        log(f'  !! 归位脚本退出码 {p.returncode}')
        log('  !! ' + ((p.stderr or '').strip()[-800:] or '(无 stderr)'))
    return p.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=int, default=600, help='探活间隔秒（默认 600）')
    ap.add_argument('--max-hours', type=float, default=12, help='最长守护小时数（默认 12）')
    ap.add_argument('--workers', type=int, default=3, help='恢复后归位并发数（默认 3）')
    ap.add_argument('--once', action='store_true', help='只探一次就退出')
    args = ap.parse_args()

    cfg = Config.load() if hasattr(Config, 'load') else Config()
    db = DB(DBP)
    n = pending_count(db)
    db.close()
    log(f'启动守护：待归位 {n} 条；探活间隔 {args.interval}s；最长 {args.max_hours}h')

    interval = args.interval
    fails = 0
    deadline = time.time() + args.max_hours * 3600
    recovered = False

    while True:
        if time.time() > deadline:
            log(f'已达最长守护时长（{args.max_hours}h），退出。')
            return 0

        client = Client(cfg, verbose=False)
        alive, why = probe_alive(client)
        log(('✔ ' if alive else '✘ ') + why)

        if alive:
            if not recovered:
                recovered = True
                run_fixer(args.workers)
                db2 = DB(DBP)
                left = pending_count(db2)
                db2.close()
                log(f'续跑结束，剩余待归位 {left} 条')
                if left == 0:
                    log('★ 全部归位完成，守护退出。')
                    return 0
            else:
                log('站方仍是活的，但已无新增待归位帖，继续守护。')
            interval = args.interval
            fails = 0
        else:
            fails += 1
            if not recovered:
                # 退避：10 → 20 → 30 → 60 分钟封顶
                interval = min(args.interval * (2 ** min(fails - 1, 3)), 3600)
                log(f'  连续失败 {fails} 次 → 下次探活间隔 {interval // 60} 分钟')

        if args.once:
            return 0
        time.sleep(interval)


if __name__ == '__main__':
    raise SystemExit(main())
