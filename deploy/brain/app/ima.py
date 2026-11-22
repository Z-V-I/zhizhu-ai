"""腾讯 IMA（ima.qq.com）OpenAPI 客户端。

## 为什么走 IMA 而不是自己搭 RAG

用户明确要求"用 IMA 的生态"：数据传进 IMA 知识库，提问时问 IMA。
自己搓一层向量检索等于重造一个更差的 IMA，也接不上他平时在 IMA 里的其他资料。

## 接口事实（2026-11 实测规格）

  Base URL : https://ima.qq.com
  路径前缀 : /openapi/wiki/v1
  方法     : 全部 POST + JSON body
  认证     : Header `ima-openapi-clientid` / `ima-openapi-apikey`
  返回     : {"retcode": 0, "errmsg": "...", "data": {...}}（老版本文档写 code）

投放文件到知识库要走四步，不能省：

  ① create_media        → 拿到 media_id 和一份 COS 临时凭据
  ② PUT 到 COS          → 用凭据把字节传上去（必须自己算 V5 签名）
  ③ add_knowledge       → 把 media 登记进知识库
  ④ （可选）check_repeated_names → 上传前查重

笔记（`import_doc`）不需要 COS，但它落在"笔记"里、不进知识库，
所以往里灌数据仍然用文件上传这条路。

## 依赖

只用标准库（httpx 已有，COS 签名用 hmac/sha1）。
不引 COS SDK：那是一个几十 MB 的包，而我们只需要一个 PUT。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("brain.ima")

BASE = "https://ima.qq.com"
WIKI = "/openapi/wiki/v1"
NOTE = "/openapi/note/v1"

# media_type 字典（IMA 自己的编号，别自己发明）
MT = {
    "pdf": 1,
    "word": 3,
    "ppt": 4,
    "excel": 5,
    "markdown": 7,
    "image": 9,
    "note": 11,
    "text": 13,
    "xmind": 14,
    "audio": 15,
}


class ImaError(RuntimeError):
    """IMA 返回 retcode != 0，或网络层失败。"""


def _cred() -> tuple[str, str]:
    cid = (os.getenv("IMA_CLIENT_ID") or "").strip()
    key = (os.getenv("IMA_API_KEY") or "").strip()
    return cid, key


def configured() -> bool:
    cid, key = _cred()
    return bool(cid and key)


async def _post(path: str, body: dict[str, Any], *, timeout: float = 40) -> dict[str, Any]:
    cid, key = _cred()
    if not cid or not key:
        raise ImaError("还没配置 IMA 的 ClientID / APIKey")

    url = BASE + path
    headers = {
        "ima-openapi-clientid": cid,
        "ima-openapi-apikey": key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(url, json=body, headers=headers)
    if r.status_code >= 400:
        raise ImaError(f"IMA HTTP {r.status_code}: {r.text[:200]}")

    try:
        data = r.json()
    except ValueError as exc:
        raise ImaError(f"IMA 返回的不是 JSON：{r.text[:200]}") from exc

    # 新版是 retcode，旧版文档写 code —— 两个都认
    code = data.get("retcode", data.get("code", 0))
    if code not in (0, None):
        msg = data.get("errmsg") or data.get("message") or ""
        raise ImaError(f"IMA 错误 {code}: {msg}")
    return data.get("data") or {}


# ------------------------------------------------------------------ 知识库

async def list_kbs(limit: int = 20) -> list[dict[str, Any]]:
    """列出我能往里写的知识库。

    limit 必须 ≤ 20，超过会报 51（IMA 的硬限制，不是我们的）。
    """
    data = await _post(f"{WIKI}/search_knowledge_base", {"query": "", "cursor": "", "limit": min(limit, 20)})
    return data.get("info_list") or []


async def search_kb_in(query: str, limit: int = 20) -> list[dict[str, Any]]:
    data = await _post(f"{WIKI}/search_knowledge_base", {"query": query, "cursor": "", "limit": min(limit, 20)})
    return data.get("info_list") or []


async def get_kb(ids: list[str]) -> dict[str, Any]:
    data = await _post(f"{WIKI}/get_knowledge_base", {"ids": ids})
    return data.get("infos") or {}


# ------------------------------------------------------------------ 检索

async def search(kb_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """在知识库里搜条目。返回 [{media_id, title, media_type, ...}]"""
    data = await _post(
        f"{WIKI}/search_knowledge",
        {"knowledge_base_id": kb_id, "query": query, "cursor": "", "limit": max(1, min(limit, 20))},
    )
    return data.get("info_list") or []


async def media_info(media_id: str) -> dict[str, Any]:
    return await _post(f"{WIKI}/get_media_info", {"media_id": media_id})


async def doc_content(note_id: str, fmt: int = 0) -> str:
    data = await _post(f"{NOTE}/get_doc_content", {"note_id": note_id, "target_content_format": fmt})
    return data.get("content") or ""


async def read_entry(media_id: str) -> str:
    """取一条知识库条目的可读内容。

    条目可能是文件（给直链，要再下载）也可能是笔记（给 notebook_id）。
    我们同步进去的都是 markdown 文件，直链能直接拿文本；
    取不到就退回空串 —— 检索阶段已经拿到标题和摘要了，正文只是加分项。
    """
    info = await media_info(media_id)
    nb = (info.get("notebook_ext_info") or {}).get("notebook_id")
    if nb:
        try:
            return await doc_content(nb)
        except ImaError as exc:
            log.warning("doc_content failed %s: %s", nb, exc)
    url = ((info.get("url_info") or {}).get("url")) or ""
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
            r = await cli.get(url)
        if r.status_code < 400:
            ct = r.headers.get("content-type", "")
            if "text" in ct or url.lower().endswith((".md", ".txt")):
                return r.text[:200_000]
    except Exception as exc:  # noqa: BLE001
        log.warning("download entry failed %s: %s", media_id, exc)
    return ""


# ------------------------------------------------------------------ 上传

def _cos_auth(secret_id: str, secret_key: str, token: str, host: str, cos_key: str, method: str = "put") -> str:
    """腾讯云 COS 的 V5 签名（纯 HMAC-SHA1，不需要 SDK）。"""
    now = int(time.time())
    sign_time = f"{now};{now + 3600}"
    key_time = sign_time

    sign_key = hmac.new(secret_key.encode(), sign_time.encode(), hashlib.sha1).hexdigest()

    # 参与签名的 header 只有 host；key 必须做 URL 编码
    http_string = f"{method}\n/{cos_key}\n\nhost={host}\n"
    string_to_sign = (
        "sha1\n"
        f"{key_time}\n"
        f"{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    )
    sig = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()

    return (
        "q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={sign_time}"
        f"&q-key-time={key_time}"
        "&q-header-list=host"
        "&q-url-param-list="
        f"&q-signature={sig}"
    )


async def upload_text(
    kb_id: str,
    name: str,
    text: str,
    *,
    media_type: int = MT["markdown"],
    folder_id: Optional[str] = None,
) -> dict[str, Any]:
    """把一个文本文件传进知识库。返回 add_knowledge 的 data。

    四步一条都不能少：create_media → COS PUT → add_knowledge。
    """
    raw = text.encode("utf-8")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "md"
    ctype = "text/markdown" if media_type == MT["markdown"] else "text/plain"

    created = await _post(
        f"{WIKI}/create_media",
        {
            "file_name": name,
            "file_size": len(raw),
            "content_type": ctype,
            "knowledge_base_id": kb_id,
            "file_ext": ext,
        },
    )
    media_id = created.get("media_id") or ""
    cred = created.get("cos_credential") or {}
    cos_key = cred.get("cos_key") or ""
    # 字段名以线上实际返回为准：bucket 叫 bucket_name。
    # ★ 不要用返回里的 custom_domain（ima-share-kb.image.myqcloud.com）：
    #   那是 CDN 域名，签了名 PUT 上去也是 403；必须走 COS 的
    #   <bucket>.cos.<region>.myqcloud.com（region=accelerate 时就是全局加速域名）。
    bucket = cred.get("bucket_name") or cred.get("bucket") or ""
    region = cred.get("region") or ""
    if not (media_id and cos_key and bucket and region):
        raise ImaError(f"create_media 返回不完整：{str(created)[:240]}")

    host = f"{bucket}.cos.{region}.myqcloud.com"
    url = f"https://{host}/{quote(cos_key, safe='/')}"
    headers = {
        "Authorization": _cos_auth(
            cred.get("secret_id", ""), cred.get("secret_key", ""), cred.get("token", ""), host, cos_key
        ),
        "x-cos-security-token": cred.get("token", ""),
        "Content-Type": ctype,
    }
    async with httpx.AsyncClient(timeout=90) as cli:
        put = await cli.put(url, content=raw, headers=headers)
    if put.status_code >= 400:
        raise ImaError(f"COS 上传失败 HTTP {put.status_code}: {put.text[:200]}")

    body: dict[str, Any] = {
        "media_type": media_type,
        "media_id": media_id,
        "title": name,
        "knowledge_base_id": kb_id,
        "file_info": {"cos_key": cos_key, "file_size": len(raw), "file_name": name},
    }
    if folder_id:
        body["folder_id"] = folder_id
    return await _post(f"{WIKI}/add_knowledge", body)


async def check_repeated(kb_id: str, names: list[tuple[str, int]]) -> list[dict[str, Any]]:
    data = await _post(
        f"{WIKI}/check_repeated_names",
        {"knowledge_base_id": kb_id, "params": [{"name": n, "media_type": t} for n, t in names]},
    )
    return data.get("info_list") or data.get("list") or []
