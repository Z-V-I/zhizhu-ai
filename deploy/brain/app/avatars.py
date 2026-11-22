"""头像对象存储：优先 Cloudflare R2，没有就落本地卷。

为什么要单独一层：LibreChat 原生把头像写到容器里的 `client/public/images`，
既不能给智能体批量换、也不方便备份。头像属于"用户资产"，和记忆一样应该
有一个可迁移的落点。这里只做两件事：存对象、给 URL。

实现走 R2 的 **REST 对象接口**（Bearer + cf-r2-jurisdiction 头），不用 S3 密钥。
原因很实际：S3 Access Key 只能在 Cloudflare 后台手工生成，而 REST 接口用
一个「只对 umf-avatars 有读写权」的 API Token 就够了，机器上不需要放全局密钥。

键的约定（决定了以后迁移方不方便）：
    u/{user_id}/{sha1}.{ext}   用户头像
    a/{agent_id}.svg           智能体头像（DiceBear）
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("brain.avatars")


def backend() -> str:
    return "r2" if settings.r2_ready else "local"


def _local_path(key: str) -> Path:
    p = Path(settings.avatar_dir) / key
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _object_url(key: str) -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{settings.r2_account_id}"
        f"/r2/buckets/{settings.r2_bucket}/objects/{key}"
    )


def _headers(content_type: str = "") -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {settings.r2_api_token}",
        # 这个头不是可选的：账号在 EU 区域，不带它对象接口一律 401。
        "cf-r2-jurisdiction": settings.r2_jurisdiction,
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def put(data: bytes, key: str, content_type: str = "") -> str:
    """写入对象并返回可直接放进 <img src> 的 URL。"""
    content_type = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
    if settings.r2_ready:
        r = httpx.put(
            _object_url(key),
            headers=_headers(content_type),
            content=data,
            timeout=30,
        )
        if r.status_code >= 300:
            # 上传失败不能让调用方拿到一个假 URL：宁可报错
            raise RuntimeError(f"R2 put {key} failed: {r.status_code} {r.text[:200]}")
        return f"{settings.r2_public_base}/{key}"

    _local_path(key).write_bytes(data)
    return f"/api/umf/avatar/file/{key}"


def get(key: str) -> Optional[tuple[bytes, str]]:
    """按 key 取回对象。R2 后端直接读公开域名（顺带吃 CF 缓存）。"""
    if settings.r2_ready:
        try:
            r = httpx.get(f"{settings.r2_public_base}/{key}", timeout=20)
        except Exception as exc:
            log.warning("avatar get %s failed: %s", key, exc)
            return None
        if r.status_code != 200:
            return None
        return r.content, r.headers.get("content-type", "application/octet-stream")

    p = Path(settings.avatar_dir) / key
    if not p.is_file():
        return None
    return p.read_bytes(), mimetypes.guess_type(key)[0] or "application/octet-stream"


def key_for_user(user_id: str, data: bytes, ext: str) -> str:
    digest = hashlib.sha1(data).hexdigest()[:12]
    return f"u/{user_id}/{digest}.{ext.lstrip('.')}"


def key_for_agent(agent_id: str) -> str:
    return f"a/{agent_id}.svg"


def ensure_dir() -> None:
    if not settings.r2_ready:
        os.makedirs(settings.avatar_dir, exist_ok=True)
