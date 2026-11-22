"""运营后台 API（/api/ops/*）。

为什么另起一个前缀而不是塞进 /api/admin：
  LibreChat 自己也占了 /api/admin/*（角色、用户配置），
  在 Caddy 上把整个前缀转给 brain 会把它的管理接口全部 404。
  /api/ops 是"运营"（ops）专属通道，不会撞。

权限模型只有一条：任何 /api/ops/* 都要求后台会话 token。
唯一的例外是注册与邀请码校验（/api/ops/public/*），它们本来就是给未登录用户用的。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import auth, db, ima, ima_sync, llm, memory_build, mg, providers, store

log = logging.getLogger("brain.ops")

router = APIRouter()
CONSOLE = Path(__file__).with_name("console.html")
# console.html 里预留的插槽。统计脚本在响应时按环境变量填进去 ——
# 直接写死在 HTML 里的话，开源出去的版本会带着别人家的站点 id。
UMAMI_SLOT = "<!--UMAMI-->"

API_INTERNAL = os.getenv("API_INTERNAL", "http://api:3080")
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")


# ---------------- 页面 ----------------

@router.get("/console")
async def console_page():
    if not CONSOLE.exists():
        raise HTTPException(500, "console.html 丢了")
    # 统计脚本从环境变量拼：这样开源版里 console.html 不含任何第三方站点 id，
    # 站长填了 UMF_UMAMI_* 才有埋点，不填就是一个空串（页面照常）。
    html = CONSOLE.read_text(encoding="utf-8")
    src = (os.getenv("UMF_UMAMI_SRC") or "").strip()
    site = (os.getenv("UMF_UMAMI_ID") or "").strip()
    tag = (
        f'<script defer src="{src}" data-website-id="{site}"></script>'
        if src and site
        else ""
    )
    html = html.replace(UMAMI_SLOT, tag, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/admin")
async def legacy_admin():
    """/admin 是老入口（只配模型）。现在整套后台在 /console，直接跳过去。"""
    return RedirectResponse("/console#/models", status_code=307)


def _tags(v: Any) -> list[str]:
    """asyncpg 把 jsonb 读成字符串，这里统一成数组给前端。"""
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v) if isinstance(v, str) else []
        return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001
        return []


# ---------------- 鉴权依赖 ----------------

def _ip(request: Request) -> str:
    # CF 在前面，真实 IP 在 CF-Connecting-IP；直连时退回 client.host
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )


async def _me(request: Request) -> dict[str, Any]:
    token = request.headers.get("x-ops-token") or ""
    me = await auth.whoami(token)
    if not me:
        raise HTTPException(401, "后台会话已过期，请重新登录")
    return me


# ---------------- 登录 ----------------

@router.post("/api/ops/auth/login")
async def login(request: Request):
    body = await request.json()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    ip, ua = _ip(request), request.headers.get("user-agent", "")

    if not username or not password:
        raise HTTPException(400, "账号和密码都要填")

    me = await auth.verify(username, password)
    if not me:
        await auth.log_login("admin", False, username, "账号或密码不对", ip, ua)
        raise HTTPException(401, "账号或密码不对")

    token = await auth.create_session(username, ip, ua)
    await auth.log_login("admin", True, username, "", ip, ua)
    return {"token": token, "user": me}


@router.post("/api/ops/auth/logout")
async def logout(request: Request):
    token = request.headers.get("x-ops-token") or ""
    if token:
        await auth.drop_session(token)
    return {"ok": True}


@router.get("/api/ops/me")
async def me(request: Request):
    return await _me(request)


@router.post("/api/ops/password")
async def password(request: Request):
    me = await _me(request)
    body = await request.json()
    old = str(body.get("old") or "")
    new = str(body.get("new") or "")
    if len(new) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    if not await auth.change_password(me["username"], old, new):
        raise HTTPException(400, "原密码不对")
    return {"ok": True, "relogin": True}


# ---------------- 邀请码 ----------------

def _gen_code() -> str:
    # 去掉容易看错的 0/O/1/I/l，邀请码可能被手抄或念出来
    alphabet = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")
    raw = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


@router.get("/api/ops/invites")
async def list_invites(request: Request):
    await _me(request)
    rows = await db.fetch(
        """SELECT code, nickname, note, created_at, expires_at, used_at, used_by, used_email, revoked
           FROM invites ORDER BY created_at DESC LIMIT 300"""
    )
    return {"invites": [dict(r) for r in rows]}


@router.post("/api/ops/invites")
async def create_invite(request: Request):
    """建邀请码。`code` 传了就用手输的，不传就自动生成。"""
    me = await _me(request)
    body = await request.json()
    nickname = str(body.get("nickname") or "").strip()
    note = str(body.get("note") or "").strip()
    days = int(body.get("expires_days") or 0)
    raw_code = str(body.get("code") or "").strip()
    custom = _norm_invite_code(raw_code)
    if not nickname:
        raise HTTPException(400, "请填写这个人的昵称（注册时要对上）")
    # LibreChat 对 name 有 min(3) 校验。中文两个字（如"小林"）就会被拒，
    # 与其让朋友注册时报错，不如在这里就拦住，说清楚要求。
    if len(nickname) < 3:
        raise HTTPException(400, "昵称至少 3 个字符（中文两字名会被注册接口拒绝，可写成「小林同学」）")

    # 手输邀请码：校验 → 查重 → 用原样（大写、去分隔符后统一格式）
    if custom:
        # 先判字符集，再判长度。反过来的话，输了一串汉字会被剥成空串，
        # 报"至少 4 个字符"——用户会以为是长度问题，改半天还是错。
        if raw_code and not re.fullmatch(r"[A-Za-z0-9\-\s]+", raw_code):
            raise HTTPException(400, "只能用字母、数字和短横线（-），不能有汉字或其它符号")
        if len(custom) < 4:
            raise HTTPException(400, "邀请码至少 4 个字符")
        if len(custom) > 32:
            raise HTTPException(400, "邀请码最长 32 个字符")
        if not re.fullmatch(r"[A-Z0-9\-]+", custom):
            raise HTTPException(400, "只能用字母、数字和短横线（-）")
        # 库里按大写存，登录时也归一成大写，两边口径必须一致
        code = custom
        dup = await db.fetchrow("SELECT code FROM invites WHERE code = $1", code)
        if dup:
            raise HTTPException(409, f"邀请码「{code}」已经存在了，换一个")
    else:
        code = _gen_code()
        # 自动生成的撞了库（概率极低）就再摇一次，别把错误抛给用户
        for _ in range(5):
            dup = await db.fetchrow("SELECT code FROM invites WHERE code = $1", code)
            if not dup:
                break
            code = _gen_code()

    expires = datetime.now(timezone.utc) + timedelta(days=days) if days > 0 else None
    await db.execute(
        """INSERT INTO invites (code, nickname, note, created_by, expires_at)
           VALUES ($1, $2, $3, $4, $5)""",
        code,
        nickname,
        note,
        me["username"],
        expires,
    )
    return {"ok": True, "code": code, "nickname": nickname, "expires_at": expires, "custom": bool(custom)}


@router.post("/api/ops/invites/{code}/revoke")
async def revoke_invite(code: str, request: Request):
    """作废邀请码。**并且立刻把用这个码的人踢下线。**

    为什么必须一起做：只把码标成 revoked，只能拦住"下一次登录"；
    已经在用的设备还能靠 refreshToken 续命好几天。对"收回访问权"这个动作来说，
    "过几天才失效"等于没做。删掉他的 sessions，下一次换 token 就 401。
    """
    await _me(request)
    row = await db.fetchrow("SELECT used_by, auto_email FROM invites WHERE code = $1", code)
    await db.execute("UPDATE invites SET revoked = true WHERE code = $1", code)
    kicked = 0
    if row and row["used_by"] and mg.ready():
        kicked = await asyncio.to_thread(mg.kick_sessions, row["used_by"])
    return {"ok": True, "kicked_sessions": kicked}


@router.delete("/api/ops/invites/{code}")
async def delete_invite(code: str, request: Request):
    await _me(request)
    row = await db.fetchrow("SELECT used_at FROM invites WHERE code = $1", code)
    if row and row["used_at"]:
        raise HTTPException(400, "已使用的邀请码不能删除（审计需要）")
    await db.execute("DELETE FROM invites WHERE code = $1", code)
    return {"ok": True}


# ---------------- 注册（公开，带邀请码 + Turnstile） ----------------

async def _turnstile_ok(token: str, ip: str) -> tuple[bool, str]:
    """校验 CF Turnstile。没配 secret 时直接放行（本地/未接入阶段）。"""
    if not TURNSTILE_SECRET:
        return True, "turnstile-disabled"
    if not token:
        return False, "缺少人机验证"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip},
            )
        data = r.json()
        if data.get("success"):
            return True, ""
        return False, "人机验证未通过"
    except Exception as exc:  # noqa: BLE001
        log.warning("turnstile verify failed: %s", exc)
        return False, "人机验证服务不可用"


@router.post("/api/ops/public/clientlog")
async def client_log(request: Request):
    """前端错误上报。只记日志，不落库 —— 这是排查工具，不是业务数据。

    为什么要它：前端裁剪层跑在别人的构建产物上，出错时浏览器里只留一片空白，
    远程看不到 console。把 error.message + stack 送回服务端是唯一可行的取证手段。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False}
    # 排查期需要完整上下文（DOM 结构 dump 会长达几千字）。
    # 400 太短，会把关键字段切掉，反而浪费一轮往返。
    msg = str(body.get("message") or "")[:4000]
    stack = str(body.get("stack") or "")[:1500]
    where = str(body.get("where") or "")[:120]
    log.warning("[client] %s | %s | %s", where, msg, stack.replace("\n", " <- "))
    return {"ok": True}


@router.get("/api/ops/public/config")
async def public_config():
    """登录页需要知道 Turnstile 的 site key。"""
    return {
        "invite_required": True,
        "mode": "invite-login",
        "turnstile_site_key": TURNSTILE_SITE_KEY,
    }


# ---------------- 邀请码登录（公开） ----------------
#
# 2026-11 改版：站点只对朋友开放，不开放自助注册。
# 邀请码就是通行证 —— 第一次被使用时自动建号，之后同一个码就是登录凭证。
# 这样 /login 上只需要一个输入框，朋友不用记邮箱、也不用记密码；
# 想收回某人的访问权，后台把码作废即可。

INVITE_EMAIL_DOMAIN = os.getenv("INVITE_EMAIL_DOMAIN", "invite.local")

# 试码限速。10 位码的空间够大，但一个不限速的公开接口
# 等于把服务器借给别人压测上游注册/登录。
_INV_TRIES: dict[str, list[float]] = {}
INV_TRIES_MAX = 20
INV_TRIES_WINDOW = 600


def _norm_invite_code(raw: Any) -> str:
    """归一化邀请码：小写、漏掉中间连字符、带空格都算对。

    ★ 只在"用户自己没写连字符的 10 位码"上补回 XXXXX-XXXXX。
      以前是无条件按长度 10 就插 —— 于是站长手输 `ABTEST-2026`
      （正好 10 个字母数字）会被悄悄改成 `ABTES-T2026`，
      他给朋友的码和库里存的码不一致，谁也进不来。
      现在先看原文有没有连字符：有就保留原样，只做大写化和去空格。
    """
    raw_s = str(raw or "").strip().upper()
    had_dash = "-" in raw_s
    s = "".join(ch for ch in raw_s if ch.isalnum())
    if not had_dash and len(s) == 10:
        return s[:5] + "-" + s[5:]
    if had_dash:
        # 保留原有的分段位置，只清掉多余空白与非法字符
        cleaned = "".join(ch for ch in raw_s if ch.isalnum() or ch == "-")
        return re.sub(r"-{2,}", "-", cleaned).strip("-")
    return s


def _invite_rate_ok(ip: str) -> bool:
    now = time.time()
    arr = [t for t in _INV_TRIES.get(ip, []) if now - t < INV_TRIES_WINDOW]
    _INV_TRIES[ip] = arr
    if len(_INV_TRIES) > 5000:  # 别让字典无限长
        for k in list(_INV_TRIES)[:2500]:
            _INV_TRIES.pop(k, None)
    return len(arr) < INV_TRIES_MAX


def _invite_rate_hit(ip: str) -> None:
    _INV_TRIES.setdefault(ip, []).append(time.time())


@router.post("/api/ops/public/invite-login")
async def invite_login(request: Request):
    """邀请码 → 会话。

    校验顺序：人机验证 → 限速 → 码本身 → 建号/登录。
    人机验证放在最前面，否则机器人可以拿无效码把注册接口刷爆。
    """
    body = await request.json()
    ip = _ip(request)
    ua = request.headers.get("user-agent", "umf")[:300]

    if not _invite_rate_ok(ip):
        raise HTTPException(429, "尝试太频繁，请过一会儿再试")
    _invite_rate_hit(ip)

    ok, why = await _turnstile_ok(
        str(body.get("turnstileToken") or body.get("cf-turnstile-response") or ""), ip
    )
    if not ok:
        raise HTTPException(400, why)

    code = _norm_invite_code(body.get("code") or body.get("inviteCode"))
    if not code:
        raise HTTPException(400, "请输入邀请码")

    row = await db.fetchrow("SELECT * FROM invites WHERE code = $1", code)
    if not row:
        await auth.log_login("user", False, "", f"邀请码不存在: {code}", ip, ua)
        raise HTTPException(404, "邀请码不存在")
    if row["revoked"]:
        await auth.log_login("user", False, "", f"邀请码已作废: {code}", ip, ua)
        raise HTTPException(403, "邀请码已作废")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        await auth.log_login("user", False, "", f"邀请码已过期: {code}", ip, ua)
        raise HTTPException(403, "邀请码已过期")

    # ---------- 第一次使用：用码建号 ----------
    email = row["auto_email"] or ""
    password = providers.decrypt_key(row["pw_enc"] or "") if row["pw_enc"] else ""

    if not email or not password:
        if row["used_at"]:
            # 老机制下注册过的码：账号是朋友自己填的邮箱密码，我们无从得知。
            raise HTTPException(409, "这个邀请码绑定的是早期账号，请改用邮箱密码登录")

        email = f"{code.replace('-', '').lower()}@{INVITE_EMAIL_DOMAIN}"
        password = secrets.token_urlsafe(24)
        name = str(row["nickname"] or "").strip()
        # 上游 name 有 min(3)，中文两字名会被拒（创建时已拦，这里兜历史数据）
        if len(name) < 3:
            name = (name + "同学")[:8] or "朋友"

        payload = {"email": email, "password": password, "confirm_password": password, "name": name}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{API_INTERNAL}/api/auth/register",
                    json=payload,
                    headers={"user-agent": ua, "content-type": "application/json"},
                )
        except Exception as exc:  # noqa: BLE001
            log.error("invite register upstream failed: %s", exc)
            raise HTTPException(502, "建号服务暂时不可用，请稍后再试")

        if r.status_code >= 300:
            detail = ""
            try:
                detail = (r.json() or {}).get("message", "")
            except Exception:  # noqa: BLE001
                detail = r.text[:160]
            log.warning("invite %s register failed: %s %s", code, r.status_code, detail)
            raise HTTPException(400, detail or "这个邀请码没能建号，请联系管理员")

        data = r.json() if r.content else {}
        uid = ((data.get("user") or {}).get("_id")) or ""
        # 凭据回存：下次凭同一个码登录要靠它。
        # 加密存（Fernet），和厂商密钥同一套机制 —— 库被看到也不至于直接登录。
        await db.execute(
            """UPDATE invites
                  SET auto_email = $2, pw_enc = $3, used_by = COALESCE(NULLIF(used_by, ''), $4),
                      used_email = COALESCE(used_email, $2)
                WHERE code = $1""",
            code,
            email,
            providers.encrypt_key(password),
            uid,
        )
        log.info("invite %s created account %s", code, email)

    # ---------- 登录 ----------
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{API_INTERNAL}/api/auth/login",
                json={"email": email, "password": password},
                headers={"user-agent": ua, "content-type": "application/json"},
            )
    except Exception as exc:  # noqa: BLE001
        log.error("invite login upstream failed: %s", exc)
        raise HTTPException(502, "登录服务暂时不可用，请稍后再试")

    if r.status_code >= 300:
        await auth.log_login("user", False, email, f"邀请码登录失败 HTTP {r.status_code}", ip, ua)
        log.warning("invite %s login failed: %s", code, r.text[:200])
        raise HTTPException(403, "这个邀请码暂时登不进来，请联系管理员")

    data = r.json() if r.content else {}
    user = data.get("user") or {}

    # 成功后才算"已使用"：建号成功但登录失败时，码还能重试。
    await db.execute(
        "UPDATE invites SET used_at = COALESCE(used_at, now()), used_by = COALESCE(NULLIF(used_by, ''), $2) WHERE code = $1",
        code,
        str(user.get("_id") or ""),
    )
    try:
        await auth.log_login("user", True, email, "邀请码登录", ip, ua)
    except Exception:  # noqa: BLE001
        pass

    # 把上游的 Set-Cookie 原样转给浏览器。
    # 不这么做的话，前端拿到 200 也还是未登录 —— LibreChat 的会话就是这几个 cookie。
    resp = JSONResponse({"ok": True, "user": user})
    for c in r.headers.get_list("set-cookie"):
        resp.headers.append("set-cookie", c)
    return resp


@router.post("/api/ops/public/register")
async def public_register(request: Request):
    """自助注册已关闭。

    Caddy 仍把 /api/auth/register 转到这（否则就会漏给上游），
    所以这里必须明确拒绝 —— 否则等于留了一条绕过邀请码的后门。
    """
    log.info("blocked self-registration from %s", _ip(request))
    raise HTTPException(403, "自助注册已关闭，请用邀请码登录")


# ---------------- 用户 ----------------

@router.get("/api/ops/overview")
async def overview(request: Request):
    await _me(request)
    stats = {
        "users": 0,
        "invites_unused": 0,
        "invites_used": 0,
        "memories": 0,
        "logins_24h": 0,
        "mongo": mg.ready(),
    }
    try:
        row = await db.fetchrow("SELECT count(*) AS n FROM invites WHERE used_at IS NULL AND NOT revoked")
        stats["invites_unused"] = row["n"]
        row = await db.fetchrow("SELECT count(*) AS n FROM invites WHERE used_at IS NOT NULL")
        stats["invites_used"] = row["n"]
        row = await db.fetchrow("SELECT count(*) AS n FROM memories WHERE deleted_at IS NULL")
        stats["memories"] = row["n"]
        row = await db.fetchrow("SELECT count(*) AS n FROM login_events WHERE at > now() - interval '24 hours'")
        stats["logins_24h"] = row["n"]
    except Exception as exc:  # noqa: BLE001
        log.warning("overview stats failed: %s", exc)
    if mg.ready():
        stats["users"] = mg.user_count()
    return stats


@router.get("/api/ops/users")
async def users(request: Request, q: str = ""):
    await _me(request)
    if not mg.ready():
        return {"users": [], "mongo": False, "hint": "Mongo 未连接，用户列表不可用（模型与邀请码不受影响）"}

    people = mg.list_users(limit=300, q=q)
    # 每个用户带上记忆条数与最近登录，后台一眼能看出"谁在用、谁只是注册了"
    ids = [p["id"] for p in people]
    mem: dict[str, int] = {}
    last: dict[str, Any] = {}
    if ids:
        for r in await db.fetch(
            """SELECT user_id, count(*) AS n FROM memories
               WHERE deleted_at IS NULL AND user_id = ANY($1::text[]) GROUP BY user_id""",
            ids,
        ):
            mem[r["user_id"]] = r["n"]
        for r in await db.fetch(
            """SELECT DISTINCT ON (user_id) user_id, at, ip FROM login_events
               WHERE user_id = ANY($1::text[]) ORDER BY user_id, at DESC""",
            ids,
        ):
            last[r["user_id"]] = {"at": r["at"], "ip": r["ip"]}
    for p in people:
        p["memories"] = mem.get(p["id"], 0)
        p["last_login"] = last.get(p["id"])
    return {"users": people, "mongo": True}


@router.get("/api/ops/users/{uid}/conversations")
async def user_conversations(uid: str, request: Request):
    await _me(request)
    if not mg.ready():
        raise HTTPException(503, "Mongo 未连接")
    u = mg.get_user(uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    return {"user": u, "conversations": mg.list_conversations(uid), "stats": mg.conversation_stats(uid)}


@router.get("/api/ops/users/{uid}/conversations/{cid}")
async def conversation_detail(uid: str, cid: str, request: Request):
    """只读。后台不发消息 —— 这里没有、也不会有任何写上游的调用。"""
    await _me(request)
    if not mg.ready():
        raise HTTPException(503, "Mongo 未连接")
    msgs = mg.conversation_messages(cid)
    summary, _covered = await store.get_summary(cid)
    return {
        "conversation_id": cid,
        "messages": msgs,
        "summary": summary,
        "compressed": bool(summary),
        "message_count": len(msgs),
    }


@router.get("/api/ops/users/{uid}/insight")
async def user_insight(uid: str, request: Request):
    """一个人的全部"可读"内容：画像 / 长期记忆 / 话题 / 时间线 / 短期上下文 / 评价。

    ★ 和用户自己的记忆板块共用 `memory_build.insight` —— 这是"两块看到的内容必须
      完全一样"的唯一保证。两边各写一份查询，迟早会漂移出"后台能看到、用户看不到"
      或者更糟的反向情况。这里只多做两件事：加 review、加 mongo 口径的统计。
    """
    await _me(request)
    data = await memory_build.insight(uid, with_review=True)
    stats = mg.conversation_stats(uid) if mg.ready() else {}
    if stats:
        data["stats"] = {**data.get("stats", {}), **stats}
    data["ima"] = {"available": False, "hint": "IMA 知识库当前未接入（无可用连接器）。接入后这里会变成对谈内容的语义检索。"}
    return data


@router.post("/api/ops/users/{uid}/memory/rebuild")
async def rebuild_memory(uid: str, request: Request):
    """后台触发"从历史对话整理记忆"。同一时间只跑一个（重复点不重复花钱）。"""
    await _me(request)
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    cur = await memory_build.job_status(uid)
    if cur and cur["status"] == "running":
        return {"job_id": cur["id"], "running": True, "job": cur}
    job_id = await memory_build.rebuild(uid, force=bool(body.get("force")))
    return {"job_id": job_id, "running": True, "job": await memory_build.job_status(uid)}


@router.get("/api/ops/users/{uid}/memory/job")
async def memory_job(uid: str, request: Request):
    await _me(request)
    return {"job": await memory_build.job_status(uid)}


# ---------------------------------------------------------------- 代他更正
#
# 站长也有编辑/删除权，理由很实际：用户看到错误内容不一定会来找你，
# 但会截图发给你说"这什么乱七八糟的"。既然他找的是你，你就得能当场改掉。
#
# 关键点：这些接口写的是**同一条更正记录**（memory_corrections），
# 不区分是谁按的按钮 —— 对重新生成来说，"本人否认了这条"的效力是一样的。
# 在 note 里标是谁改的，事后能查。

def _edit_note(body: dict[str, Any], who: str) -> str:
    note = str(body.get("note") or "").strip()
    return (f"{note}｜{who}" if note else who)[:400]


@router.patch("/api/ops/users/{uid}/memories/{memory_id}")
async def ops_edit_memory(uid: str, memory_id: str, request: Request):
    me = await _me(request)
    body = await request.json()
    try:
        got = await store.update_memory(
            uid, memory_id, text=body.get("text"), layer=body.get("layer"),
            note=_edit_note(body, f"站长 {me['username']} 修改"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not got:
        raise HTTPException(404, "not found")
    await memory_build.maybe_refresh_profile(uid, threshold=2)
    return {"ok": True, **got}


@router.delete("/api/ops/users/{uid}/memories/{memory_id}")
async def ops_delete_memory(uid: str, memory_id: str, request: Request):
    me = await _me(request)
    if not await store.delete_memory(uid, memory_id, note=f"站长 {me['username']} 删除"):
        raise HTTPException(404, "not found")
    await memory_build.maybe_refresh_profile(uid, threshold=2)
    return {"ok": True}


@router.patch("/api/ops/users/{uid}/topics/{topic_id}")
async def ops_edit_topic(uid: str, topic_id: str, request: Request):
    me = await _me(request)
    body = await request.json()
    got = await store.update_topic(
        uid, topic_id, title=body.get("title"), summary=body.get("summary"),
        note=_edit_note(body, f"站长 {me['username']} 修改"),
    )
    if not got:
        raise HTTPException(404, "not found")
    return {"ok": True, **got}


@router.delete("/api/ops/users/{uid}/topics/{topic_id}")
async def ops_delete_topic(uid: str, topic_id: str, request: Request):
    me = await _me(request)
    if not await store.delete_topic(uid, topic_id, note=f"站长 {me['username']} 删除"):
        raise HTTPException(404, "not found")
    return {"ok": True}


@router.patch("/api/ops/users/{uid}/digests/{conversation_id}")
async def ops_edit_digest(uid: str, conversation_id: str, request: Request):
    me = await _me(request)
    body = await request.json()
    got = await store.update_digest(
        uid, conversation_id, title=body.get("title"), one_line=body.get("one_line"),
        note=_edit_note(body, f"站长 {me['username']} 修改"),
    )
    if not got:
        raise HTTPException(404, "not found")
    return {"ok": True, **got}


@router.patch("/api/ops/users/{uid}/profile")
async def ops_edit_profile(uid: str, request: Request):
    me = await _me(request)
    body = await request.json()
    section = body.get("section")
    if section not in ("basics", "traits", "interests", "values", "ongoing"):
        raise HTTPException(400, "section 必须是 basics/traits/interests/values/ongoing 之一")
    prof = await store.update_profile_section(
        uid, section=section, items=body.get("items"), headline=body.get("headline"),
        note=_edit_note(body, f"站长 {me['username']} 修改"),
    )
    return {"ok": True, "profile": prof}


@router.get("/api/ops/users/{uid}/corrections")
async def ops_corrections(uid: str, request: Request):
    await _me(request)
    return {"corrections": await store.list_corrections(uid, limit=100)}


@router.post("/api/ops/users/{uid}/review")
async def make_review(uid: str, request: Request):
    """用便宜模型对这个人生成一段"评价"。按需触发，结果缓存。

    评价只基于数据，不做人格评判：写的是"这个人怎么用、关心什么、沟通习惯"。
    """
    await _me(request)
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    force = bool(body.get("force"))

    if not force:
        row = await db.fetchrow(
            "SELECT summary, tags, generated_at FROM user_reviews WHERE user_id = $1", uid
        )
        if row:
            return {
                "summary": row["summary"],
                "tags": _tags(row["tags"]),
                "generated_at": row["generated_at"],
                "cached": True,
            }

    if not mg.ready():
        raise HTTPException(503, "Mongo 未连接，无法取到对话样本")

    texts = mg.recent_user_texts(uid, limit=100)
    profile = await store.get_profile(uid)
    mem = await db.fetch(
        """SELECT layer, text FROM memories WHERE user_id = $1 AND deleted_at IS NULL
           ORDER BY created_at DESC LIMIT 60""",
        uid,
    )
    stats = mg.conversation_stats(uid)
    u = mg.get_user(uid) or {}

    if not texts and not mem:
        raise HTTPException(400, "这个人还没有可分析的对话或记忆")

    prompt = (
        "只根据下面事实，用中文写一张用户卡片。禁止编造，禁止评价人品。\n"
        '只输出 JSON，不要解释：{"summary":"120字以内","tags":["3-6个短标签"]}\n'
        f"用户：{u.get('name') or u.get('email')}\n"
        f"对话 {stats.get('conversations')} 条 / 消息 {stats.get('messages')} 条\n"
        f"画像：{json.dumps(profile, ensure_ascii=False)[:1200]}\n"
        f"记忆：{json.dumps([r['text'] for r in mem], ensure_ascii=False)[:2400]}\n"
        f"最近发言：{json.dumps(texts[:60], ensure_ascii=False)[:3000]}"
    )

    # 这类"结构化输出"的调用会偶发返回空：思考型模型可能把 token 花在内联推理上，
    # 或 max_tokens 刚好卡在 JSON 之前。重试一次并把预算放宽，是唯一实用的兜底。
    reply = ""
    for attempt in range(2):
        try:
            reply = await llm.complete(
                "cheap-extract", [{"role": "user", "content": prompt}], max_tokens=800 + 600 * attempt
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("review llm attempt %s failed: %s", attempt + 1, exc)
            reply = ""
        if reply.strip():
            break
        log.info("review llm empty (attempt %s), retrying", attempt + 1)

    if not reply.strip():
        raise HTTPException(502, "模型这次没返回内容，稍后再试")

    summary, tags = "", []
    try:
        i, j = reply.find("{"), reply.rfind("}")
        data = json.loads(reply[i : j + 1]) if i >= 0 and j > i else {}
        summary = str(data.get("summary") or "").strip()
        tags = [str(t)[:20] for t in (data.get("tags") or [])][:6]
    except Exception:  # noqa: BLE001
        pass
    if not summary:
        # 模型没按 JSON 回，就整段当摘要，别让后台看到空白
        summary = reply.strip()[:600]

    based_on = {
        "conversations": stats.get("conversations"),
        "messages": stats.get("messages"),
        "memories": len(mem),
    }
    await db.execute(
        """INSERT INTO user_reviews (user_id, summary, tags, model, based_on, generated_at)
           VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, now())
           ON CONFLICT (user_id) DO UPDATE
           SET summary = EXCLUDED.summary, tags = EXCLUDED.tags, model = EXCLUDED.model,
               based_on = EXCLUDED.based_on, generated_at = now()""",
        uid,
        summary,
        json.dumps(tags, ensure_ascii=False),
        "cheap-extract",
        json.dumps(based_on, ensure_ascii=False),
    )
    row = await db.fetchrow(
        "SELECT summary, tags, generated_at FROM user_reviews WHERE user_id = $1", uid
    )
    return {
        "summary": row["summary"],
        "tags": _tags(row["tags"]),
        "generated_at": row["generated_at"],
        "cached": False,
    }


@router.get("/api/ops/logins")
async def logins(request: Request, actor: str = "", ok: str = "", q: str = "", limit: int = 200):
    await _me(request)
    where, args = [], []
    if actor in ("user", "admin"):
        args.append(actor)
        where.append(f"actor = ${len(args)}")
    if ok in ("true", "false"):
        args.append(ok == "true")
        where.append(f"ok = ${len(args)}")
    if q.strip():
        args.append(f"%{q.strip()}%")
        where.append(f"(email ILIKE ${len(args)} OR ip ILIKE ${len(args)})")
    args.append(min(max(limit, 1), 500))
    sql = "SELECT id, actor, user_id, email, ok, reason, ip, user_agent, at FROM login_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY at DESC LIMIT ${len(args)}"
    rows = await db.fetch(sql, *args)
    return {"events": [dict(r) for r in rows], "ima": {"available": False}}


@router.post("/api/ops/logins")
async def log_user_login(request: Request):
    """给 LibreChat 侧回填用户登录记录用（内部调用，需 admin token）。

    为什么不直接读 Mongo：LibreChat 的登录不走我们的库，
    要让"登录记录"完整，只能两边都记。这个接口由 Caddy 在 /api/auth/login
    成功响应后由前端脚本回填（见 umf-ui.js）。
    """
    body = await request.json()
    await auth.log_login(
        "user",
        bool(body.get("ok", True)),
        str(body.get("email") or ""),
        str(body.get("reason") or ""),
        _ip(request),
        request.headers.get("user-agent", ""),
    )
    # 顺手把 user_id 补上：后台要能从一条登录记录直接跳到那个人。
    # 用子查询定位刚插入的那行，避免并发下错行。
    email = str(body.get("email") or "")[:120]
    if mg.ready() and email:
        try:
            u = mg.col("users").find_one({"email": email}, {"_id": 1})
            if u:
                await db.execute(
                    """UPDATE login_events SET user_id = $1
                       WHERE id = (SELECT id FROM login_events
                                   WHERE email = $2 ORDER BY id DESC LIMIT 1)""",
                    str(u["_id"]),
                    email,
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("backfill user_id failed: %s", exc)
    return {"ok": True}


# ---------------------------------------------------------------- AI 管理（IMA）
#
# 站长的诉求：把站内数据（用户数据 + 运维数据）同步进 IMA 知识库，
# 然后提问时走 IMA 检索、由它在 IMA 侧命中后交回答案。
# 凭证通过环境变量给（IMA_CLIENT_ID / IMA_API_KEY），不在界面上存明文。

@router.get("/api/ops/ima/status")
async def ima_status(request: Request):
    await _me(request)
    out: dict[str, Any] = {"configured": ima.configured()}
    if not ima.configured():
        return out
    try:
        kbs = await ima.list_kbs()
        out["knowledge_bases"] = [
            {"kb_id": k.get("kb_id"), "name": k.get("kb_name"), "count": k.get("content_count")}
            for k in kbs
        ]
        # 默认选中的知识库：按 IMA_KB_NAME 精确匹配，匹配不到就取第一个。
        # 前端不再要求用户每次手动挑——他已经建好了 chat.example.com 这一个。
        want = (os.getenv("IMA_KB_NAME") or "").strip().lower()
        pick = ""
        for k in out["knowledge_bases"]:
            if want and (k.get("name") or "").strip().lower() == want:
                pick = k["kb_id"]
                break
        out["default_kb_id"] = pick or (out["knowledge_bases"][0]["kb_id"] if out["knowledge_bases"] else "")
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(exc)[:300]
    try:
        await ima_sync.ensure_schema()
        rows = await db.fetch(
            """SELECT count(*) AS n, coalesce(sum(bytes),0) AS bytes, max(synced_at) AS last
               FROM ima_sync_state WHERE media_id IS NOT NULL"""
        )
        out["synced_docs"] = int(rows[0]["n"])
        out["synced_bytes"] = int(rows[0]["bytes"])
        out["last_sync"] = rows[0]["last"]
    except Exception:  # noqa: BLE001
        pass
    return out


@router.post("/api/ops/ima/sync")
async def ima_sync_now(request: Request):
    """把数据库整理成文档推到 IMA。立刻返回，后台跑。"""
    await _me(request)
    body = await request.json() if await request.body() else {}
    kb_id = str(body.get("kb_id") or "").strip()
    if not kb_id:
        raise HTTPException(400, "先选一个知识库")
    if not ima.configured():
        raise HTTPException(400, "还没配置 IMA 的 ClientID / APIKey")

    only_user = body.get("user_id") or None
    job_id = "ima_" + uuid.uuid4().hex[:10]
    await db.execute(
        """INSERT INTO memory_jobs (id, user_id, kind, status, stage, total, done)
           VALUES ($1,$2,'ima_sync','running','准备中',0,0)""",
        job_id,
        only_user or "__site__",
    )

    async def _run():
        try:
            def prog(i, n, name):
                asyncio.create_task(
                    db.execute(
                        "UPDATE memory_jobs SET stage=$2, done=$3, total=$4, updated_at=now() WHERE id=$1",
                        job_id, f"上传 {name}", i, n,
                    )
                )

            stats = await ima_sync.sync(
                kb_id,
                only_user=only_user,
                force=bool(body.get("force")),
                on_progress=prog,
            )
            await db.execute(
                """UPDATE memory_jobs SET status=$2, stage='完成', stats=$3::jsonb, updated_at=now()
                   WHERE id=$1""",
                job_id,
                "done" if not stats.get("failed") else "failed",
                json.dumps(stats, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ima sync failed: %s", exc)
            await db.execute(
                "UPDATE memory_jobs SET status='failed', error=$2, updated_at=now() WHERE id=$1",
                job_id, str(exc)[:300],
            )

    asyncio.create_task(_run())
    return {"job_id": job_id, "running": True}


@router.get("/api/ops/ima/job/{job_id}")
async def ima_job(job_id: str, request: Request):
    await _me(request)
    row = await db.fetchrow(
        "SELECT id, status, stage, done, total, stats, error FROM memory_jobs WHERE id = $1", job_id
    )
    if not row:
        raise HTTPException(404, "任务不存在")
    out = dict(row)
    if isinstance(out.get("stats"), str):
        try:
            out["stats"] = json.loads(out["stats"])
        except Exception:  # noqa: BLE001
            out["stats"] = {}
    return {"job": out}


@router.post("/api/ops/ima/ask")
async def ima_ask(request: Request):
    """问 IMA。scope=site 问全站，scope=user 问某个人。"""
    await _me(request)
    body = await request.json()
    kb_id = str(body.get("kb_id") or "").strip()
    q = str(body.get("q") or "").strip()
    if not kb_id:
        raise HTTPException(400, "先选一个知识库")
    if len(q) < 2:
        raise HTTPException(400, "问题太短")
    if not ima.configured():
        raise HTTPException(400, "还没配置 IMA 的 ClientID / APIKey")
    try:
        return await ima_sync.ask(kb_id, q)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"IMA 请求失败：{str(exc)[:200]}") from exc


@router.get("/api/ops/ima/docs")
async def ima_docs(request: Request):
    """已同步文档清单，用于看"哪些传上去了、哪些失败了"。"""
    await _me(request)
    await ima_sync.ensure_schema()
    rows = await db.fetch(
        """SELECT doc_key, kb_id, title, media_id, bytes, synced_at, error
           FROM ima_sync_state ORDER BY updated_at DESC LIMIT 120"""
    )
    return {"docs": [dict(r) for r in rows]}


# ---- 问答会话（后台「问 AI」是能连续聊的聊天区，历史要能翻回去）----

@router.get("/api/ops/ima/chats")
async def ima_chats(request: Request):
    await _me(request)
    return {"chats": await ima_sync.list_chats()}


@router.post("/api/ops/ima/chats")
async def ima_chat_new(request: Request):
    await _me(request)
    body = await request.json() if await request.body() else {}
    kb_id = str(body.get("kb_id") or "").strip()
    if not kb_id:
        raise HTTPException(400, "先选一个知识库")
    return {"chat": await ima_sync.create_chat(kb_id, str(body.get("title") or ""))}


@router.get("/api/ops/ima/chats/{chat_id}")
async def ima_chat_get(chat_id: str, request: Request):
    await _me(request)
    chat = await ima_sync.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "会话不存在")
    return {"chat": chat, "messages": await ima_sync.chat_messages(chat_id)}


@router.delete("/api/ops/ima/chats/{chat_id}")
async def ima_chat_delete(chat_id: str, request: Request):
    await _me(request)
    await ima_sync.delete_chat(chat_id)
    return {"ok": True}


@router.post("/api/ops/ima/chats/{chat_id}/ask")
async def ima_chat_ask(chat_id: str, request: Request):
    """在会话里问一句。返回这一轮的回答，历史由前端自己往上叠。"""
    await _me(request)
    body = await request.json() if await request.body() else {}
    q = str(body.get("q") or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "问题太短")
    if not ima.configured():
        raise HTTPException(400, "还没配置 IMA 的 ClientID / APIKey")
    try:
        return await ima_sync.chat_ask(chat_id, q)
    except KeyError as exc:
        raise HTTPException(404, "会话不存在") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"IMA 请求失败：{str(exc)[:200]}") from exc
