"""记忆抽取：把一段对话变成几条可入库的事实。

这是"一个大脑"真正长出来的地方。抽取质量直接决定产品观感，
所以有四条硬规则写进 prompt，不靠模型自觉：
  1. 只抽长期有效的，不抽寒暄和一次性信息
  2. 每条一句话，带得出具体名词
  3. 区分事实 / 偏好 / 决定，因为它们的生命周期完全不同
  4. 宁可少抽，不要抽错 —— 抽错的代价是用户要手动删
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .compress import text_of
from .config import settings
from .llm import complete

log = logging.getLogger("brain.extract")

SYSTEM = """你是一个记忆抽取器，从对话里提炼"值得长期记住"的信息。

输出严格的 JSON 数组，不要任何解释文字。每个元素：
{"layer": "identity|preference|experience|activity|context",
 "kind": "fact|preference|decision|relation|event",
 "text": "一句话，第三人称，不超过 40 字，必须含具体名词",
 "scope_hint": "global|agent",
 "entities": ["名词1", "名词2"],
 "confidence": 0.0-1.0,
 "valid_from": "YYYY-MM-DD 或 null"}

layer 的判断（最重要，决定这条记忆活多久）：
- identity   他是谁：职业、城市、家庭、称呼、长期身份。几乎不变。
- preference 他喜欢怎样：沟通风格、输出格式、口味、禁忌、反感的事。会慢慢变。
- experience 他做过什么、踩过什么坑、验证过的结论。可复用，不分话题。
- activity   他正在做什么：进行中的项目、计划、最近在学的东西。**有保质期**。
- context    只在这一次对话里成立的背景。默认不进 global。

判定标准：
- 抽：身份、职业、长期项目、技术栈偏好、审美偏好、重要人际关系、明确做出的决定、改变过的看法。
- 不抽：当前这句话在问什么、临时情绪、客套、能从本轮上下文直接看出的东西、模型的建议。
- scope_hint 用 global 表示"任何人格都该知道"；用 agent 表示"只有当前这个角色需要知道"（例如某个技术方案细节）。
- 最多 6 条。没有值得记的就输出 []。
- 不确定的宁可不抽。

**最容易搞错的一条，务必分清"他是谁"和"他说到谁"：**
对话里经常出现第三个人 —— 朋友、同事、采访对象、新闻里的人、他读到或分析的案例、
他帮我写的虚构角色。这些人的身份和经历**绝对不能写成他的**。
判断方法：这句事实的主语是不是"我 / 他本人"？
  对话："我认识一个创始人，他是河南省重点研发专项负责人" → 不抽，那是别人
  对话："我在做表白墙机器人"                                → 抽，这是他本人
如果一句话只说明"聊天里提到过某人"，本来就不值得记 —— 不要抽。
拿不准主语是谁时，不抽。"""

_LAYERS = {"identity", "preference", "experience", "activity", "context"}

_JSON_BLOCK = re.compile(r"\[.*\]", re.S)


async def extract(messages: list[dict[str, Any]], model: str | None = None) -> list[dict[str, Any]]:
    transcript = "\n".join(f"{m.get('role')}: {text_of(m)[:800]}" for m in messages if m.get("role") != "system")
    if not transcript.strip():
        return []

    try:
        raw = await complete(
            model or settings.cheap_model,
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": transcript}],
            temperature=0.0,
            max_tokens=1200,
        )
    except Exception as exc:
        log.warning("extract call failed: %s", exc)
        return []

    match = _JSON_BLOCK.search(raw)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("extract returned non-json: %s", raw[:200])
        return []

    clean: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or len(text) > 120:
            continue
        clean.append(
            {
                "layer": item.get("layer") if item.get("layer") in _LAYERS else None,
                "kind": item.get("kind") if item.get("kind") in {"fact", "preference", "decision", "relation", "event"} else "fact",
                "text": text,
                "scope_hint": "agent" if item.get("scope_hint") == "agent" else "global",
                "entities": [str(e)[:24] for e in (item.get("entities") or [])][:6],
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.7))),
                "valid_from": item.get("valid_from") or None,
            }
        )
    return clean
