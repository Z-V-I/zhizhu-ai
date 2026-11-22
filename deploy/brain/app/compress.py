"""对话压缩。三层，从零成本到高成本，只在需要时逐层启用：

1. 结构裁剪     —— 保留最近 N 轮 + 首条指令，零成本，永远可用
2. 滚动摘要     —— 一次 cheap-extract 调用，结果落库复用（真正的主力）
3. LLMLingua-2 —— token 级压缩，只用于"又长又不像对话"的片段（粘贴的文档、日志）

为什么第 3 层不是主力：token 级压缩会打散中文语序、破坏工具调用参数。
它擅长的是"把 20 页文档压成 6 页"，不擅长"把聊天压短"。搞反了会明显掉智商。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from .config import settings
from .llm import complete

log = logging.getLogger("brain.compress")

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """不引 tiktoken：对中文按字算、其余按 4 字符 1 token，误差 ±10%，用来做预算足够。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + max(0, len(text) - cjk) // 4


def text_of(message: dict[str, Any]) -> str:
    """兼容多模态 content（字符串或 parts 数组）。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(text_of(m)) for m in messages)


# ---------------------------------------------------------------- LLMLingua-2

_lingua = None
_lingua_failed = False
_lingua_lock = threading.Lock()


def _get_lingua():
    """懒加载。模型 5 亿参数，首次下载几百 MB，之后常驻内存。"""
    global _lingua, _lingua_failed
    if _lingua is not None or _lingua_failed:
        return _lingua
    with _lingua_lock:
        if _lingua is not None or _lingua_failed:
            return _lingua
        try:
            from llmlingua import PromptCompressor

            _lingua = PromptCompressor(
                model_name=settings.llmlingua_model,
                use_llmlingua2=True,
                device_map="cpu",
            )
            log.info("llmlingua-2 loaded: %s", settings.llmlingua_model)
        except Exception as exc:  # 没装 torch / 没下到模型 / 内存不够，都只是降级
            _lingua_failed = True
            log.warning("llmlingua unavailable, fallback to truncation: %s", exc)
    return _lingua


def compress_long_text(text: str, ratio: float | None = None) -> str:
    """超长片段压缩。失败一律降级成"保留头尾"，绝不阻塞请求。"""
    ratio = settings.compress_ratio if ratio is None else ratio
    if not text or estimate_tokens(text) < settings.compress_min_tokens:
        return text

    lingua = _get_lingua()
    if lingua is not None:
        try:
            out = lingua.compress_prompt(
                text,
                rate=ratio,
                force_tokens=["\n", "？", "！", "：", "，", "。", "`"],
                drop_consecutive=True,
            )
            compressed = out.get("compressed_prompt") or ""
            if len(compressed) > 200:
                log.info("llmlingua %d -> %d tokens", estimate_tokens(text), estimate_tokens(compressed))
                return compressed
        except Exception as exc:
            log.warning("llmlingua compress failed: %s", exc)

    return _head_tail(text, ratio)


def _head_tail(text: str, ratio: float) -> str:
    keep = max(400, int(len(text) * max(0.2, ratio)))
    head = int(keep * 0.65)
    tail = keep - head
    return (
        text[:head]
        + f"\n\n…[此处省略约 {len(text) - keep} 字，原文已保留在会话记录中]…\n\n"
        + text[-tail:]
    )


# ---------------------------------------------------------------- 滚动摘要

SUMMARY_SYSTEM = """你在维护一段长期对话的记忆摘要，用来替代被裁掉的原文。

规则：
- 用第三人称短句，只写"事实、决定、未决问题、情绪脉络、称呼习惯"。
- 合并同类项，不要按时间流水账复述。
- 保留具体的名字、数字、版本、文件名；丢掉寒暄和重复。
- 输出不超过 400 字，纯文本，不要标题、不要分点符号。
- 如果新内容与旧摘要冲突，以新内容为准，并在句末标注（已更新）。"""


async def summarize(
    previous_summary: str,
    dropped: list[dict[str, Any]],
    model: str | None = None,
) -> str:
    transcript = "\n".join(f"{m.get('role')}: {text_of(m)[:1200]}" for m in dropped)
    user = (
        f"【已有摘要】\n{previous_summary or '（无）'}\n\n"
        f"【需要并入的新对话】\n{transcript}\n\n"
        "请输出合并后的新摘要。"
    )
    try:
        return await complete(
            model or settings.cheap_model,
            [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=700,
        )
    except Exception as exc:
        log.warning("summarize failed, keep old summary: %s", exc)
        return previous_summary
