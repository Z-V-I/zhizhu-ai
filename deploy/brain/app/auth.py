"""后台账号体系：账号 + 密码 + 服务端会话。

为什么不用 JWT：
  这是单人后台，会话总量可用一只手数。放一张表里能"删行即踢下线"，
  比维护吊销列表简单得多，也方便在后台里直接看到"谁在哪儿登录着"。

为什么不用 bcrypt：
  bcrypt 需要编译扩展，而这个镜像已经够重了。PBKDF2-HMAC-SHA256 是标准库，
  26 万次迭代在单次登录上是几十毫秒，对"人工输密码登录"完全够。
  迭代次数存在行里，以后调高不会让旧密码失效。

首次启动：
  没有任何管理员时，用环境变量 ADMIN_USERNAME / ADMIN_PASSWORD 建一个 owner。
  之后密码就在后台改，不再看环境变量。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import db

log = logging.getLogger("brain.auth")

SESSION_DAYS = 7
ITERATIONS = 260_000


def _hash(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()


def _new_salt() -> str:
    return secrets.token_hex(16)


async def ensure_owner() -> None:
    """没有任何管理员时，从环境变量建一个。只跑一次。"""
    row = await db.fetchrow("SELECT count(*) AS n FROM admin_users")
    if row and row["n"]:
        return
    username = (os.getenv("ADMIN_USERNAME") or "admin").strip()
    password = os.getenv("ADMIN_PASSWORD") or ""
    if not password:
        log.warning("没有管理员账号，且未设置 ADMIN_PASSWORD —— 后台无法登录")
        return
    salt = _new_salt()
    await db.execute(
        """INSERT INTO admin_users (username, pw_salt, pw_hash, iterations, display_name, is_owner)
           VALUES ($1, $2, $3, $4, $5, true)""",
        username,
        salt,
        _hash(password, salt, ITERATIONS),
        ITERATIONS,
        "管理员",
    )
    log.info("已创建初始管理员：%s", username)


async def verify(username: str, password: str) -> Optional[dict[str, Any]]:
    row = await db.fetchrow("SELECT * FROM admin_users WHERE username = $1", username.strip())
    if not row or row["disabled"]:
        return None
    calc = _hash(password, row["pw_salt"], row["iterations"])
    if not hmac.compare_digest(calc, row["pw_hash"]):
        return None
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "is_owner": row["is_owner"],
    }


async def create_session(username: str, ip: str, ua: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.execute(
        """INSERT INTO admin_sessions (token, username, ip, user_agent, expires_at)
           VALUES ($1, $2, $3, $4, $5)""",
        token,
        username,
        ip[:64],
        ua[:300],
        expires,
    )
    await db.execute("UPDATE admin_users SET last_login_at = now() WHERE username = $1", username)
    return token


async def whoami(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    row = await db.fetchrow(
        """SELECT s.username, s.expires_at, u.display_name, u.is_owner, u.disabled
           FROM admin_sessions s JOIN admin_users u ON u.username = s.username
           WHERE s.token = $1""",
        token,
    )
    if not row or row["disabled"]:
        return None
    if row["expires_at"] < datetime.now(timezone.utc):
        await db.execute("DELETE FROM admin_sessions WHERE token = $1", token)
        return None
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "is_owner": row["is_owner"],
    }


async def drop_session(token: str) -> None:
    await db.execute("DELETE FROM admin_sessions WHERE token = $1", token)


async def change_password(username: str, old: str, new: str) -> bool:
    me = await verify(username, old)
    if not me:
        return False
    salt = _new_salt()
    await db.execute(
        "UPDATE admin_users SET pw_salt = $2, pw_hash = $3, iterations = $4 WHERE username = $1",
        username,
        salt,
        _hash(new, salt, ITERATIONS),
        ITERATIONS,
    )
    # 改密后把其它会话全部踢掉，只保留当前这次（调用方会重新登录）
    await db.execute("DELETE FROM admin_sessions WHERE username = $1", username)
    return True


async def log_login(
    actor: str,
    ok: bool,
    username: str = "",
    reason: str = "",
    ip: str = "",
    ua: str = "",
) -> None:
    """登录审计。失败也要记 —— 连续失败是唯一的入侵信号。"""
    try:
        await db.execute(
            """INSERT INTO login_events (actor, email, ok, reason, ip, user_agent)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            actor,
            username[:120],
            ok,
            reason[:200],
            ip[:64],
            ua[:300],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("log_login failed: %s", exc)
