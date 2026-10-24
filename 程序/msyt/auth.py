# -*- coding: utf-8 -*-
"""
auth.py —— 成员身份：邮箱账号 + 密码 + JWT
================================================================
Beta 的身份层：每个小组成员用**邮箱**登录，登录后即成为**一个采集节点**。

设计要点：
  · 密码哈希用 **`hashlib.scrypt`**（标准库）—— 本机没装 argon2/bcrypt/werkzeug，
    但 scrypt 同样是内存硬函数，比 PBKDF2 强得多，**零新增依赖**。
  · JWT 用 **PyJWT**（本机已装）。密钥存在数据库 `meta` 表里，
    首次启动自动生成并持久化 → 重启服务不失效、也不用配环境变量。
  · **白名单 = members 表**。只有表里 status='active' 的邮箱能登录；
    顺带支持「只允许某个邮箱域」的策略（组员可能用同一个学校/团队邮箱）。
  · **角色**：admin（管理员，持有权威库）/ member（成员，只回传数据）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

import jwt

#: scrypt 参数（n=2^14, r=8, p=1）—— 约 16MB 内存，够用且不慢
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

TOKEN_TTL = 7 * 24 * 3600          # 7 天


# ------------------------------------------------------------------ 密码
def hash_pw(pw: str) -> str:
    """scrypt 哈希。格式：scrypt$n$r$p$salt_b64$dk_b64"""
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                         n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                         maxmem=_SCRYPT_MAXMEM)
    return "$".join(["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
                      base64.b64encode(salt).decode(),
                      base64.b64encode(dk).decode()])


def verify_pw(pw: str, stored: str) -> bool:
    """校验密码。用 hmac.compare_digest 做常数时间比较。"""
    try:
        kind, n, r, p, s_b64, d_b64 = stored.split("$")
        if kind != "scrypt":
            return False
        salt = base64.b64decode(s_b64)
        want = base64.b64decode(d_b64)
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                             n=int(n), r=int(r), p=int(p),
                             maxmem=_SCRYPT_MAXMEM)
        return hmac.compare_digest(dk, want)
    except Exception:
        return False


# ------------------------------------------------------------------ JWT
def make_token(payload: Dict[str, Any], secret: str,
               ttl: int = TOKEN_TTL) -> str:
    now = int(time.time())
    body = dict(payload)
    body.update({"iat": now, "exp": now + ttl})
    return jwt.encode(body, secret, algorithm="HS256")


def read_token(token: str, secret: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


def new_secret() -> str:
    return secrets.token_urlsafe(48)


# ------------------------------------------------------------------ 账号
def email_allowed(email: str, members: Dict[str, Dict], allow_domains: str = "") -> Tuple[bool, str]:
    """这个邮箱能不能登录。

    规则：
      ① 必须在 members 表里（**白名单**）；自助注册会往表里加一条 `pending`
      ② 状态必须是 active：
           pending  → 「账号待管理员审核」（自助注册后等管理员放行）
           disabled → 「账号已被停用」
      ③ 若配了 allow_domains（逗号分隔，如 "qq.com,edu.cn"），
         还要域名匹配 —— 防止有人拿同域名的另一个地址冒充。
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False, "邮箱格式不对"
    m = members.get(email)
    if m is None:
        return False, "该邮箱不在成员名单里（请先注册，或找管理员添加）"
    status = (m.get("status") or "active").lower()
    if status == "pending":
        return False, "账号待管理员审核，请等管理员放行后再登录"
    if status != "active":
        return False, "账号已被停用"
    if allow_domains:
        doms = [d.strip().lower() for d in allow_domains.split(",") if d.strip()]
        dom = email.split("@", 1)[1]
        if dom not in doms:
            return False, f"该邮箱域名（{dom}）不在允许范围内"
    return True, ""
