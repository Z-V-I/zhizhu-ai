"""模型调用客户端。只做两件轻活：摘要/抽取（非流式）、embedding。

流式聊天不走这里 —— 那条链路必须逐字节透传，单独在 main.py 里处理。

解析顺序（重要）：
  1. 厂商代号（deepseek / qwen / …）—— 管理面板配的那套
  2. models.json 里的别名 —— 环境变量里给了 Key 的旧部署仍然生效
  3. 任意一个可用厂商 —— 面板里只填了 Key 没走 .env 时，靠这条兜住
这样"只在面板里配 Key"和"只在 .env 里配 Key"两种用法都能跑，不会出现
"能聊天但记不住"这种一半坏掉的状态。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from . import providers, upstream

log = logging.getLogger("brain.llm")

_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))


async def _resolve(model: str, *, kind: str = "chat") -> upstream.Target:
    # "deepseek:deepseek-flash" —— 同一家的另一个型号，只给内部杂活用。
    # 没有这一条的话，"摘要用便宜的、聊天用贵的"就得在面板里多开一张卡。
    if ":" in model:
        code, _, sub = model.partition(":")
        if code and sub and await providers.row(code):
            target = await providers.target_with(code, sub)
            if target is not None:
                return target

    target = await providers.target(model)
    if target is not None:
        return target

    target = upstream.router.get(model)
    if target is not None:
        return target

    if kind == "cheap":
        target = await providers.chat_target() or upstream.router.cheap
    else:
        target = await providers.chat_target() or upstream.router.default("chat")
    if target is None:
        raise RuntimeError(f"没有可用的上游模型（找过厂商代号 {model}、别名、兜底）")
    return target


async def complete(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 1200,
) -> str:
    target = await _resolve(model, kind="cheap")
    body = upstream.router.prepare(
        {
            "model": target.alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        },
        target,
    )
    r = await _client.post(target.url, json=body, headers=target.headers())
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"].get("content") or "").strip()


async def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    target: Optional[upstream.Target] = await providers.embed_target()
    if target is None:
        target = upstream.router.embed
    if target is None:
        raise RuntimeError("没有可用的 embedding 模型，本次不写入向量")
    body: dict[str, Any] = {"model": target.model, "input": texts}
    if target.dimensions:
        body["dimensions"] = target.dimensions
    r = await _client.post(target.embed_url, json=body, headers=target.headers())
    r.raise_for_status()
    return [item["embedding"] for item in r.json()["data"]]
