"""Postgres 连接与建表。用 asyncpg，不引 ORM —— 只有十几张表，SQL 比 ORM 清楚。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import asyncpg

from .config import settings

log = logging.getLogger("brain.db")
_pool: Optional[asyncpg.Pool] = None

SCHEMA = Path(__file__).with_name("schema.sql")
# 后台表单独一个文件：主 schema 是"记忆资产"，这个是"运营后台"，混在一起会难读
SCHEMA_ADMIN = Path(__file__).with_name("schema_admin.sql")


async def init() -> None:
    global _pool
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL 未配置")
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        await conn.execute(SCHEMA_ADMIN.read_text(encoding="utf-8"))
    log.info("schema ready")


async def close() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db 未初始化")
    return _pool


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> Optional[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    async with pool().acquire() as conn:
        return await conn.execute(sql, *args)
