"""导出：把用户的全部对话（含导入的）读成可移植结构。"""

from __future__ import annotations

import hashlib
from typing import Any

from . import mg

ROOT_PARENT = "00000000-0000-0000-0000-000000000000"


def _content_id(title: str, created: Any, texts: list[str]) -> str:
    """用内容算一个稳定 ID，用于回灌时去重。

    为什么不用 conversationId：那是一次写入时随机生成的 uuid，
    换机器/换站之后它没有任何意义，再导一次只会变成两份。
    用「标题 + 创建时间 + 全部正文」算哈希，同一段对话无论导出导入多少次，
    得到的 ID 都一样 —— 幂等就自动成立了。
    """
    h = hashlib.sha1()
    h.update((title or "").encode("utf-8"))
    h.update(str(created).encode("utf-8"))
    for t in texts:
        h.update(t.encode("utf-8"))
    return "umf1-" + h.hexdigest()[:20]


def export_conversations(
    user_id: str,
    *,
    include_imported: bool = True,
    include_own: bool = True,
    limit: int = 2000,
) -> dict[str, Any]:
    """读取用户的对话，按时间正序返回可移植结构。

    返回的每条：
        {id, title, created, updated, source, platform, tags, messages:[{role,text}]}

    source 区分 own / import，让用户知道哪些是搬来的、哪些是自己聊的。
    """
    if not mg.ready():
        raise RuntimeError("Mongo 未连接")

    q: dict[str, Any] = {"user": user_id}
    conds = []
    if include_imported:
        conds.append({"umfImport": True})
    if include_own:
        conds.append({"umfImport": {"$ne": True}})
    if not conds:
        return {"conversations": [], "conversationsCount": 0, "messagesCount": 0}
    if len(conds) == 1:
        q.update(conds[0])
    else:
        q["$or"] = conds

    cur = (
        mg.col("conversations")
        .find(q, {"conversationId": 1, "title": 1, "createdAt": 1, "updatedAt": 1,
                  "umfImport": 1, "umfImportPlatform": 1, "tags": 1, "messages": 1})
        .sort("createdAt", 1)
        .limit(limit)
    )
    rows = list(cur)
    if not rows:
        return {"conversations": [], "conversationsCount": 0, "messagesCount": 0}

    # 一次把所有消息查出来，避免 N+1（93 个对话 * 1 次查询会很慢）
    all_ids = [r["conversationId"] for r in rows if r.get("conversationId")]
    msgs_by_conv: dict[str, list[dict[str, Any]]] = {}
    mcur = mg.col("messages").find(
        {"conversationId": {"$in": all_ids}},
        # content 必须取：新版 LibreChat 的 AI 回复正文存在 content 数组里，
        # text 是空的。只读 text 会把 AI 回答整段丢掉（实测 46 条）。
        {"conversationId": 1, "messageId": 1, "parentMessageId": 1,
         "isCreatedByUser": 1, "text": 1, "content": 1, "createdAt": 1},
    )
    raw: dict[str, dict[str, Any]] = {}
    for m in mcur:
        cid = m.get("conversationId")
        if not cid:
            continue
        raw.setdefault(cid, {})[m.get("messageId")] = m

    out: list[dict[str, Any]] = []
    total_msgs = 0
    for r in rows:
        cid = r.get("conversationId")
        bucket = raw.get(cid) or {}
        if not bucket:
            continue

        # 沿着 parentMessageId 排成一条链：Mongo 的返回顺序不保证是对话顺序
        ordered = _order_chain(bucket)
        msgs = []
        for m in ordered:
            text = (m.get("text") or "").strip()
            if not text:
                # text 为空时从 content 还原（AI 回复常是这种情况）
                text, _think, _err = mg.parts_to_text(m.get("content"))
                text = (text or "").strip()
            if not text:
                continue
            msgs.append(
                {
                    "role": "user" if m.get("isCreatedByUser") else "assistant",
                    "text": text,
                }
            )
        if not msgs:
            continue

        created = r.get("createdAt")
        title = (r.get("title") or "未命名对话").strip()
        out.append(
            {
                "id": _content_id(title, created, [x["text"] for x in msgs]),
                "title": title,
                "created": created.isoformat() if created else None,
                "updated": r["updatedAt"].isoformat() if r.get("updatedAt") else None,
                "source": "import" if r.get("umfImport") else "own",
                "platform": r.get("umfImportPlatform") or None,
                "tags": r.get("tags") or [],
                "messages": msgs,
            }
        )
        total_msgs += len(msgs)

    return {
        "conversations": out,
        "conversationsCount": len(out),
        "messagesCount": total_msgs,
    }


def _order_chain(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """按 parentMessageId 把消息排成对话顺序。

    为什么不能直接按 createdAt 排：同一秒内写入的多条消息时间戳可能完全相同，
    排序不稳定，导出后回灌就乱序了。按父子链走是唯一可靠的办法。

    兜底：链断了（父不存在）就用时间戳补，宁可顺序略糙也不能丢消息。
    """
    by_parent: dict[str, list[dict[str, Any]]] = {}
    ids = set(bucket.keys())
    for mid, m in bucket.items():
        p = m.get("parentMessageId") or ROOT_PARENT
        # 父不在本对话里 → 当成根
        if p not in ids:
            p = ROOT_PARENT
        by_parent.setdefault(p, []).append(m)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(pid: str) -> None:
        for m in sorted(by_parent.get(pid, []), key=lambda x: str(x.get("createdAt") or "")):
            mid = m.get("messageId")
            if mid in seen:
                continue
            seen.add(mid or "")
            out.append(m)
            if mid:
                walk(mid)

    walk(ROOT_PARENT)

    # 断链兜底：把没走到的按时间追加
    if len(out) < len(bucket):
        rest = [m for mid, m in bucket.items() if mid not in seen]
        rest.sort(key=lambda x: str(x.get("createdAt") or ""))
        out.extend(rest)
    return out
