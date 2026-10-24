# -*- coding: utf-8 -*-
"""
test_password_echo.py —— 密码输入「带 * 回显」的逻辑测试
================================================================================
为什么要有这个测试：
  Windows 的 getpass **一个字符都不回显**，组员以为「密码无法输入」，
  其实密码早就读进去了（登录是成功的），只是屏幕上什么都没有。
  改用自己实现的 `_ask_password()`（msvcrt.getwch 逐字符读 + 打 `*`）后，
  必须保证：回显、退格、Ctrl+C、Enter 这四条分支都对。

msvcrt.getwch 读的是**控制台**，没法用管道喂 —— 所以这里把 getwch 换成一个
可编排的假字符流，验证拼装出来的密码与屏幕输出。

用法：python _tools/test_password_echo.py
"""
import io
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.cli import _ask_password     # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def run_with_fake_keys(keys, out=None):
    """把 msvcrt.getwch 换成喂固定字符，捕获 stdout。"""
    fake = types.SimpleNamespace(getwch=lambda: keys.pop(0) if keys else "\r")
    real_msvcrt = sys.modules.get("msvcrt", None)
    sys.modules["msvcrt"] = fake
    real_out = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        pw = _ask_password("  密码    : ")
    finally:
        sys.stdout = real_out
        if real_msvcrt is not None:
            sys.modules["msvcrt"] = real_msvcrt
        else:
            sys.modules.pop("msvcrt", None)
    return pw, buf.getvalue()


def main() -> int:
    print("=" * 78)
    print("密码输入回显 —— 逻辑测试")
    print("=" * 78)

    # ① 普通输入
    pw, out = run_with_fake_keys(list("abc123") + ["\r"])
    check("普通输入：拿到正确的密码", pw == "abc123", repr(pw))
    check("普通输入：每个字符回显一个 *", out.count("*") == 6, repr(out))
    check("普通输入：提示语在前面", out.startswith("  密码    : "), repr(out[:20]))
    check("普通输入：回车后换行", out.endswith("\n"), repr(out[-6:]))

    # ② 退格
    pw, out = run_with_fake_keys(list("abcd") + ["\x08", "\x08"] + list("xy") + ["\r"])
    check("退格：删掉两个字符再输入", pw == "abxy", repr(pw))
    check("退格：屏幕上留了擦除痕迹", "\b \b" in out, repr(out))

    # ③ 空密码（直接回车）
    pw, out = run_with_fake_keys(["\r"])
    check("直接回车：得到空串", pw == "", repr(pw))

    # ④ 退格删到空
    pw, out = run_with_fake_keys(list("ab") + ["\x08", "\x08", "\r"])
    check("全删掉：得到空串", pw == "", repr(pw))

    # ⑤ Ctrl+C 要能中断，而不是被当成普通字符
    raised = False
    try:
        run_with_fake_keys(list("ab") + ["\x03"])
    except KeyboardInterrupt:
        raised = True
    check("Ctrl+C：抛出 KeyboardInterrupt（不吞掉）", raised, "")

    # ⑥ 密码本身**不出现在**屏幕上
    pw, out = run_with_fake_keys(list("secret") + ["\r"])
    check("★ 密码明文不出现在屏幕上", "secret" not in out, repr(out))

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
