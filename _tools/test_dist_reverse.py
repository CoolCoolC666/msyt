# -*- coding: utf-8 -*-
"""分发包**真反向验证**：把分发包弄坏 → 同一批断言必须报红。

为什么要有它：闸自己报绿不能证明闸有用。
只有「把实现改坏 → 同一批断言报红」才能证明这套断言真的在测那件事。

用法：
    python _tools/test_dist_reverse.py [分发包目录]

默认分发包目录 = 项目同级目录下的 ../msyt-本地分发版
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
PKG = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (
    PROJ.parent / "msyt-本地分发版")
GATE = PROJ / "_tools" / "test_dist_package.py"
PY = sys.executable
BACKUP = PROJ / "_备份" / "_dist_rev_backup"


def run_gate():
    r = subprocess.run([PY, str(GATE), str(PKG)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout or ""


def restore_pkg():
    for name in os.listdir(BACKUP):
        s = BACKUP / name
        d = PKG / name
        if d.exists():
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        shutil.copytree(s, d) if s.is_dir() else shutil.copy2(s, d)


def case(name, mutate):
    restore_pkg()
    mutate()
    code, out = run_gate()
    ok = (code != 0)
    print("[%s] %s -> gate exit=%d %s" % ("PASS" if ok else "FAIL", name, code,
                                          "(报红 OK)" if ok else "(没报红!)"))
    if not ok:
        print(out[-1500:])
    return ok


def mut_email():
    f = PKG / "msyt" / "web" / "index.html"
    f.write_text(f.read_text(encoding="utf-8") + "\n<!-- admin@example.com -->",
                 encoding="utf-8")


def mut_crlf():
    f = PKG / "启动.cmd"
    raw = f.read_bytes().replace(b"\r\n", b"\n")
    f.write_bytes(raw)


def mut_dep():
    f = PKG / "requirements.txt"
    lines = [l for l in f.read_text(encoding="utf-8").splitlines()
             if not l.lower().startswith("uvicorn")]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mut_pw():
    db = sqlite3.connect(str(PKG / "data" / "msyt.db"))
    db.execute("UPDATE members SET pw_hash='x' WHERE id=1")
    db.commit()
    db.close()


def mut_ticket():
    db = sqlite3.connect(str(PKG / "data" / "msyt.db"))
    db.execute("INSERT INTO agent_tickets(ticket,email,server,expires_at) "
               "VALUES('t','e','s',0)")
    db.commit()
    db.close()


def mut_rows():
    db = sqlite3.connect(str(PKG / "data" / "msyt.db"))
    db.execute("DELETE FROM threads WHERE tid IN (SELECT tid FROM threads LIMIT 100)")
    db.commit()
    db.close()


def mut_missing():
    f = PKG / "启动.cmd"
    f.unlink()


def mut_baseline():
    """把基线数字改大 → 「包内实际 == 基线」必须报红。"""
    f = PKG / "manifest.json"
    man = json.loads(f.read_text(encoding="utf-8"))
    man["tables"]["threads"] = man["tables"]["threads"] + 1
    f.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")


def mut_manifest_gone():
    (PKG / "manifest.json").unlink()


def _cleanup() -> None:
    """被打断（Ctrl-C / 超时 kill）时也要把包恢复原样 —— 
    否则会留下一个「少了几行数据」或「多了个 manifest」的坏包。"""
    if BACKUP.exists():
        try:
            restore_pkg()
            print("[cleanup] 包已恢复原样")
        except Exception as e:      # 恢复失败要看得见，不能静默
            print("[cleanup] 恢复失败：%s（备份还在 %s，手动拷回）" % (e, BACKUP))


if __name__ == "__main__":
    if not PKG.exists():
        print("分发包目录不存在：%s" % PKG)
        sys.exit(2)
    if BACKUP.exists():
        # 上一次跑被打断 → 手里这份备份就是**完好包**，先恢复再重跑
        print("发现残留备份（上次跑被打断），先恢复包 ...")
        restore_pkg()
        shutil.rmtree(BACKUP, ignore_errors=True)
    shutil.copytree(PKG, BACKUP)
    import signal
    signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(143)))
    print("backup done")
    results = []
    results.append(case("源码里塞真实邮箱", mut_email))
    results.append(case("启动.cmd 改成 LF 换行", mut_crlf))
    results.append(case("requirements 删掉 uvicorn", mut_dep))
    results.append(case("members 口令哈希重新填上", mut_pw))
    results.append(case("agent_tickets 塞回一张票据", mut_ticket))
    results.append(case("删掉 100 条 threads", mut_rows))
    results.append(case("删掉 启动.cmd", mut_missing))
    results.append(case("基线数字被改大", mut_baseline))
    results.append(case("删掉 manifest.json", mut_manifest_gone))
    restore_pkg()
    shutil.rmtree(BACKUP, ignore_errors=True)
    code, out = run_gate()
    print("restore -> gate exit=%d" % code)
    results.append(code == 0)
    print("-" * 60)
    print("反向验证 %d/%d 通过" % (sum(1 for x in results if x), len(results)))
    sys.exit(0 if all(results) else 1)
