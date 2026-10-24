# -*- coding: utf-8 -*-
"""重置某个成员的密码（管理员专用）。

用法：python reset_member_pw.py <email>
"""
import os
import secrets
import string
import sys
import time

ROOT = r"【项目根】"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.auth import hash_pw          # noqa: E402
from msyt.config import Config         # noqa: E402
from msyt.db import DB                  # noqa: E402

if len(sys.argv) < 2:
    print("用法: reset_member_pw.py <email>")
    sys.exit(2)

email = sys.argv[1].strip().lower()
alphabet = string.ascii_letters + string.digits
pw = "msyt-" + "".join(secrets.choice(alphabet) for _ in range(10))

cfg = Config.load()
db = DB(cfg.db_path)
try:
    with db.lock:
        row = db.conn.execute("SELECT id,email,role FROM members WHERE email=?",
                               (email,)).fetchone()
        if not row:
            print("★ 找不到该成员")
            sys.exit(3)
        db.conn.execute("UPDATE members SET pw_hash=? WHERE id=?",
                        (hash_pw(pw), row["id"]))
        db.conn.commit()
    print(f"已重置 {email} 的密码")
    print(f"新密码：{pw}")
    print("★ 提醒对方：登录后在右上角「改密码」自行改掉。")
finally:
    db.close()
