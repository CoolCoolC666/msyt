# -*- coding: utf-8 -*-
"""闸：存证域（案件 + 哈希链 + 证据包）必须真能证明自己，也必须真能发现篡改。

★ 为什么这个闸存在
  「存证」最容易变成**自我感动**：导出一个 zip、写一句 SHA256，
  看起来很像证据，但从来没验证过「如果有人改了会怎样」。
  这个闸跑三件事：
    ① 正向：建案 → 挂帖 → 导出 → 三查必须全 OK
    ② 反向 1：改包内正文 → 校验必须 FAILED 且指出是哪个文件
    ③ 反向 2：改链上某条记录 → 链校验必须报「断裂于 seq=N」
  只跑在**临时库 + 临时目录**，不碰真库、不碰 2.5 GB 的 data。

用法：python _tools/test_evidence.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.db import DB                              # noqa: E402
from msyt.evidence import (Chain, _verify_zip, add_item,     # noqa: E402
                           archive_page, create_case, export_case,
                           record_snapshot, verify_case)

FAILED: list[str] = []


def check(ok: bool, msg: str) -> None:
    if ok:
        print("  OK   " + msg)
    else:
        print("  FAIL " + msg)
        FAILED.append(msg)


def seed(db: DB) -> None:
    """造一条最小可用的真实形状数据（字段名与线上一致）。"""
    with db.lock:
        db.conn.execute(
            "INSERT INTO threads(tid,title,url,author,author_uid,pub_time,pub_day,"
            "pub_ts,replies,views,detail_state,posts_n,chars,state,category) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (9001, "楼道灯坏了三周没人修", "https://bbs.cnnb.com.cn/forum.php?mod=viewthread&tid=9001",
             "居民甲", "123456", "2026-11-20 08:30", "2026-11-20", 1763598600,
             2, 120, "done", 2, 60, "已受理", "投诉"))
        db.conn.execute(
            "INSERT INTO posts(pid,tid,page_no,floor,author,author_uid,post_time,"
            "post_ts,content,chars,images,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (900101, 9001, 1, 1, "居民甲", "123456", "2026-11-20 08:30", 1763598600,
             "楼道灯坏了三周，物业一直没修。", 15, "[]", "2026-11-20 09:00:00"))
        db.conn.execute(
            "INSERT INTO posts(pid,tid,page_no,floor,author,author_uid,post_time,"
            "post_ts,content,chars,images,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (900102, 9001, 1, 2, "物业客服", "654321", "2026-11-21 10:00", 1763690400,
             "已安排电工，本周内更换。", 12, "[]", "2026-11-21 10:05:00"))
        db.conn.execute(
            "INSERT INTO list_changes(ts,source,tid,kind,old_val,new_val,note) "
            "VALUES(?,?,?,?,?,?,?)",
            ("2026-11-21 09:00:00", "global", 9001, "state", "待受理", "已受理", "站方改状态"))
        db.conn.execute(
            "INSERT INTO flows(tid,step,name,time,ts) VALUES(?,?,?,?,?)",
            (9001, 1, "已受理", "2026-11-21 09:00", 1763686800))
        db.conn.commit()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="msyt_ev_")
    db = DB(os.path.join(tmp, "msyt.db"))
    seed(db)

    # ---- ① 建案 + 挂帖 ----
    print("\n--- ① 建案与挂帖 ---")
    c = create_case(db, "楼道灯长期失修（物业未响应）", level="一般",
                    category="投诉纠纷", owner="张三", created_by="测试")
    check(c["case_id"].startswith("MSYT-"), f"案件编号 = {c['case_id']}")
    check(c["status"] == "open", "初始状态 open")
    r = add_item(db, c["case_id"], 9001, role="first", added_by="测试")
    check(r["tid"] == 9001 and r["role"] == "first", "条目已挂上")
    chain = Chain(db.conn)
    check(chain.head()[0] == 3, f"链已有 3 条（genesis/create/item，实得 {chain.head()[0]}）")

    # 同一条目重复挂 → 幂等（不报错、不重复）
    add_item(db, c["case_id"], 9001, role="first")
    n_items = db.conn.execute("SELECT COUNT(*) FROM case_items WHERE case_id=?",
                              (c["case_id"],)).fetchone()[0]
    check(n_items == 1, f"重复挂同一条目仍是 1 条（实得 {n_items}）")

    # ---- ② 载体：随采集归档（模拟 detail 采到那一页时）----
    print("\n--- ② 载体随采集归档 ---")
    url = ("https://bbs.cnnb.com.cn/forum.php?mod=viewthread&tid=9001&page=1&extra=page%3D1")
    page_html = ('<html><head><title>楼道灯坏了三周没人修</title></head>'
                 '<body><div id="post_900101">楼道灯坏了三周</div></body></html>')
    got = archive_page(db, 9001, 1, url, page_html)
    check(len(got) == 1, f"归档 1 页（实得 {len(got)}）")
    check(got[0]["rel_path"].startswith("pages/9001/p1_"), f"落盘路径 = {got[0]['rel_path']}")
    check(os.path.exists(os.path.join(tmp, "evidence", got[0]["rel_path"])),
          "文件确实在磁盘上")
    again = archive_page(db, 9001, 1, url, page_html)
    check(len(again) == 0, "同内容重复归档被去重")
    nf = db.conn.execute("SELECT COUNT(*) FROM evidence_files WHERE kind='html'"
                         ).fetchone()[0]
    check(nf == 1, f"evidence_files 里只 1 条（实得 {nf}）")
    check(archive_page(db, 9999999, 1, "u", "<html/>") == [],
          "非案件 tid 直接 no-op（61 万帖不会被存成 HTML）")
    snap = record_snapshot(db, 9001)
    check(bool(snap) and snap["views"] == 120, f"热度快照写入：{snap}")

    # ---- ③ 导出 + 正向三查 ----
    print("\n--- ③ 导出证据包并三查 ---")
    ev = export_case(db, c["case_id"], out_dir=os.path.join(tmp, "ev"),
                     operator="测试员")
    check(os.path.exists(ev["zip"]), "zip 已生成")
    check(os.path.exists(ev["zip"][:-4] + ".zip.sha256.txt"), "外部 .sha256.txt 已生成")
    check(os.path.exists(ev["zip"][:-4] + ".export.json"), "链侧车已生成")
    with zipfile.ZipFile(ev["zip"]) as z:
        names = set(z.namelist())
        man = json.loads(z.read("manifest.json").decode("utf-8"))
        posts = json.loads(z.read("content/posts.json").decode("utf-8"))
    check({"manifest.json", "manifest.sha256", "verify.py", "custody.md",
           "chain/chain_slice.json"} <= names, "包结构齐全")
    check(len(posts) == 2, f"两层正文全在（实得 {len(posts)}）")
    check(man["scope"]["changes"] == 1, "变更史入包")
    check(man["clock_source"] == "local", "时钟来源如实标注 local")
    check(man["carriers"] == 1, f"manifest 记录 1 个载体（实得 {man['carriers']}）")
    check(any(f["path"].startswith("carrier/pages/9001/") for f in man["files"]),
          "载体文件进了 manifest")
    check(any(n.startswith("carrier/pages/9001/") for n in names), "载体文件进了 zip")
    car = next(f for f in man["files"] if f["path"].startswith("carrier/pages/9001/"))
    check(car["source_url"].endswith("page%3D1"), f"原始 URL 原样记录：{car['source_url'][-24:]}")
    check(car["http_status"] == 200, "http_status 记录")
    check(not any("不含网页载体" in x for x in man["limitations"]),
          "有载体时不再声称「不含载体」")
    check(any("服务器原始 HTML" in x for x in man["limitations"]),
          "如实说明载体是原始 HTML 而非截图")

    v = verify_case(db, c["case_id"])
    check(v["ok"], "三查全过（zip/包内/链）")
    check(v["outside"]["match_db"], "zip 与库记录的 bundle_sha256 一致")

    # ---- ④ 反向 1：改包内正文 ----
    print("\n--- ④ ★反向验证 1：改一个字符（同长度，绕开大小校验）---")
    bad_zip = os.path.join(tmp, "bad.zip")
    with zipfile.ZipFile(ev["zip"]) as z:
        items = {n: z.read(n) for n in z.namelist()}
    data = bytearray(items["content/posts.json"])
    idx = data.find("楼道灯".encode("utf-8"))
    data[idx] ^= 0x01          # 同长度改一个字节：只改内容，不改大小
    items["content/posts.json"] = bytes(data)
    with zipfile.ZipFile(bad_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in items.items():
            z.writestr(n, d)
    res = _verify_zip(bad_zip)
    check(not res["ok"], "改了正文 → 校验 FAILED")
    check(any("哈希不符" in x and "posts.json" in x for x in res["bad"]),
          f"同长度改动被哈希抓住：{res['bad'][:1]}")

    # ---- ④ 反向 2：改链上一条记录 ----
    print("\n--- ④ ★反向验证 2：改链上一条 ---")
    check(Chain(db.conn).verify()["ok"], "改之前链是完整的（对照）")
    with db.lock:
        db.conn.execute("UPDATE custody_chain SET at=? WHERE seq=2",
                        ("2020-01-01 00:00:00",))
        db.conn.commit()
    cv = Chain(db.conn).verify()
    check(not cv["ok"], "改了链上时间 → 校验 FAILED")
    check(cv["broken_at"] == 2, f"断点定位到 seq=2（实得 {cv['broken_at']}）")

    # ---- ⑤ 边界：封存后不许加条目 / 空案件不许导出 ----
    print("\n--- ⑤ 边界 ---")
    db.conn.execute("UPDATE cases SET status='sealed', sealed_at=? WHERE case_id=?",
                    ("2026-11-26 12:00:00", c["case_id"]))
    db.conn.commit()
    try:
        add_item(db, c["case_id"], 9001, role="reply")
        check(False, "封存后仍能加条目（应报错）")
    except ValueError:
        check(True, "封存后加条目被拒")
    c2 = create_case(db, "空案件（无条目）")
    try:
        export_case(db, c2["case_id"])
        check(False, "空案件也导出了（应报错）")
    except ValueError:
        check(True, "空案件导出被拒")

    print("\n" + "=" * 62)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
