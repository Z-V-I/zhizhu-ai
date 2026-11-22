"""Mem0 适配层（可选）。

启用后由 Mem0 负责"抽取 + 混合检索"，我们只负责把结果写进 UMF 表。
不启用时走 extract.py + pgvector，功能等价、依赖更少。

两者都保留的原因：Mem0 升级可能引入行为变化，而记忆是长期资产，
必须保证"哪怕 Mem0 挂了，我的记忆还在、还能用"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .config import settings

log = logging.getLogger("brain.mem0")

_memory = None
_failed = False


def _pg_config() -> dict[str, Any]:
    url = urlparse(settings.database_url)
    return {
        "dbname": (url.path or "/umf").lstrip("/"),
        "user": unquote(url.username or "umf"),
        "password": unquote(url.password or ""),
        "host": url.hostname or "postgres",
        "port": url.port or 5432,
        "collection_name": "mem0_vectors",
        "embedding_model_dims": 1536,
        "hnsw": True,
    }


def _get():
    global _memory, _failed
    if _memory is not None or _failed:
        return _memory
    try:
        from mem0 import Memory

        _memory = Memory.from_config(
            {
                "vector_store": {"provider": "pgvector", "config": _pg_config()},
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": settings.cheap_model,
                        "openai_base_url": f"{settings.litellm_base}/v1",
                        "api_key": settings.master_key,
                    },
                },
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": settings.embed_model,
                        "openai_base_url": f"{settings.litellm_base}/v1",
                        "api_key": settings.master_key,
                    },
                },
            }
        )
        log.info("mem0 engine ready (pgvector)")
    except Exception as exc:
        _failed = True
        log.warning("mem0 unavailable, using builtin store: %s", exc)
    return _memory


async def search(
    query: str,
    *,
    user_id: str,
    agent_id: Optional[str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    if not settings.mem0_enabled:
        return []
    memory = _get()
    if memory is None:
        return []
    try:
        result = await asyncio.to_thread(
            memory.search, query, user_id=user_id, agent_id=agent_id, limit=limit
        )
        items = result.get("results", result) if isinstance(result, dict) else result
        return [
            {"text": it.get("memory", ""), "score": float(it.get("score") or 0), "id": it.get("id"), "source": "mem0"}
            for it in items or []
            if it.get("memory")
        ]
    except Exception as exc:
        log.warning("mem0 search failed: %s", exc)
        return []


async def remember(messages: list[dict[str, Any]], *, user_id: str, agent_id: Optional[str]) -> None:
    if not settings.mem0_enabled:
        return
    memory = _get()
    if memory is None:
        return
    try:
        await asyncio.to_thread(memory.add, messages, user_id=user_id, agent_id=agent_id)
    except Exception as exc:
        log.warning("mem0 add failed: %s", exc)
