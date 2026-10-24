# -*- coding: utf-8 -*-
"""把某个成员账号的密码设成指定值（直接写库）。

用途：管理员在界面上「重置密码」后，只有临时口令。
如果临时口令丢了，可以在这里设一个新的 —— 不用重新走审核流程。
注意：**改密码后该账号要用新口令登录**，旧的立即失效。

用法：python _tools/set_member_pw.py <email> <new-password>
"""
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.auth import hash_pw       # noqa: E402
from msyt.config import Config      # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    email = sys.argv[1].strip().lower()
    pw = sys.argv[2]
    cfg = Config.load()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT id,email,role,status FROM members WHERE email=?",
                     (email,)).fetchone()
    if not r:
        print("找不到成员：", email)
        conn.close()
        return 1
    conn.execute("UPDATE members SET pw_hash=?, last_seen=? WHERE id=?",
                 (hash_pw(pw), time.strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
    conn.commit()
    print(f"已把 {r['email']}（id={r['id']}, role={r['role']}, "
          f"status={r['status']}）的密码设为新值")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
