# -*- coding: utf-8 -*-
"""给 msyt 加一个成员账号（逻辑与 webapp 的 POST /api/members 一致）。

★ 为什么直接写库而不用接口：
  管理员账号的密码我不知道（也不该去猜/重置），
  而这个账号是**新增**的，走 DB 层用同一套 hash_pw + 同样的字段，
  结果与接口完全一致。

用法：python add_member.py <email> [备注]
"""
import os
import secrets
import string
import sys
import time

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.auth import hash_pw          # noqa: E402
from msyt.config import Config         # noqa: E402
from msyt.db import DB                  # noqa: E402

if len(sys.argv) < 2:
    print("用法: add_member.py <email> [备注]")
    sys.exit(2)

email = sys.argv[1].strip().lower()
note = sys.argv[2] if len(sys.argv) > 2 else ""
alphabet = string.ascii_letters + string.digits
pw = "msyt-" + "".join(secrets.choice(alphabet) for _ in range(10))

cfg = Config.load()
db = DB(cfg.db_path)
try:
    with db.lock:
        if db.conn.execute("SELECT 1 FROM members WHERE email=?",
                           (email,)).fetchone():
            print("★ 该邮箱已存在，未改动")
            sys.exit(3)
        db.conn.execute(
            "INSERT INTO members(email,pw_hash,role,node_id,status,quota,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (email, hash_pw(pw), "member", f"member:{email}", "active", 50, note,
             time.strftime("%Y-%m-%d %H:%M:%S")))
        db.conn.commit()
    r = db.conn.execute("SELECT id,email,role,node_id,status,quota,note FROM members "
                        "WHERE email=?", (email,)).fetchone()
    print("已新增成员：")
    print(f"  id={r['id']}  {r['email']}  role={r['role']}  node={r['node_id']}")
    print(f"  status={r['status']}  quota={r['quota']}  note={r['note']!r}")
    print(f"\n初始密码：{pw}")
    print("★ 提醒对方：登录后请在右上角「改密码」自行改掉这个临时口令。")
finally:
    db.close()
