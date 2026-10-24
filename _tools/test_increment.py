# -*- coding: utf-8 -*-
"""
test_increment.py —— 增量识别 / 重跑 / 自行清除 的验收
================================================================================
三块要验的东西：

  ① 增量识别：站方又回复了 → 本地正文过时 → 自动标记 stale 并丢回队列
     判据是**站方自己给的数字**（列表页的「回复数」变大）。

  ② 重跑：把帖丢回队列重新采（软失败 —— 数据留着）。

  ③ 自行清除：把帖的详情彻底清空（楼层/分页凭据/流程/队列项全删）。

写入类测试**跑在库副本上**；HTTP 端点测试用**专用测试 tid** 并在测完清理，
不碰权威库里的真实帖子。

用法：python _tools/test_increment.py [--base http://127.0.0.1:8765]
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("qa-member@example.com", "qa-test-password")
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(base, path, method="GET", body=None, token=""):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def login(base, cred):
    st, d = call(base, "/api/auth/login", "POST",
                 {"email": cred[0], "password": cred[1]})
    if st != 200:
        raise SystemExit(f"登录失败 {cred[0]}: {st} {d}")
    return d["token"], d["member"]


class FakeRow:
    """最小 ListRow 替身：detect_reply_growth 只看 tid / replies。"""
    def __init__(self, tid, replies):
        self.tid = tid
        self.replies = replies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("增量识别 / 重跑 / 自行清除 —— 验收")
    print("=" * 78)

    from msyt.config import Config
    from msyt.db import DB
    from msyt.detail import DetailCollector
    from msyt.http_client import Client
    from msyt.listpage import ListCollector

    cfg = Config.load()

    # ================= ① 增量识别（库副本，不碰权威库）=================
    print("\n① 增量识别：站方回复数变大 → 标记待重采")
    tmpdir = tempfile.mkdtemp(prefix="msyt-inc-")
    copy = os.path.join(tmpdir, "inc-test.db")
    shutil.copy2(cfg.db_path, copy)
    try:
        db = DB(copy)
        TID = 777000001
        with db.lock:
            db.conn.execute("DELETE FROM threads WHERE tid=?", (TID,))
            db.conn.execute(
                "INSERT INTO threads(tid,title,detail_state,replies,posts_n,chars) "
                "VALUES(?,'增量测试帖','done',3,4,100)", (TID,))
            db.conn.execute("DELETE FROM detail_queue WHERE tid=?", (TID,))
            db.conn.commit()

        # ①a 列表页报同样的回复数 → 不是增量
        growth = db.detect_reply_growth([FakeRow(TID, 3)])
        check("回复数没变 → 不判为增量", not growth, str(growth))

        # ①b 列表页报更大的回复数 → 命中
        growth = db.detect_reply_growth([FakeRow(TID, 7)])
        check("★ 回复数变大 → 判为增量", len(growth) == 1, str(growth))
        check("增量记录带新旧计数",
              bool(growth) and growth[0]["old_replies"] == 3
              and growth[0]["new_replies"] == 7,
              json.dumps(growth, ensure_ascii=False) if growth else "")

        # ①c 回复数变小 → 不判（可能是站方删楼/解析抖动，宁可漏报）
        growth = db.detect_reply_growth([FakeRow(TID, 1)])
        check("回复数变小 → 不判为增量（避免误重跑）", not growth, str(growth))

        # ①d 非 done 的帖不判（本来就没采完，没什么可过时的）
        with db.lock:
            db.conn.execute("UPDATE threads SET detail_state='pending' WHERE tid=?",
                            (TID,))
            db.conn.commit()
        growth = db.detect_reply_growth([FakeRow(TID, 99)])
        check("本地还没采完的帖 → 不判为增量", not growth, str(growth))
        with db.lock:
            db.conn.execute("UPDATE threads SET detail_state='done' WHERE tid=?",
                            (TID,))
            db.conn.commit()

        # ①e 标记 stale：状态变 stale + 丢回队列
        n = db.mark_detail_stale([TID], reason="站方又回复了")
        row = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=?", (TID,)).fetchone()
        q = db.conn.execute(
            "SELECT state, attempts, pages_done FROM detail_queue WHERE tid=?",
            (TID,)).fetchone()
        check("★ 标记 stale 成功", n == 1 and row["detail_state"] == "stale",
              row["detail_state"])
        check("★ 丢回队列（pending，计数归零）",
              bool(q) and q["state"] == "pending" and q["attempts"] == 0
              and (q["pages_done"] or 0) == 0,
              str(dict(q)) if q else "没有队列行")

        # ①f 体检：stale 不算硬问题，单独一栏（这就是"已识别的增量"）
        dc = DetailCollector(db, Client(cfg, verbose=False), cfg)
        rep = dc.integrity()
        grow_tids = [g["tid"] for g in rep.get("growth", [])]
        prob_tids = [p["tid"] for p in rep["problems"]]
        check("★ stale 不进硬问题", TID not in prob_tids, str(prob_tids[:5]))
        check("★ stale 进「已识别的增量」栏", TID in grow_tids, str(grow_tids[:5]))

        # ①g 「站方称 N+1 楼 vs 本地实收 N 楼」—— 要分两种（2026-10-23 改）：
        #    · **尾部少**（楼层号连续）→ 站方还有楼我们没采到 → **算增量**，
        #      保留原帖 + 标记 + 入队补采
        #    · **中间缺**（楼层号有缺口）→ 站方删楼/置顶 → 补采没意义，
        #      只进软警告
        #    ⚠ 先造两楼（有头有尾），否则「缺头/缺尾」会先变成**硬问题**，
        #      根本轮不到这一条。
        db.save_posts(TID, 1, [
            {"pid": 880000001, "floor": 1, "author": "甲", "author_uid": "",
             "post_time": "2026-01-01", "content": "第一楼", "chars": 3,
             "images": []},
            {"pid": 880000002, "floor": 2, "author": "乙", "author_uid": "",
             "post_time": "2026-01-02", "content": "第二楼", "chars": 3,
             "images": []},
        ])
        db.record_page(TID, 1, "880000001,880000002", "ok")
        with db.lock:
            db.conn.execute("UPDATE threads SET detail_state='done' WHERE tid=?",
                            (TID,))
            db.conn.execute(
                "UPDATE threads SET replies=9, posts_n=2 WHERE tid=?", (TID,))
            db.conn.commit()
        rep = dc.integrity()
        warn_tids = [w["tid"] for w in rep["warnings"]]
        grow_tids2 = [g["tid"] for g in rep.get("growth", [])]
        prob_tids2 = [p["tid"] for p in rep["problems"]]
        check("★ 有头有尾 → 不算硬问题", TID not in prob_tids2, str(prob_tids2[:5]))
        # ★ 尾部少（楼层号连续）→ 现在**算增量**（要补采）
        check("★ 尾部少（楼层连续）→ **进增量栏**（要补采）",
              TID in grow_tids2, str(grow_tids2[:5]))
        check("★ 尾部少 → 不再进软警告栏", TID not in warn_tids, str(warn_tids[:5]))
        _row = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=?", (TID,)).fetchone()
        check("★ 尾部少 → 被标记为 stale（保留原帖，等自动补采）",
              _row["detail_state"] == "stale", _row["detail_state"])

        # ---- ①h 中间缺（站方删楼/置顶）→ 软警告，**不**补采 ----
        TID2 = TID + 1
        # ★ 先建 threads 行（posts 有外键指向 threads）——
        #   否则插楼层会触发 FOREIGN KEY constraint failed
        with db.lock:
            db.conn.execute(
                "INSERT OR REPLACE INTO threads(tid,title,replies,detail_state,"
                "posts_n,chars,first_post_id,last_post_id,pages_total) "
                "VALUES(?,'中间缺楼层测试',2,'done',2,6,880000101,880000103,1)",
                (TID2,))
            db.conn.commit()
        db.save_posts(TID2, 1, [
            {"pid": 880000101, "floor": 1, "author": "甲", "author_uid": "",
             "post_time": "2026-01-01", "content": "第一楼", "chars": 3,
             "images": []},
            {"pid": 880000103, "floor": 3, "author": "丙", "author_uid": "",
             "post_time": "2026-01-03", "content": "第三楼", "chars": 3,
             "images": []},
        ])
        db.record_page(TID2, 1, "880000101,880000103", "ok")
        rep = dc.integrity()
        warn_tids = [w["tid"] for w in rep["warnings"]]
        grow_tids2 = [g["tid"] for g in rep.get("growth", [])]
        prob_tids2 = [p["tid"] for p in rep["problems"]]
        check("★ 中间缺 → 有头有尾，不算硬问题",
              TID2 not in prob_tids2, str(prob_tids2[:5]))
        check("★ 中间缺（站方删楼）→ 进**软警告**栏", TID2 in warn_tids,
              str(warn_tids[:5]))
        check("★ 中间缺 → **不算增量**（补采没意义）",
              TID2 not in grow_tids2, str(grow_tids2[:5]))
        _row2 = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=?", (TID2,)).fetchone()
        check("★ 中间缺 → **不**被标 stale（不会被反复重采）",
              _row2["detail_state"] == "done", _row2["detail_state"])

        # ================= ②③ 重跑 / 清除（库副本）=================
        print("\n② 重跑：丢回队列重采（数据留着）")
        db.save_posts(TID, 1, [
            {"pid": 880000001, "floor": 1, "author": "甲", "author_uid": "",
             "post_time": "2026-01-01", "content": "第一楼正文", "chars": 5,
             "images": []}])
        db.record_page(TID, 1, "880000001", "ok")
        db.set_thread_detail(TID, detail_state="done", posts_n=1, chars=5,
                             pages_total=1, first_post_id=880000001,
                             last_post_id=880000001)
        res = db.retry_details([TID], reason="验收重跑")
        row = db.conn.execute(
            "SELECT detail_state, posts_n, chars FROM threads WHERE tid=?",
            (TID,)).fetchone()
        q = db.conn.execute(
            "SELECT state, attempts FROM detail_queue WHERE tid=?", (TID,)).fetchone()
        check("★ 重跑：状态回到 pending", res["reset"] == 1
              and row["detail_state"] == "pending", row["detail_state"])
        check("★ 重跑：队列置 pending、attempts 归零",
              bool(q) and q["state"] == "pending" and q["attempts"] == 0,
              str(dict(q)) if q else "没有队列行")
        check("★ 重跑：楼层数据**保留**（软失败）", row["posts_n"] == 1,
              f"posts_n={row['posts_n']}")
        n_posts = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=?", (TID,)).fetchone()[0]
        check("★ 重跑：正文还在（层数不变）", n_posts == 2, str(n_posts))

        print("\n③ 自行清除：详情彻底清空")
        res = db.purge_details([TID])
        row = db.conn.execute(
            "SELECT detail_state, posts_n, chars, first_post_id FROM threads "
            "WHERE tid=?", (TID,)).fetchone()
        n_posts = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=?", (TID,)).fetchone()[0]
        n_pages = db.conn.execute(
            "SELECT COUNT(*) FROM thread_pages WHERE tid=?", (TID,)).fetchone()[0]
        n_q = db.conn.execute(
            "SELECT COUNT(*) FROM detail_queue WHERE tid=?", (TID,)).fetchone()[0]
        check("★ 清除：楼层删光", n_posts == 0, str(n_posts))
        check("★ 清除：分页凭据删光", n_pages == 0, str(n_pages))
        check("★ 清除：队列项删光", n_q == 0, str(n_q))
        check("★ 清除：计数归零、状态回 pending",
              row["detail_state"] == "pending" and (row["posts_n"] or 0) == 0
              and (row["chars"] or 0) == 0 and row["first_post_id"] is None,
              str(dict(row)))
        check("★ 清除：返回删了多少（界面要说清楚）",
              res["purged"] == 1 and res["posts"] >= 1, json.dumps(res))
        db.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ================= HTTP 端点（专用测试 tid，测完清理）=================
    print("\n④ HTTP 端点：/api/integrity/retry 与 /api/integrity/purge")
    try:
        atok, _ = login(base, ADMIN)
        mtok, _ = login(base, MEMBER)
    except SystemExit as e:
        print("  （服务没起来，跳过 HTTP 段）", e)
        atok = mtok = ""

    if atok:
        TID = 778000001
        conn = sqlite3.connect(cfg.db_path)
        conn.row_factory = sqlite3.Row
        for sql in ("DELETE FROM posts WHERE tid=?", "DELETE FROM thread_pages WHERE tid=?",
                    "DELETE FROM flows WHERE tid=?", "DELETE FROM detail_queue WHERE tid=?",
                    "DELETE FROM assist_conflicts WHERE tid=?"):
            conn.execute(sql, (TID,))
        conn.execute("DELETE FROM threads WHERE tid=?", (TID,))
        conn.execute(
            "INSERT INTO threads(tid,title,detail_state,posts_n,chars,pages_total) "
            "VALUES(?,'端点测试帖','done',1,5,1)", (TID,))
        conn.commit()
        db2 = DB(cfg.db_path)
        db2.save_posts(TID, 1, [
            {"pid": 881000001, "floor": 1, "author": "甲", "author_uid": "",
             "post_time": "2026-01-01", "content": "端点测试正文", "chars": 6,
             "images": []}])
        db2.record_page(TID, 1, "881000001", "ok")
        db2.close()
        try:
            st, d = call(base, "/api/integrity/retry", "POST",
                         {"tids": [TID]}, token=atok)
            check("管理员能调重跑", st == 200 and d.get("reset") == 1,
                  f"HTTP {st} {d}")
            st, d = call(base, "/api/integrity/retry", "POST",
                         {"tids": [TID]}, token=mtok)
            check("★ 成员调重跑 → 403", st == 403, f"HTTP {st}")
            st, d = call(base, "/api/integrity/purge", "POST",
                         {"tids": [TID]}, token=atok)
            check("管理员能调清除", st == 200 and d.get("purged") == 1,
                  f"HTTP {st} {d}")
            check("清除返回删了多少（界面要展示）",
                  bool(d.get("posts") or 0) >= 1, json.dumps(d, ensure_ascii=False))
            st, d = call(base, "/api/integrity/purge", "POST",
                         {"tids": [TID]}, token=mtok)
            check("★ 成员调清除 → 403", st == 403, f"HTTP {st}")
            st, d = call(base, "/api/integrity/retry", "POST",
                         {"tids": []}, token=atok)
            check("没选帖子 → 400", st == 400, f"HTTP {st}")
        finally:
            for sql in ("DELETE FROM posts WHERE tid=?", "DELETE FROM thread_pages WHERE tid=?",
                        "DELETE FROM flows WHERE tid=?", "DELETE FROM detail_queue WHERE tid=?",
                        "DELETE FROM assist_conflicts WHERE tid=?"):
                conn.execute(sql, (TID,))
            conn.execute("DELETE FROM threads WHERE tid=?", (TID,))
            conn.commit()
            conn.close()
            print("  （端点测试帖已清理干净）")

    print("\n" + "=" * 78)
    npass = sum(1 for _, ok, _ in results if ok)
    nfail = len(results) - npass
    for name, ok, extra in results:
        if not ok:
            print(f"  FAIL {name}  {extra}")
    print(f"共 {len(results)} 项 · 通过 {npass} · 失败 {nfail}")
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
