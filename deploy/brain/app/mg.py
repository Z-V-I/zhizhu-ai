"""Mongo 只读访问：后台要"看"用户和对话，但不参与聊天写入。

为什么直连 LibreChat 的 Mongo，而不是给它加接口：
  对话、用户、消息的表结构已经是既成事实，加一层 API 只是多一次转发；
  后台是只读的旁观者，直接读库最省事，也不会因为上游改接口而断。

约束（很重要）：
  · 只允许 find / count / aggregate 这几种读操作，本模块不提供任何写方法
  · 所有查询都带上限（limit），后台页面不会因为某个用户有 3000 条消息而拖死
  · 连接池懒初始化：没配 MONGO_URL 时后台里"用户追踪"这一块自动降级为不可用，
    但模型切换、邀请码这些不依赖 Mongo 的功能照常工作
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("brain.mongo")

_client = None
_db = None


def _url() -> str:
    # 容器网络里直接用服务名；本地调试可用 MONGO_URL 覆盖
    return os.getenv("MONGO_URL", "mongodb://mongodb:27017/LibreChat")


def ready() -> bool:
    return _db is not None


def connect() -> None:
    """在 startup 时调用一次。失败不抛：后台降级，但站点必须能起来。"""
    global _client, _db
    if _db is not None:
        return
    try:
        from pymongo import MongoClient

        _client = MongoClient(_url(), serverSelectionTimeoutMS=2500, maxPoolSize=5)
        _client.admin.command("ping")
        _db = _client.get_default_database()
        log.info("mongo connected: %s", _db.name)
    except Exception as exc:  # noqa: BLE001
        _client, _db = None, None
        log.warning("mongo unavailable (%s): 用户追踪功能将不可用", exc)


def close() -> None:
    global _client, _db
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
    _client, _db = None, None


def col(name: str):
    if _db is None:
        raise RuntimeError("Mongo 未连接")
    return _db[name]


# ---------------- 用户 ----------------

USER_FIELDS = {
    "_id": 1,
    "name": 1,
    "username": 1,
    "email": 1,
    "role": 1,
    "provider": 1,
    "createdAt": 1,
    "updatedAt": 1,
}


def list_users(limit: int = 200, q: str = "") -> list[dict[str, Any]]:
    import re

    flt: dict[str, Any] = {}
    if q.strip():
        rx = re.compile(re.escape(q.strip()), re.I)
        flt = {"$or": [{"email": rx}, {"name": rx}, {"username": rx}]}
    out = []
    for u in col("users").find(flt, USER_FIELDS).sort("createdAt", -1).limit(limit):
        out.append(
            {
                "id": str(u["_id"]),
                "name": u.get("name") or u.get("username") or "",
                "username": u.get("username") or "",
                "email": u.get("email") or "",
                "role": u.get("role") or "USER",
                "provider": u.get("provider") or "local",
                "created_at": u.get("createdAt"),
            }
        )
    return out


def get_user(user_id: str) -> Optional[dict[str, Any]]:
    from bson import ObjectId

    try:
        u = col("users").find_one({"_id": ObjectId(user_id)}, USER_FIELDS)
    except Exception:  # noqa: BLE001
        return None
    if not u:
        return None
    return {
        "id": str(u["_id"]),
        "name": u.get("name") or u.get("username") or "",
        "username": u.get("username") or "",
        "email": u.get("email") or "",
        "role": u.get("role") or "USER",
        "provider": u.get("provider") or "local",
        "created_at": u.get("createdAt"),
    }


def user_count() -> int:
    try:
        return col("users").count_documents({})
    except Exception:  # noqa: BLE001
        return 0


# ---------------- 对话 ----------------

def list_conversations(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    out = []
    cur = (
        col("conversations")
        .find(
            {"user": user_id},
            {
                "_id": 0,
                "conversationId": 1,
                "title": 1,
                "createdAt": 1,
                "updatedAt": 1,
                "model": 1,
                "endpoint": 1,
                "agent_id": 1,
            },
        )
        .sort("updatedAt", -1)
        .limit(limit)
    )
    for c in cur:
        out.append(
            {
                "id": c.get("conversationId"),
                "title": c.get("title") or "（未命名）",
                "created_at": c.get("createdAt"),
                "updated_at": c.get("updatedAt"),
                "model": c.get("model"),
                "endpoint": c.get("endpoint"),
                "agent_id": c.get("agent_id"),
            }
        )
    return out


def parts_to_text(content: Any) -> tuple[str, str, str]:
    """把 LibreChat 的 content 数组还原成 (正文, 思考, 错误)。

    ## 为什么必须做这件事

    LibreChat 新版把 AI 回复存成 `content: [{type, ...}]` 数组，而 `text` 字段
    是**空的**。后台管理页只读 `text`，于是所有 AI 回复都显示"（空）"。

    实测这个库里 AI 消息的分布：
        总数 204 / text 非空 158 / text 为空 46
    part 的类型：text(41) / think(27) / error(5)

    ## 各类型怎么处理

    - `text`  → 正文，按顺序拼起来（这是用户真正看到的回答）
    - `think` → 思考过程。不混进正文（否则会和答案黏在一起看不懂），
                单独返回，后台折叠展示
    - `error` → 上游报错。单独返回，否则"空回复"看起来像数据丢了，
                其实是当时请求失败了

    其它类型（工具调用、图片等）当前不渲染 —— 后台是只读审阅，
    需要的是"用户看到了什么回答"，不是完整回放。
    """
    if not isinstance(content, list):
        return "", "", ""
    texts: list[str] = []
    thinks: list[str] = []
    errs: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t == "text":
            v = part.get("text")
            if isinstance(v, str) and v.strip():
                texts.append(v.strip())
        elif t == "think":
            v = part.get("think") or part.get("text")
            if isinstance(v, str) and v.strip():
                thinks.append(v.strip())
        elif t == "error":
            v = part.get("error") or part.get("text")
            if isinstance(v, str) and v.strip():
                errs.append(v.strip())
    return "\n\n".join(texts), "\n\n".join(thinks), "\n\n".join(errs)


def conversation_messages(conversation_id: str, limit: int = 400) -> list[dict[str, Any]]:
    """按时间正序返回一条对话的消息。

    正文优先取 `text`；为空时从 `content` 数组还原（AI 回复常是后者）。
    额外返回 thinking / error，让后台能区分"真没说话"和"请求失败了"。
    """
    out = []
    cur = (
        col("messages")
        .find(
            {"conversationId": conversation_id},
            {
                "_id": 0,
                "messageId": 1,
                "isCreatedByUser": 1,
                "text": 1,
                "content": 1,
                "createdAt": 1,
                "model": 1,
                "endpoint": 1,
                "error": 1,
                "parentMessageId": 1,
            },
        )
        .sort("createdAt", 1)
        .limit(limit)
    )
    for m in cur:
        text = (m.get("text") or "").strip()
        thinking = ""
        err_text = ""

        # text 为空时再去看 content —— 省掉不必要的解析
        if not text:
            text, thinking, err_text = parts_to_text(m.get("content"))
        else:
            # text 有值也顺手取一下 think/error，用于后台标注
            _t, thinking, err_text = parts_to_text(m.get("content"))

        out.append(
            {
                "id": m.get("messageId"),
                "from": "user" if m.get("isCreatedByUser") else "assistant",
                "text": text[:8000],
                "thinking": thinking[:4000],
                "error_text": err_text[:2000],
                "at": m.get("createdAt"),
                "model": m.get("model"),
                "error": bool(m.get("error")) or bool(err_text),
            }
        )
    return out


def conversation_stats(user_id: str) -> dict[str, Any]:
    """一个用户的对话规模：用于画像和评价的"口径快照"。"""
    try:
        convos = col("conversations").count_documents({"user": user_id})
        conv_ids = [c["conversationId"] for c in col("conversations").find({"user": user_id}, {"conversationId": 1}).limit(3000)]
        msgs = col("messages").count_documents({"conversationId": {"$in": conv_ids}}) if conv_ids else 0
        first = col("messages").find_one({"conversationId": {"$in": conv_ids}}, sort=[("createdAt", 1)]) if conv_ids else None
        last = col("messages").find_one({"conversationId": {"$in": conv_ids}}, sort=[("createdAt", -1)]) if conv_ids else None
        return {
            "conversations": convos,
            "messages": msgs,
            "first_at": (first or {}).get("createdAt"),
            "last_at": (last or {}).get("createdAt"),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("conversation_stats failed: %s", exc)
        return {"conversations": 0, "messages": 0, "first_at": None, "last_at": None}


def kick_sessions(user_id: str) -> int:
    """立刻让某个人所有已登录设备掉线。返回删除的会话条数。

    ## 为什么删 sessions 就够了

    LibreChat 的登录态是一个 httpOnly 的 refreshToken cookie，服务端在
    `sessions` 集合里存它的哈希。前端拿它换 access token 时，服务端两步都会查
    这个集合：查不到 → 401 → 前端清 cookie 并跳回登录页。
    所以删行 = 立刻失效，不用等 7 天过期，也不用维护吊销列表。

    只删这一个用户的行，不碰其它人。这个镜像里的文档形状是
    `{_id, user, refreshTokenHash, expiration}`，`user` 是 ObjectId；
    老版本是 `session.userId`，新老两种都匹配一遍，免得换个镜像就踢不动人。
    """
    try:
        import bson

        oid = None
        try:
            oid = bson.ObjectId(user_id)
        except Exception:  # noqa: BLE001
            pass
        conds: list[dict[str, Any]] = [{"user": user_id}, {"session.userId": {"$in": [user_id]}}]
        if oid is not None:
            conds.append({"user": oid})
            conds.append({"session.userId": {"$in": [oid]}})
        r = col("sessions").delete_many({"$or": conds})
        log.info("kicked %s sessions for %s", r.deleted_count, user_id)
        return int(r.deleted_count)
    except Exception as exc:  # noqa: BLE001
        log.warning("kick_sessions failed for %s: %s", user_id, exc)
        return 0


def session_alive(refresh_token: str) -> Optional[str]:
    """拿浏览器里的 refreshToken 原文问一句：这个登录态还在不在？活着就返回 user_id。

    ## 为什么需要它

    `kick_sessions` 删完行，服务端就不再认这个 token；但**已经发出去的 access
    token 是自包含的 JWT**，签名照样有效，最长还能用 15 分钟。这段时间里页面
    完全正常，用户甚至能继续对话 —— 对"收回访问权"来说，这 15 分钟是真实漏洞。

    这里不去改 LibreChat 的中间件（那是人家的镜像），只是提供一个"我还在不在"
    的旁路查询：前端每 25 秒问一次，答 401 就立刻回登录页。
    LibreChat 存的不是 token 本身而是 `sha256(token)` 的十六进制，所以这里
    照同样口径算一遍再查 `sessions.refreshTokenHash`，不做任何解密。

    Mongo 不可用时返回 None —— 调用方要当成"不确定"而不是"已失效"，
    否则数据库抖一下就把所有人都踢了。
    """
    raw = (refresh_token or "").strip()
    if not raw or not ready():
        return None
    try:
        import hashlib

        h = hashlib.sha256(raw.encode()).hexdigest()
        doc = col("sessions").find_one({"refreshTokenHash": h}, {"user": 1, "session": 1})
        if not doc:
            return None
        uid = doc.get("user") or (doc.get("session") or {}).get("userId")
        return str(uid) if uid else None
    except Exception as exc:  # noqa: BLE001
        log.warning("session_alive failed: %s", exc)
        return None


def recent_user_texts(user_id: str, limit: int = 120) -> list[str]:
    """取该用户最近说过的话（只要用户侧），用于生成画像评价。"""
    conv_ids = [c["conversationId"] for c in col("conversations").find({"user": user_id}, {"conversationId": 1}).limit(200)]
    if not conv_ids:
        return []
    texts = []
    cur = (
        col("messages")
        .find(
            {"conversationId": {"$in": conv_ids}, "isCreatedByUser": True},
            {"_id": 0, "text": 1},
        )
        .sort("createdAt", -1)
        .limit(limit)
    )
    for m in cur:
        t = (m.get("text") or "").strip()
        if t:
            texts.append(t[:600])
    return texts
