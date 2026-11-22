"""UMF 记忆存储。这里是唯一真相源；Mem0 只是它上面的一层检索索引。

所有函数都强制带 user_id —— 记忆串号是这个产品唯一"致命"的事故类型，
宁可在签名上啰嗦，也不靠调用方自觉。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from . import db, llm

log = logging.getLogger("brain.store")

# 五层记忆模型（沿用 LobeHub v2 的分法，理由见 docs/05）：
# 分层不是分类癖，它决定了注入顺序和生命周期：
#   identity/preference 短、稳、必带；activity 有时效，要配 valid_to；
#   context 只活在单个话题里，永远不进 global。
LAYERS = {"identity", "preference", "experience", "activity", "context"}

# 旧数据的 kind 兜底映射，保证抽取器没给 layer 时也能落到合理层
KIND_TO_LAYER = {
    "preference": "preference",
    "fact": "identity",
    "decision": "activity",
    "relation": "identity",
    "event": "activity",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _vec(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


# ------------------------------------------------------------------ 画像

async def get_profile(user_id: str) -> dict[str, Any]:
    row = await db.fetchrow("SELECT data, version FROM umf_profiles WHERE user_id = $1", user_id)
    if not row:
        return {}
    data = row["data"]
    return data if isinstance(data, dict) else json.loads(data)


async def set_profile(user_id: str, data: dict[str, Any]) -> int:
    row = await db.fetchrow(
        """INSERT INTO umf_profiles (user_id, data, version, updated_at)
           VALUES ($1, $2::jsonb, 1, now())
           ON CONFLICT (user_id) DO UPDATE
             SET data = EXCLUDED.data,
                 version = umf_profiles.version + 1,
                 updated_at = now()
           RETURNING version""",
        user_id,
        json.dumps(data, ensure_ascii=False),
    )
    await log_event(user_id, "profile_updated", user_id, "画像更新")
    return int(row["version"])


# ------------------------------------------------------------------ 记忆

async def add_memory(
    user_id: str,
    text: str,
    *,
    scope: str = "global",
    agent_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    kind: str = "fact",
    layer: Optional[str] = None,
    entities: Optional[list[str]] = None,
    confidence: float = 0.8,
    valid_from: Optional[date] = None,
    source: Optional[dict[str, Any]] = None,
    import_batch: Optional[str] = None,
    embed: bool = True,
    vector_json: Optional[str] = None,
) -> str:
    """写一条记忆。返回 id（和已有条目合并时返回的是被合并那条的 id）。
    被用户否认过的内容不会写入，返回空字符串。

    `vector_json` 是给批处理用的：一次 embed 二十条，比一条一次快两个数量级。
    传了就跳过单条向量化，不传才现算。
    """
    memory_id = f"m_{uuid.uuid4().hex[:16]}"
    layer = layer if layer in LAYERS else KIND_TO_LAYER.get(kind, "context")
    vector: Optional[str] = vector_json
    if embed and not vector:
        try:
            vector = _vec((await llm.embed([text]))[0])
        except Exception as exc:
            log.warning("embed failed, memory saved without vector: %s", exc)

    # 否决检查放在算完向量之后：可以复用同一个向量，不必再 embed 一次。
    # （整理 277 条时，每条多一次 embed 就是多二十几秒。）
    # manual 例外见上：本人刚手打的，尊重他加回来的意愿。
    if (source or {}).get("platform") != "manual":
        try:
            if await is_rejected(user_id, text, vector=vector):
                log.info("skip rejected memory for %s: %s", user_id, text[:60])
                return ""
        except Exception as exc:  # noqa: BLE001
            log.warning("is_rejected check failed, allowing write: %s", exc)

    # 近似去重：同一层里已经有 92% 像的一条，就更新它而不是再插一条。
    # 没有这一步，聊三天就会长出一堆「用户在做一个聊天站」的变体——
    # 记忆越多、召回越差，这是长期使用最容易烂掉的地方。
    if vector:
        dup = await db.fetchrow(
            """SELECT id, text, confidence, 1 - (embedding <=> $2::vector) AS sim
               FROM memories
               WHERE user_id=$1 AND deleted_at IS NULL AND embedding IS NOT NULL AND layer=$3
               ORDER BY embedding <=> $2::vector LIMIT 1""",
            user_id,
            vector,
            layer,
        )
        if dup and float(dup["sim"]) >= 0.92:
            await db.execute(
                """UPDATE memories
                   SET text=$2, embedding=$3::vector, confidence=GREATEST(confidence,$4),
                       created_at=now(), source=$5::jsonb
                   WHERE id=$1""",
                dup["id"],
                text,
                vector,
                confidence,
                json.dumps(source or {}, ensure_ascii=False),
            )
            await log_event(user_id, "memory_merged", dup["id"], text[:120])
            return dup["id"]

    await db.execute(
        """INSERT INTO memories
             (id, user_id, scope, agent_id, topic_id, kind, layer, text, entities,
              embedding, valid_from, source, confidence, import_batch)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::vector,$11,$12::jsonb,$13,$14)""",
        memory_id,
        user_id,
        scope,
        agent_id,
        topic_id,
        kind,
        layer,
        text,
        json.dumps(entities or [], ensure_ascii=False),
        vector,
        valid_from,
        json.dumps(source or {}, ensure_ascii=False),
        confidence,
        import_batch,
    )
    await log_event(
        user_id,
        "preference_change" if kind == "preference" else "memory_added",
        memory_id,
        text[:120],
    )
    return memory_id


async def list_memories(
    user_id: str,
    *,
    scope: Optional[str] = None,
    agent_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT id, scope, agent_id, topic_id, kind, layer, text, entities, valid_from, valid_to,",
        "       confidence, created_at FROM memories",
        "WHERE user_id = $1 AND deleted_at IS NULL",
    ]
    args: list[Any] = [user_id]
    if scope:
        args.append(scope)
        sql.append(f"AND scope = ${len(args)}")
    if agent_id:
        args.append(agent_id)
        sql.append(f"AND agent_id = ${len(args)}")
    if q:
        args.append(f"%{q}%")
        sql.append(f"AND text ILIKE ${len(args)}")
    args.append(limit)
    sql.append(f"ORDER BY created_at DESC LIMIT ${len(args)}")

    rows = await db.fetch(" ".join(sql), *args)
    return [dict(r) for r in rows]


async def delete_memory(user_id: str, memory_id: str, *, note: str = "") -> bool:
    """软删一条记忆，并把它记进更正表。

    ## 为什么删除必须留痕

    只把 `deleted_at` 填上，这条记忆在下次整理时就会复活 ——
    它本来就是从某段对话里抽出来的，那段对话还在，抽取会再抽一次。
    用户会看到"我明明删了三次，它又回来了"，然后彻底不信这个功能。

    记进 corrections 之后有两道闸：prompt 里出现"本人否认"，写入前
    还会有 `is_rejected` 的相似度拦截。所以这里必须拿到原文再删。
    """
    old = await db.fetchrow(
        "SELECT text FROM memories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        memory_id,
        user_id,
    )
    result = await db.execute(
        "UPDATE memories SET deleted_at = now() WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        memory_id,
        user_id,
    )
    ok = result.endswith("1")
    if ok:
        await log_event(user_id, "memory_deleted", memory_id, "手动删除")
        if old:
            # 更正记录失败不能让整个删除报错。
            # 踩过一次：log_correction 因为向量维度不对抛异常，删除本身已经写进库了，
            # 接口却返回 500 —— 用户重试会看到"not found"，以为没删掉。
            # 主操作（删除）和附加操作（留痕）的失败必须分开。
            try:
                await log_correction(
                    user_id,
                    kind="memory_delete",
                    target="memory",
                    target_id=memory_id,
                    before_text=old["text"] or "",
                    note=note,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("correction log failed after delete (item still deleted): %s", exc)
    return ok


async def search_memories(
    user_id: str,
    query: str,
    *,
    agent_id: Optional[str] = None,
    k: int = 8,
    min_score: float = 0.35,
) -> list[dict[str, Any]]:
    """检索。优先向量，没向量就退回全文，再不行就返回最近的几条。

    作用域规则：global 永远可读；agent 记忆只有同一个 agent 能读。
    """
    if not query.strip():
        return []

    try:
        vector = _vec((await llm.embed([query]))[0])
        rows = await db.fetch(
            """SELECT id, scope, agent_id, kind, layer, text, entities, confidence, created_at,
                      1 - (embedding <=> $1::vector) AS score
               FROM memories
               WHERE user_id = $2 AND deleted_at IS NULL AND embedding IS NOT NULL
                 AND (scope = 'global' OR (scope = 'agent' AND agent_id = $3))
               ORDER BY embedding <=> $1::vector
               LIMIT $4""",
            vector,
            user_id,
            agent_id,
            k,
        )
        # 绝对阈值会把"最好的那条"挡在外面。
        #
        # 中文短问句跟记忆陈述句的余弦相似度整体偏低：问"我该不该继续喜欢她"，
        # 库里那条"暗恋一位长发淑女，对方有男友且自认不够优秀"只有 0.325，
        # 而固定阈值 0.35 会把它判成不相关 —— 恰恰是该命中的那条被丢掉，
        # 然后模型只好泛泛而谈。所以再加一条相对线：只要它跟"本轮最高分"
        # 差不多，就算命中。噪声不会被带进来，因为下面还有 0.24 的地板：
        # 全场都很低时（比如 0.2 以下），相对线也跟着降到底，但地板拦住它。
        best = max((float(r["score"] or 0) for r in rows), default=0.0)
        cutoff = min(min_score, max(0.24, best * 0.92))
        hits = [dict(r) for r in rows if (r["score"] or 0) >= cutoff]
        if hits:
            return hits
    except Exception as exc:
        log.warning("vector search failed, fallback to text: %s", exc)

    rows = await db.fetch(
        """SELECT id, scope, agent_id, kind, layer, text, entities, confidence, created_at, 0.0 AS score
           FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL
             AND (scope = 'global' OR (scope = 'agent' AND agent_id = $2))
             AND text ILIKE $3
           ORDER BY created_at DESC LIMIT $4""",
        user_id,
        agent_id,
        f"%{query[:40]}%",
        k,
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 必带层

async def get_core_memories(
    user_id: str,
    *,
    agent_id: Optional[str] = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """每轮无条件注入的两层：identity + preference。

    它们短、稳、直接决定"像不像懂你"，不该依赖语义检索能不能命中 ——
    用户说"今天好累"时，向量检索命不中"他在创业"，但那句话恰恰最需要这个背景。
    所以先取必带层，再用检索补齐，两条路都不落空。
    """
    rows = await db.fetch(
        """SELECT id, scope, agent_id, kind, layer, text, entities, confidence, created_at,
                0.0 AS score
           FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL
             AND layer IN ('identity','preference')
             AND (valid_to IS NULL OR valid_to >= CURRENT_DATE)
             AND (scope = 'global' OR (scope = 'agent' AND agent_id = $2))
           ORDER BY confidence DESC, created_at DESC
           LIMIT $3""",
        user_id,
        agent_id,
        limit,
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 摘要

async def get_summary(conversation_id: str) -> tuple[str, int]:
    row = await db.fetchrow(
        "SELECT summary, covered_messages FROM conversation_summaries WHERE conversation_id = $1",
        conversation_id,
    )
    if not row:
        return "", 0
    return row["summary"] or "", int(row["covered_messages"] or 0)


async def set_summary(
    conversation_id: str,
    user_id: str,
    summary: str,
    covered_messages: int,
    covered_ids: Optional[list[str]] = None,
    *,
    model: str = "",
    tokens_before: int = 0,
) -> None:
    from .compress import estimate_tokens

    # covered_ids 是"压缩可见"的唯一证据：网关层压缩对前端透明，
    # 只有记下被压掉的是哪几条，记忆中心才能展开还原上下文。
    await db.execute(
        """INSERT INTO conversation_summaries
             (conversation_id, user_id, covered_messages, covered_message_ids,
              summary, model, tokens_before, tokens, updated_at)
           VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,now())
           ON CONFLICT (conversation_id) DO UPDATE
             SET summary = EXCLUDED.summary,
                 covered_messages = EXCLUDED.covered_messages,
                 covered_message_ids = EXCLUDED.covered_message_ids,
                 model = EXCLUDED.model,
                 tokens_before = EXCLUDED.tokens_before,
                 tokens = EXCLUDED.tokens,
                 updated_at = now()""",
        conversation_id,
        user_id,
        covered_messages,
        json.dumps(covered_ids or [], ensure_ascii=False),
        summary,
        model,
        tokens_before,
        estimate_tokens(summary),
    )


# ------------------------------------------------------------------ 时间线

async def log_event(user_id: str, event_type: str, ref_id: Optional[str], summary: str) -> None:
    try:
        await db.execute(
            "INSERT INTO memory_events (user_id, type, ref_id, summary) VALUES ($1,$2,$3,$4)",
            user_id,
            event_type,
            ref_id,
            summary,
        )
    except Exception as exc:
        log.warning("log_event failed: %s", exc)


async def timeline(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT at, type, ref_id, summary FROM memory_events WHERE user_id = $1 ORDER BY at DESC LIMIT $2",
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def topic_of_conversation(conversation_id: str) -> Optional[dict[str, Any]]:
    """这条对话属于哪个话题。没有就返回 None（没整理过历史对话时全都是 None）。

    读的是 conv_digests.topic_id —— 话题归属只有批处理会写，
    在线聊天永远不写，所以这里不需要按 user 过滤（conversation_id 本身就是全局唯一）。
    """
    row = await db.fetchrow(
        """SELECT t.id, t.title, t.summary
           FROM conv_digests d JOIN topics t ON t.id = d.topic_id
           WHERE d.conversation_id = $1
           LIMIT 1""",
        conversation_id,
    )
    return dict(row) if row else None


# ------------------------------------------------- 会话状态（模型切换检测）

async def get_state(conversation_id: str) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT conversation_id, user_id, model, prev_model, switch_count FROM conversation_state WHERE conversation_id=$1",
        conversation_id,
    )
    return dict(row) if row else {}


async def get_pref(user_id: str, key: str, default: Optional[str] = None) -> Optional[str]:
    row = await db.fetchrow("SELECT value FROM umf_prefs WHERE user_id=$1 AND key=$2", user_id, key)
    return row["value"] if row else default


async def set_pref(user_id: str, key: str, value: str) -> None:
    await db.execute(
        """INSERT INTO umf_prefs (user_id, key, value, updated_at)
             VALUES ($1,$2,$3,now())
           ON CONFLICT (user_id, key) DO UPDATE
             SET value = EXCLUDED.value, updated_at = now()""",
        user_id,
        key,
        value,
    )


async def touch_state(conversation_id: str, user_id: str, model: str) -> Optional[str]:
    """记录本会话当前用哪个厂商，返回「上一个模型」（没切换则 None）。

    这是"聊天中途换模型"唯一的判断依据：LibreChat 每次请求都会带完整消息，
    但不会告诉你"用户刚换了模型"。只能自己记。
    """
    if not conversation_id or not model:
        return None
    row = await db.fetchrow(
        "SELECT model FROM conversation_state WHERE conversation_id=$1", conversation_id
    )
    prev = row["model"] if row and row["model"] and row["model"] != model else None
    if row and prev:
        await db.execute(
            """UPDATE conversation_state
                 SET model=$2, prev_model=$3, switch_count=switch_count+1, updated_at=now()
               WHERE conversation_id=$1""",
            conversation_id,
            model,
            prev,
        )
    else:
        await db.execute(
            """INSERT INTO conversation_state (conversation_id, user_id, model, updated_at)
               VALUES ($1,$2,$3,now())
               ON CONFLICT (conversation_id) DO UPDATE
                 SET model=EXCLUDED.model, user_id=EXCLUDED.user_id, updated_at=now()""",
            conversation_id,
            user_id,
            model,
        )
    return prev


# ------------------------------------------------------------------ 更正
#
# 这三件事（编辑/删除/否决）在设计上是一件事：用户在告诉系统"这条不对"。
# 所以统一走 corrections 表，再由它派生出三种效果 —— 审计、注入、防复活。
# 分开写会让"删了但下次整理又冒出来"这种问题反复出现。

CORRECTION_KINDS = {
    "memory_edit", "memory_delete", "topic_edit", "topic_delete",
    "digest_edit", "profile_edit",
}


async def log_correction(
    user_id: str,
    *,
    kind: str,
    target: str,
    target_id: Optional[str] = None,
    before_text: str = "",
    after_text: str = "",
    note: str = "",
    embed: bool = True,
) -> int:
    """记一条更正。返回自增 id。

    会给 before_text 算向量：防复活时要拿新抽出来的记忆跟它比相似度，
    现场算每一条否决都要一次 embed 太慢，落库时算一次就够。
    """
    vector: Optional[str] = None
    text_for_vec = (before_text or after_text or "").strip()
    if embed and text_for_vec:
        try:
            vector = _vec((await llm.embed([text_for_vec]))[0])
        except Exception as exc:  # noqa: BLE001
            log.warning("correction embed failed: %s", exc)

    row = await db.fetchrow(
        """INSERT INTO memory_corrections
             (user_id, kind, target, target_id, before_text, after_text, note, embedding)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector)
           RETURNING id""",
        user_id,
        kind,
        target,
        target_id,
        (before_text or "")[:2000],
        (after_text or "")[:2000],
        (note or "")[:400],
        vector,
    )
    await log_event(user_id, kind, target_id or target, (after_text or before_text or kind)[:120])
    return int(row["id"])


async def safe_log_correction(user_id: str, **kwargs: Any) -> bool:
    """记更正，但绝不因为"留痕失败"把主操作变成报错。

    留痕是附加动作：改/删已经写进库了，这时若抛异常，前端看到 500，
    用户会以为没生效并重试 —— 重试只会得到 404，体验更糟。
    失败时打 warning（能查日志），返回 False。
    """
    try:
        await log_correction(user_id, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("log_correction failed (main op already applied): %s", exc)
        return False


async def backfill_correction_vectors(user_id: str) -> int:
    """给缺向量的更正记录补算向量。返回补了几条。

    ## 为什么需要这个

    向量列会"凭空丢掉"：换 embedding 模型要 `ALTER COLUMN ... TYPE`，
    维度一变就得 `USING NULL`，历史行的向量全变 NULL，而记录还在。
    那些记录的防复活能力会静默降级成"只能原样匹配"——
    换个说法就能绕过，但界面上完全正常，没人会发现。

    所以每次整理记忆前都补一次。代价可控：更正记录一般只有几十条，
    而且补过之后 `embedding IS NULL` 立刻为空，第二次是零成本查询。
    """
    rows = await db.fetch(
        """SELECT id, coalesce(nullif(before_text,''), after_text) AS txt
           FROM memory_corrections
           WHERE user_id = $1 AND embedding IS NULL AND coalesce(before_text, after_text) <> ''
           ORDER BY id DESC LIMIT 80""",
        user_id,
    )
    n = 0
    for r in rows:
        txt = (r["txt"] or "").strip()
        if not txt:
            continue
        try:
            vec = _vec((await llm.embed([txt]))[0])
            await db.execute(
                "UPDATE memory_corrections SET embedding = $2::vector WHERE id = $1",
                r["id"],
                vec,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill correction vector failed id=%s: %s", r["id"], exc)
    if n:
        log.info("backfilled %d correction vectors for %s", n, user_id)
    return n


async def list_corrections(user_id: str, *, limit: int = 60) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """SELECT id, kind, target, target_id, before_text, after_text, note, at
           FROM memory_corrections WHERE user_id = $1 ORDER BY at DESC LIMIT $2""",
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def rejection_list(user_id: str, *, limit: int = 40) -> list[str]:
    """本人明确否认过的句子。给 prompt 用（"这些不是他，别再写进去"）。

    只取删除和修改前的原文 —— 修改后的新文本是他认可的，不该进否决清单。
    """
    rows = await db.fetch(
        """SELECT before_text FROM memory_corrections
           WHERE user_id = $1 AND kind IN ('memory_delete','memory_edit')
             AND before_text <> ''
           ORDER BY at DESC LIMIT $2""",
        user_id,
        limit,
    )
    return [r["before_text"] for r in rows]


async def is_rejected(
    user_id: str, text: str, *, threshold: float = 0.60, vector: Optional[str] = None
) -> bool:
    """这条新记忆是不是用户否决过的？

    用途：整理时从原始对话重新抽取，会把用户刚删掉的错误信息再抽一次
    （对话里确实讲过那句话）。只删库不拦抽取，等于下次整理就复活。

    阈值 0.60 是实测校准出来的，不是拍的。用真实否决记录测过一组：

        原样复述                      1.000  ← 拦
        "…的创始人，毕业于…"（换说法）  0.954  ← 拦
        同一条的另一半                0.923  ← 拦
        "他是XX创始人，主攻空间计算"    0.750  ← 拦
        "用户是郑州轻工业大学的学生"     0.625  ← 拦（同属被否认的身份）
        "用户是高中生，INTJ-A-C"       0.401  不拦
        "用户偏好极简设计风格"          0.318  不拦
        "用户穿索康尼 43 码跑鞋"        0.219  不拦

    0.86 太高（换说法就漏），0.40 太低（无关偏好会被误杀）。
    宁可错杀一条近似的，也别让本人已经否认过的内容再冒出来 ——
    后者对信任的伤害大得多。向量缺失时退回子串包含，仍能挡住"原样复活"。

    性能：先数一下有没有否决记录。绝大多数用户一条都没有，
    那就直接返回，不必为每条记忆多付一次 embed。
    """
    text = (text or "").strip()
    if not text:
        return False
    try:
        n = await db.fetchrow(
            """SELECT count(*) AS n FROM memory_corrections
               WHERE user_id = $1 AND kind IN ('memory_delete','memory_edit')
                 AND before_text <> ''""",
            user_id,
        )
        if not n or int(n["n"]) == 0:
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("is_rejected count failed: %s", exc)
        return False

    # 兜底：还没向量化的更正记录（刚改过维度、或当时 embed 失败过）先补上，
    # 否则下面这一轮比对只能靠文本匹配，换个说法就漏过去了。
    try:
        if not vector:
            miss = await db.fetchrow(
                """SELECT count(*) AS n FROM memory_corrections
                   WHERE user_id = $1 AND kind IN ('memory_delete','memory_edit')
                     AND embedding IS NULL AND before_text <> ''""",
                user_id,
            )
            if miss and int(miss["n"]) > 0:
                await backfill_correction_vectors(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("correction vector backfill skipped: %s", exc)

    try:
        if not vector:
            vector = _vec((await llm.embed([text]))[0])
        row = await db.fetchrow(
            """SELECT before_text, 1 - (embedding <=> $2::vector) AS sim
               FROM memory_corrections
               WHERE user_id = $1 AND kind IN ('memory_delete','memory_edit')
                 AND before_text <> '' AND embedding IS NOT NULL
               ORDER BY embedding <=> $2::vector LIMIT 1""",
            user_id,
            vector,
        )
        if row and float(row["sim"] or 0) >= threshold:
            log.info("memory blocked by correction (sim=%.3f): %s", float(row["sim"]), text[:60])
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("is_rejected vector check failed: %s", exc)

    try:
        row = await db.fetchrow(
            """SELECT before_text FROM memory_corrections
               WHERE user_id = $1 AND kind IN ('memory_delete','memory_edit')
                 AND (before_text = $2 OR position(before_text in $2) > 0)
               LIMIT 1""",
            user_id,
            text,
        )
        if row:
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("is_rejected text check failed: %s", exc)
    return False


# ------------------------------------------------------------------ 可编辑对象
#
# 四个板块各有一套"改一行"的需求。分开写四份 SQL 不难，难的是四份都要记得
# 顺手记一条更正 —— 漏一个就等于那个板块的纠正不进 prompt，下次整理改回去。
# 所以下面几个函数都统一：改完必记更正。

async def update_memory(
    user_id: str, memory_id: str, *, text: Optional[str] = None,
    layer: Optional[str] = None, note: str = "",
) -> Optional[dict[str, Any]]:
    old = await db.fetchrow(
        """SELECT id, layer, text FROM memories
           WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL""",
        memory_id,
        user_id,
    )
    if not old:
        return None

    new_text = (text if text is not None else old["text"]) or ""
    new_text = new_text.strip()
    if not new_text:
        raise ValueError("内容不能为空")
    new_layer = layer if layer in LAYERS else old["layer"]

    # 改文本必须重算向量，否则召回还按老内容走 —— 改了等于没改。
    vector_sql = ""
    args: list[Any] = [memory_id, user_id, new_text, new_layer]
    if new_text != old["text"]:
        try:
            vec = _vec((await llm.embed([new_text]))[0])
            args.append(vec)
            vector_sql = f", embedding = ${len(args)}::vector"
        except Exception as exc:  # noqa: BLE001
            log.warning("re-embed on edit failed: %s", exc)

    await db.execute(
        f"""UPDATE memories SET text = $3, layer = $4{vector_sql}
            WHERE id = $1 AND user_id = $2""",
        *args,
    )
    if new_text != old["text"]:
        await safe_log_correction(
            user_id,
            kind="memory_edit",
            target="memory",
            target_id=memory_id,
            before_text=old["text"],
            after_text=new_text,
            note=note,
        )
    return {"id": memory_id, "text": new_text, "layer": new_layer}


async def update_topic(
    user_id: str, topic_id: str, *, title: Optional[str] = None,
    summary: Optional[str] = None, note: str = "",
) -> Optional[dict[str, Any]]:
    old = await db.fetchrow(
        "SELECT id, title, summary FROM topics WHERE id = $1 AND user_id = $2",
        topic_id,
        user_id,
    )
    if not old:
        return None
    new_title = (title if title is not None else old["title"]) or ""
    new_summary = (summary if summary is not None else old["summary"]) or ""
    await db.execute(
        "UPDATE topics SET title = $3, summary = $4 WHERE id = $1 AND user_id = $2",
        topic_id,
        user_id,
        new_title.strip()[:80],
        new_summary.strip()[:2000],
    )
    if new_title != (old["title"] or "") or new_summary != (old["summary"] or ""):
        await safe_log_correction(
            user_id,
            kind="topic_edit",
            target="topic",
            target_id=topic_id,
            before_text=f"{old['title'] or ''}｜{old['summary'] or ''}"[:1200],
            after_text=f"{new_title}｜{new_summary}"[:1200],
            note=note,
        )
    return {"id": topic_id, "title": new_title, "summary": new_summary}


async def delete_topic(user_id: str, topic_id: str, *, note: str = "") -> bool:
    old = await db.fetchrow(
        "SELECT id, title, summary FROM topics WHERE id = $1 AND user_id = $2",
        topic_id,
        user_id,
    )
    if not old:
        return False
    # 话题删掉后，挂在它下面的会话要断开，否则时间线里会指向一个不存在的话题
    await db.execute("UPDATE conv_digests SET topic_id = NULL WHERE user_id = $1 AND topic_id = $2",
                     user_id, topic_id)
    await db.execute("DELETE FROM topics WHERE id = $1 AND user_id = $2", topic_id, user_id)
    await safe_log_correction(
        user_id,
        kind="topic_delete",
        target="topic",
        target_id=topic_id,
        before_text=f"{old['title'] or ''}｜{old['summary'] or ''}"[:1200],
        note=note,
    )
    return True


async def update_digest(
    user_id: str, conversation_id: str, *, title: Optional[str] = None,
    one_line: Optional[str] = None, note: str = "",
) -> Optional[dict[str, Any]]:
    """时间线的最小单位是 conv_digests 的一天一条，改的就是它的一句话摘要。"""
    old = await db.fetchrow(
        """SELECT conversation_id, title, one_line FROM conv_digests
           WHERE conversation_id = $1 AND user_id = $2""",
        conversation_id,
        user_id,
    )
    if not old:
        return None
    new_title = (title if title is not None else old["title"]) or ""
    new_one = (one_line if one_line is not None else old["one_line"]) or ""
    await db.execute(
        """UPDATE conv_digests SET title = $3, one_line = $4
           WHERE conversation_id = $1 AND user_id = $2""",
        conversation_id,
        user_id,
        new_title.strip()[:200],
        new_one.strip()[:400],
    )
    if new_title != (old["title"] or "") or new_one != (old["one_line"] or ""):
        await safe_log_correction(
            user_id,
            kind="digest_edit",
            target="digest",
            target_id=conversation_id,
            before_text=f"{old['title'] or ''}｜{old['one_line'] or ''}"[:1200],
            after_text=f"{new_title}｜{new_one}"[:1200],
            note=note,
        )
    return {"conversation_id": conversation_id, "title": new_title, "one_line": new_one}


async def update_profile_section(
    user_id: str, *, section: str, items: Optional[list[dict[str, str]]] = None,
    headline: Optional[str] = None, note: str = "",
) -> dict[str, Any]:
    """改画像的某一组（basics/traits/interests/values/ongoing）或 headline。

    整组替换而不是逐条 patch：前端就是一张表，用户改完点保存，语义更清楚；
    逐条 patch 还要维护 item id，而画像条目本来就没有稳定的 id。
    """
    prof = await get_profile(user_id)
    before = json.dumps(
        {k: prof.get(k) for k in ("headline", section) if prof.get(k) is not None},
        ensure_ascii=False,
    )
    if headline is not None:
        prof["headline"] = str(headline).strip()[:120]
    if items is not None:
        clean = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            k = str(it.get("k") or "").strip()[:16]
            v = str(it.get("v") or "").strip()[:200]
            if k and v:
                clean.append({"k": k, "v": v})
        prof[section] = clean[:8]
    prof["edited_by_user"] = True
    prof["edited_at"] = _now().isoformat()
    await set_profile(user_id, prof)
    after = json.dumps(
        {k: prof.get(k) for k in ("headline", section) if prof.get(k) is not None},
        ensure_ascii=False,
    )
    if before != after:
        await safe_log_correction(
            user_id,
            kind="profile_edit",
            target="profile",
            target_id=section,
            before_text=before[:1200],
            after_text=after[:1200],
            note=note,
        )
    return prof


async def correction_digest(user_id: str, *, limit: int = 12) -> str:
    """把最近的更正压成一段给 prompt 用的文字。

    只取 after_text（他认可的正确说法）+ note，不取 before_text —
    把错误内容再喂回模型，反而会增加它复述错误的概率。
    删除类没有 after_text，就明确说"这类信息属于他人、不要写进他的画像"。
    """
    rows = await db.fetch(
        """SELECT kind, before_text, after_text, note, at FROM memory_corrections
           WHERE user_id = $1 ORDER BY at DESC LIMIT $2""",
        user_id,
        limit,
    )
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows:
        if r["kind"] in ("memory_delete", "topic_delete"):
            lines.append(f"- 已删除（本人否认，勿再写入）：{(r['before_text'] or '')[:120]}")
        else:
            after = (r["after_text"] or "").replace("\n", " ")[:140]
            note = (r["note"] or "").strip()
            lines.append(f"- 已更正为：{after}" + (f"（本人说明：{note[:60]}）" if note else ""))
    return "\n".join(lines)
