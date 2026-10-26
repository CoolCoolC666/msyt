# -*- coding: utf-8 -*-
"""test_agent_multi_endpoint.py —— 验收「agent 多地址选址 + 地址池热更新」。

★ 这次改的是什么（2026-10-26）：
  公网入口是**隧道地址**，隧道一重启地址就变；而且境内按 SNI 封锁了一批
  域名后缀，能用的公网入口只剩 Cloudflare 托管型后缀（quick tunnel）。
  所以 agent 不能只认「一个主地址 + 一个备用」，
  得能认**一串**候选，并且能从服务端把新地址捞回来。

★ 验收四件事：
  ① `server-alt.txt` 里的多行地址**都**进了候选池
  ② 命令行传来的**多行**地址被按行拆开（不是当成一个带换行的 URL）
  ③ 主地址不通时，会自动选中**一个真的能通的**公网地址
  ④ `_refresh_endpoints()` 能把服务端 `/api/agent/endpoints` 的地址合并进来

用法：python _tools\\test_agent_multi_endpoint.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
PKG = PROJ.parent / "msyt-成员端启动包"
sys.path.insert(0, str(PKG))

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✓ {name}")
    else:
        FAILS.append(name)
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    try:
        from msyt.agent import Agent
    except Exception as e:                                   # noqa: BLE001
        print("导入 msyt.agent 失败:", e)
        return 1

    alt_file = PKG / "server-alt.txt"
    file_urls = [ln.strip() for ln in alt_file.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")] if alt_file.is_file() else []
    print(f"[信息] server-alt.txt 里有 {len(file_urls)} 个地址")

    # ① 文件里的多行地址都进候选池
    ag = Agent(server="http://127.0.0.1:8765")
    cands = ag._alt_candidates()
    check("① server-alt.txt 的地址都进了候选池",
          all(u in cands for u in file_urls),
          f"cands={cands}")
    check("① 候选池去重", len(cands) == len(set(cands)), f"cands={cands}")

    # ② 命令行传多行 → 按行拆开
    multi = "\n".join(["https://a.example.com", "https://b.example.com"])
    ag2 = Agent(server="http://127.0.0.1:8765", server_alt=multi)
    c2 = ag2._alt_candidates()
    check("② 命令行多行地址被按行拆开",
          "https://a.example.com" in c2 and "https://b.example.com" in c2
          and not any("\n" in x for x in c2), f"c2={c2}")

    # ③ 主地址不通 → 自动选一个真能通的公网地址
    ag3 = Agent(server="http://127.0.0.1:9")
    picked = ag3.pick_server()
    check("③ 主地址不通时选到了备用地址", picked != "http://127.0.0.1:9", f"picked={picked}")
    live = [u for u in file_urls if u == picked]
    check("③ 选中的地址来自 server-alt.txt", bool(live), f"picked={picked}")

    # ④ 从服务端拉地址池 → 合并进来
    ag4 = Agent(server="http://127.0.0.1:8765")
    ag4._refresh_endpoints("http://127.0.0.1:8765")
    check("④ 地址池从服务端拉到了地址", len(ag4._alt_list) > 0,
          f"alt_list={ag4._alt_list}")
    check("④ 服务端给的地址也在候选池里",
          all(u in ag4._alt_candidates() for u in ag4._alt_list),
          f"cands={ag4._alt_candidates()}")

    # ★ 汇总行格式要跟回归 runner 认的一致（`共 N 项 · 通过 N · 失败 N`），
    #   否则统计不到 → 被误判成 FAIL。
    print(f"\n共 {PASSES + len(FAILS)} 项 · 通过 {PASSES} · 失败 {len(FAILS)}")
    if FAILS:
        for f in FAILS:
            print("  失败:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
