"""上下文组装：把"人格 + 记忆 + 摘要 + 最近对话"拼成一次请求。

这里有两个决定产品观感的东西，和一个决定成本的东西：
  · 记忆块写成第三人称短句，模型才不会照着旁白念出来
  · 规则放在记忆块之后（最后读到的内容权重最高）
  · 总预算硬限制，超了就砍检索条数，绝不砍画像卡
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import mem0_engine, store
from .compress import compress_long_text, estimate_tokens, messages_tokens, summarize, text_of
from .config import settings

log = logging.getLogger("brain.context")

MEMORY_RULES = """关于 <memory> 里的内容，遵守这几条：
- 它是你的背景知识，不是台词。禁止说"根据我的记忆""我记得你之前说过""作为 AI 我记得"。
- 用户换话题就直接接新话题，不要总结上文，不要问"我们刚才聊到哪了"。
- 只有当它真的能改变你的判断时才用；无关的一律不提。
- 不确定的不要断言，用"你之前好像提过…"这种自然的口气。
- 不要复述这段话本身，也不要把它当成人设介绍。"""

KIND_LABEL = {
    "fact": "事实",
    "preference": "偏好",
    "decision": "决定",
    "relation": "关系",
    "event": "经历",
}

# 标签按 layer 来，比按 kind 更像"人记得的事"。
# 模型看到"[他在做] 正在写一个 AI 聊天平台"比"[事实] …"更容易自然使用。
LAYER_LABEL = {
    "identity": "他是谁",
    "preference": "偏好",
    "experience": "经验",
    "activity": "在做",
    "context": "背景",
}


def _section(title: str, items: list[str]) -> str:
    if not items:
        return ""
    body = "\n".join(f"- {i}" for i in items)
    return f"【{title}】\n{body}"


def _flatten(value: Any) -> list[str]:
    """画像里的条目允许多种写法：{"k","v"} / "文本" / {"v": ...}，统一成一行。"""
    out: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            key = item.get("k") or item.get("key") or ""
            val = item.get("v") or item.get("value") or ""
            if key and val:
                out.append(f"{key}：{val}")
            elif val:
                out.append(str(val))
    return out


def render_memory_block(
    profile: dict[str, Any],
    memories: list[dict[str, Any]],
    summary: str = "",
    topic: str = "",
    switch_from: str = "",
) -> str:
    parts: list[str] = []

    about = (
        _flatten(profile.get("basics"))
        + _flatten(profile.get("traits"))
        + _flatten(profile.get("values"))
    )
    if about:
        parts.append("【关于他】" + "；".join(about))

    interests = _flatten(profile.get("interests"))
    if interests:
        parts.append("【在意的东西】" + "；".join(interests))

    ongoing = _flatten(profile.get("ongoing"))
    if ongoing:
        parts.append("【正在做】" + "；".join(ongoing))

    lines = []
    for m in memories:
        label = LAYER_LABEL.get(m.get("layer") or "") or KIND_LABEL.get(m.get("kind", "fact"), "事实")
        lines.append(f"- [{label}] {m.get('text', '').strip()}")
    if lines:
        parts.append("【记得的事】\n" + "\n".join(lines))

    if summary:
        parts.append("【更早的对话】\n" + summary)
    if topic:
        parts.append("【这条线】\n" + topic)

    if not parts:
        return "" if not switch_from else _handoff(switch_from)
    block = "<memory>\n" + "\n\n".join(parts) + "\n</memory>\n\n" + MEMORY_RULES
    if switch_from:
        block += "\n" + _handoff(switch_from)
    return block


def _handoff(prev_model: str) -> str:
    """换模型时的交接说明。

    这是「聊天中途切模型掉质量」的真正原因：新模型看到的 assistant 历史是
    别人写的，它会当成"自己说过的话"去延续，于是口吻漂移、重复结论、
    甚至把上一个模型犯的错当成事实继续推理。

    修法不是重放历史，而是三句话把"这不是你说的"讲清楚。
    实测成本约 90 token，比重新摘要整段对话便宜两个数量级。
    """
    label = PROVIDER_LABEL.get(prev_model, prev_model)
    return (
        "\n\n【交接说明】\n"
        f"这条对话前面的回复由另一个模型（{label}）生成，用户刚切换到你。\n"
        "1. 那些回复不是你写的，不要模仿它们的口吻、长度和口头禅。\n"
        "2. 它们已经给出的结论不要重复，直接在此基础上往下说。\n"
        "3. 用户没问「是不是换模型了」就不要提这件事，也不要解释自己是谁。\n"
        "4. 如果你判断前面有错，简短纠正一句就继续，不要长篇复盘。"
    )


# 只用于交接话术里的人话名字；面板里改了 label 也不影响这里
PROVIDER_LABEL = {
    "deepseek": "DeepSeek",
    "qwen": "通义千问",
    "glm": "智谱 GLM",
    "hunyuan": "腾讯混元",
    "minimax": "MiniMax",
    "gpt": "ChatGPT",
    "gemini": "Gemini",
    "grok": "Grok",
    "openrouter": "聚合",
}


def provider_note(model: str) -> str:
    """不同厂商的脾气。这几句能让同一套提示词在 8 家上表现接近。"""
    m = (model or "").lower()
    notes: list[str] = []
    if any(k in m for k in ("gpt", "grok", "gemini")):
        notes.append("全程使用简体中文回答，专有名词保留原文。")
    if "gemini" in m:
        notes.append("回答直接给结论，不要复述上面的背景设定。")
    if "reasoner" in m or "r1" in m:
        notes.append("先思考再回答，但最终只输出结论，不要输出思考过程。")
    return ("\n" + "\n".join(notes)) if notes else ""


def _attach(messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    """记忆块追加到最后一个 system 消息末尾；没有 system 就新建一条放最前。"""
    if not block:
        return messages
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "system":
            base = out[i].get("content")
            if isinstance(base, str):
                out[i] = {**out[i], "content": base + "\n\n" + block}
                return out
            break
    return [{"role": "system", "content": block}] + out


async def build(
    messages: list[dict[str, Any]],
    *,
    user_id: str,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    model: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "user": user_id,
        "agent": agent_id,
        "recalled": 0,
        "compressed": False,
        "summarized": False,
        "tokens_before": messages_tokens(messages),
        "tokens_after": 0,
    }

    system_msgs = [m for m in messages if m.get("role") == "system"]
    dialog = [m for m in messages if m.get("role") != "system"]

    # ---- 1. 超长片段：只压"用户贴进来的大段文本"，不动对话 ----
    if settings.compress_enabled:
        for m in dialog:
            if m.get("role") != "user" or not isinstance(m.get("content"), str):
                continue
            if estimate_tokens(m["content"]) >= settings.compress_min_tokens:
                m["content"] = compress_long_text(m["content"])
                meta["compressed"] = True

    # ---- 2. 超预算：老对话压成滚动摘要，只保留最近 N 轮原文 ----
    summary_text, covered = ("", 0)
    if conversation_id:
        summary_text, covered = await store.get_summary(conversation_id)

    keep = max(2, settings.keep_recent_turns * 2)
    if messages_tokens(dialog) > settings.history_budget and len(dialog) > keep:
        dropped, dialog = dialog[:-keep], dialog[-keep:]
        if settings.compress_enabled:
            summary_text = await summarize(summary_text, dropped)
            if conversation_id and summary_text:
                await store.set_summary(
                    conversation_id,
                    user_id,
                    summary_text,
                    covered + len(dropped),
                    [i for i in (m.get("messageId") or m.get("id") for m in dropped) if i],
                    model=model,
                    tokens_before=meta["tokens_before"],
                )
            meta["summarized"] = True
        log.info("history trimmed: dropped=%d remaining=%d", len(dropped), len(dialog))

    # ---- 3. 检索记忆（Mem0 优先，失败自动退回自带向量检索）----
    # 顺序很重要：先放"必带层"（identity+preference），再放检索命中。
    # 必带层不参与相似度竞争，否则"今天好累"这种短句永远映不到"他在创业"。
    core = await store.get_core_memories(user_id, agent_id=agent_id)
    query = next((text_of(m) for m in reversed(dialog) if m.get("role") == "user"), "")
    hits: list[dict[str, Any]] = []
    if settings.mem0_enabled and query:
        hits = await mem0_engine.search(
            query, user_id=user_id, agent_id=agent_id, limit=settings.memory_top_k
        )
    builtin = await store.search_memories(
        user_id, query, agent_id=agent_id, k=settings.memory_top_k, min_score=settings.memory_min_score
    )
    seen = {c.get("text") for c in core} | {h.get("text") for h in hits}
    hits += [b for b in builtin if b.get("text") not in seen]
    hits = core + hits[: settings.memory_top_k]
    meta["recalled"] = len(hits)

    # ---- 4. 组装 ----
    profile = await store.get_profile(user_id)
    switch_from = ""
    if conversation_id and model:
        switch_from = await store.touch_state(conversation_id, user_id, model) or ""
        meta["switch_from"] = switch_from

    # 话题记忆：这条对话如果已经归进某个话题，把话题摘要一起带上。
    # 作用不是"补充事实"，而是让它在聊到一半时知道"这件事之前进展到哪了"——
    # 单条记忆只能回答"他是谁"，话题摘要才能回答"这件事做到哪一步"。
    topic_text = ""
    if conversation_id:
        try:
            t = await store.topic_of_conversation(conversation_id)
            if t:
                topic_text = f"{t['title']}｜{t['summary']}".strip("｜")
                meta["topic"] = t["title"]
        except Exception as exc:  # noqa: BLE001
            log.warning("topic lookup failed: %s", exc)

    block = render_memory_block(profile, hits, summary_text, topic_text, switch_from=switch_from)
    block += provider_note(model)

    out = _attach(system_msgs + dialog, block)
    meta["tokens_after"] = messages_tokens(out)
    return out, meta
