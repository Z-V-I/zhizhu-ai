"""厂商注册表：用户选「厂商」，管理员决定「这家实际用哪个模型」。

为什么要有这一层：
  直接让用户在模型列表里挑 `deepseek-chat` / `qwen3-max` 这种真实模型名，
  等于把"厂商改型号、下线旧型号、价格调整"的运维责任丢给使用者。
  这里改成：前端只暴露 8 个稳定的「厂商代号」，代号背后挂哪个真实模型
  由管理员在 /admin 里定。厂商换代时，用户无感。

密钥怎么存：
  Postgres 里存 Fernet 密文（`f:` 前缀）。没配 SECRET_KEY 时降级成明文
  并打 `p:` 前缀——不是"更安全的明文"，只是让单机部署不至于起不来。
  环境变量里的同名 Key 优先级更高，这样旧部署不用迁移。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from typing import Any, Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import db, upstream

log = logging.getLogger("brain.providers")

# code 就是暴露给前端的 model id，一旦上线不要改（会话记录里存的是它）
DEFAULTS: list[dict[str, Any]] = [
    {
        "code": "deepseek",
        "label": "DeepSeek",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "sort": 10,
    },
    {
        "code": "qwen",
        "label": "通义千问",
        "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "sort": 20,
    },
    {
        "code": "glm",
        "label": "智谱 GLM",
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "sort": 30,
    },
    {
        "code": "hunyuan",
        "label": "腾讯混元",
        "kind": "openai",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "key_env": "HUNYUAN_API_KEY",
        "sort": 40,
    },
    {
        "code": "minimax",
        "label": "MiniMax",
        "kind": "openai",
        "base_url": "https://api.minimax.chat/v1",
        "key_env": "MINIMAX_API_KEY",
        "sort": 50,
    },
    {
        "code": "gpt",
        "label": "ChatGPT",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "sort": 60,
    },
    {
        "code": "gemini",
        "label": "Gemini",
        "kind": "gemini",
        # OpenAI 兼容层，这样流式和参数处理跟其它家一致
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "list_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "key_env": "GEMINI_API_KEY",
        "sort": 70,
    },
    {
        "code": "grok",
        "label": "Grok",
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "sort": 80,
    },
    {
        # 聚合网关：一个 Key 打通所有海外厂商。国内家用它比自己挨个开卡省事，
        # 代价是官方模型名要带 vendor 前缀（openai/gpt-5、google/gemini-2.5-pro）。
        # 它自己也是一个可选厂商（前端显示为「聚合」），同时给其它厂商当"借 Key"来源。
        "code": "openrouter",
        "label": "OpenRouter 聚合",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "sort": 90,
    },
]

CODE_TO_KEY_ENV = {p["code"]: p["key_env"] for p in DEFAULTS}

# 这些参数国内几家收到会直接 400
# 注意：这里不要再剔 stream_options。剔掉之后上游不返回 usage，
# LibreChat 的 agents 客户端会抛 "Cannot use 'in' operator ... tool_calls"。
_DROP_FOR_CN = ("logit_bias", "seed", "presence_penalty", "frequency_penalty")


# ---------------------------------------------------------------- 密钥加解密


def _fernet() -> Optional[Fernet]:
    raw = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET") or ""
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(plain: str) -> str:
    if not plain:
        return ""
    f = _fernet()
    if not f:
        return "p:" + plain
    return "f:" + f.encrypt(plain.encode()).decode()


def decrypt_key(blob: str) -> str:
    if not blob:
        return ""
    if blob.startswith("f:"):
        f = _fernet()
        if not f:
            log.error("密钥是密文但拿不到 SECRET_KEY/JWT_SECRET，无法解密")
            return ""
        try:
            return f.decrypt(blob[2:].encode()).decode()
        except InvalidToken:
            log.error("密钥解密失败：SECRET_KEY 变过了？重新填一次即可")
            return ""
    if blob.startswith("p:"):
        return blob[2:]
    return blob


def mask(plain: str) -> str:
    if not plain:
        return ""
    return plain[:4] + "…" + plain[-4:] if len(plain) > 12 else "已设置"


# ---------------------------------------------------------------- 读取


_cache: dict[str, Any] = {"at": 0.0, "rows": []}
_TTL = 5.0


async def seed() -> None:
    """首次启动把 8 家写进表；已存在的不动（管理员改过的不能被覆盖）。"""
    for p in DEFAULTS:
        await db.execute(
            """INSERT INTO providers (code, label, kind, base_url, list_url, sort, enabled)
               VALUES ($1,$2,$3,$4,$5,$6,false)
               ON CONFLICT (code) DO NOTHING""",
            p["code"],
            p["label"],
            p["kind"],
            p["base_url"],
            p.get("list_url"),
            p["sort"],
        )
    # 兼容旧部署：如果这个厂商在环境变量里有 Key，自动填进去（只需一次）
    for p in DEFAULTS:
        env_key = os.getenv(p["key_env"], "").strip()
        if not env_key:
            continue
        row = await db.fetchrow(
            "SELECT api_key_enc, model_id FROM providers WHERE code=$1", p["code"]
        )
        if row and not row["api_key_enc"]:
            await db.execute(
                "UPDATE providers SET api_key_enc=$2 WHERE code=$1",
                p["code"],
                encrypt_key(env_key),
            )
            log.info("provider %s: 从环境变量导入密钥", p["code"])
    _cache["at"] = 0.0


async def all_rows(force: bool = False) -> list[dict[str, Any]]:
    if not force and time.time() - _cache["at"] < _TTL:
        return _cache["rows"]
    rows = await db.fetch(
        """SELECT code,label,kind,base_url,list_url,api_key_enc,enabled,model_id,params,
                  last_fetch_at,last_fetch_error,sort
           FROM providers ORDER BY sort, code"""
    )
    out = [dict(r) for r in rows]
    own: dict[str, bool] = {}
    for r in out:
        own[r["code"]] = bool(decrypt_key(r.pop("api_key_enc") or ""))
    for r in out:
        r["has_key"] = own.get(r["code"], False) or bool(
            own.get(_params(r).get("key_from") or "", False)
        )
    _cache.update(at=time.time(), rows=out)
    return out


def _params(r: dict[str, Any]) -> dict[str, Any]:
    p = r.get("params") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    return p if isinstance(p, dict) else {}


async def row(code: str) -> Optional[dict[str, Any]]:
    for r in await all_rows():
        if r["code"] == code:
            return r
    return None


def key_of(r: dict[str, Any]) -> str:
    """环境变量优先（旧部署仍然生效），其次才是库里存的。"""
    env_key = os.getenv(CODE_TO_KEY_ENV.get(r["code"], ""), "").strip()
    if env_key:
        return env_key
    raw = r.get("api_key_enc")
    if not raw:
        # all_rows 已经把 api_key_enc 弹出去了，回表拿一次
        return ""
    return decrypt_key(raw)


async def _full_row(code: str) -> Optional[dict[str, Any]]:
    r = await db.fetchrow(
        """SELECT code,label,kind,base_url,list_url,api_key_enc,enabled,model_id,params,
                  last_fetch_at,last_fetch_error,sort FROM providers WHERE code=$1""",
        code,
    )
    return dict(r) if r else None


# ---------------------------------------------------------------- 对外：模型目录


async def catalog() -> dict[str, Any]:
    """给 /v1/models 用。只列「已启用 + 有 Key + 选了模型」的厂商。

    少一个厂商不是 bug：那是配置没填完。管理员面板会显示缺哪一步。
    """
    data = []
    for r in await all_rows():
        if not r["enabled"] or not r["model_id"]:
            continue
        if not (await _key_for(r["code"])):
            continue
        data.append(
            {
                "id": r["code"],
                "object": "model",
                "created": 0,
                "owned_by": "umf",
                "label": r["label"],
            }
        )
    # 智能体用的隐藏别名：不在 modelSpecs 里露面，用户看不到它，
    # 但 LibreChat 会拿这个列表做「模型是否合法」的校验 —— 所以必须在这里出现。
    # 带型号的隐藏别名：只为了让「能看图」这件事被 LibreChat 认出来。
    # 不出现在 modelSpecs 里，用户看到的仍然是 ChatGPT / Gemini 两张卡。
    hidden = {"gpt": "gpt-4.1-nano", "gemini": "gemini-2.5-flash-lite"}
    present = {d["id"] for d in data}
    for code, alias in hidden.items():
        if code in present and alias not in present:
            data.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "umf",
                    "label": f"{code}（支持图片）",
                }
            )

    if not any(d["id"] == "auto" for d in data):
        data.append(
            {
                "id": "auto",
                "object": "model",
                "created": 0,
                "owned_by": "umf",
                "label": "跟随当前厂商",
            }
        )
    return {"object": "list", "data": data}


async def _key_for(code: str, _depth: int = 0) -> str:
    r = await _full_row(code)
    # 显式写了 key_from 就以它为准：这张卡明说了"我的 Key 从别处来"，
    # 再优先用自己那把只会得到 401（拿 DeepSeek 的 Key 去请求 OpenRouter）。
    borrow = _params(r or {}).get("key_from")
    if borrow and borrow != code and _depth < 2:
        borrowed = await _key_for(str(borrow), _depth + 1)
        if borrowed:
            return borrowed
    env_key = os.getenv(CODE_TO_KEY_ENV.get(code, ""), "").strip()
    if env_key:
        return env_key
    return decrypt_key((r or {}).get("api_key_enc") or "")


async def target(code: str) -> Optional[upstream.Target]:
    """把厂商代号翻译成一次上游调用所需的全部信息。不可用时返回 None。

    支持「代号 + 后缀」：`gpt-4.1-nano` 会落到 `gpt` 这张卡上。
    为什么要这个：LibreChat 判断一个模型能不能看图，靠的是**模型名里有没有
    vision 关键字**（gpt-4.1 / gemini-2.5 …），而我们的代号（gpt / gemini）
    不含这些词，图片会被当成普通附件丢掉。让前端传带型号的名字，
    路由仍然回到同一张卡，用户看到的还是「ChatGPT」这张卡。
    """
    r = await _full_row(code)
    if not r:
        best = None
        for x in await all_rows():
            c = x["code"]
            if code.startswith(c + "-") or code.startswith(c + "/"):
                if best is None or len(c) > len(best):
                    best = c
        if best:
            t = await target(best)
            return replace(t, alias=code) if t else None
        return None
    if not r["enabled"] or not r["model_id"]:
        return None
    key = await _key_for(code)
    if not key:
        return None
    params = r["params"] or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    if not isinstance(params, dict):
        params = {}
    borrow = bool(params.get("key_from"))
    drop = set(params.get("drop") or ())
    # 借 Key 的（也就是走 OpenRouter 的）说 OpenAI 协议，别套国内厂商那套删参规则
    if code != "gpt" and not borrow:
        drop.update(_DROP_FOR_CN)
    return upstream.Target(
        alias=code,
        base=r["base_url"],
        model=r["model_id"],
        key=key,
        # prepare() 用它判断要不要剔 OpenAI 专属参数，所以 gpt 和聚合都按 openai 对待
        provider="openai" if (code == "gpt" or borrow) else code,
        drop=frozenset(drop),
        max_tokens=params.get("max_tokens"),
        label=r["label"],
        extra=params.get("body") or {},
    )


async def target_with(code: str, model: str) -> Optional[upstream.Target]:
    """同一家厂商，但指定具体型号。给内部杂活用（CHEAP_MODEL=deepseek:deepseek-flash）。

    为什么不另开一张卡：这张卡是给用户看的，不该为了"摘要用便宜型号"
    这种内部需求多出一个选项。用一个临时 Target 就够了。
    """
    t = await target(code)
    if t is None:
        return None
    return replace(t, model=model)


# ---------------------------------------------------------------- 拉模型列表


async def fetch_models(code: str) -> tuple[list[str], str]:
    """问厂商要模型列表。返回 (模型 id 列表, 错误信息)。

    8 家里有 3 家（MiniMax / 混元 / 智谱）的 /models 时有时无，
    所以失败不是异常，是把原因写进 last_fetch_error 让面板显示，
    同时允许管理员手填模型名——这是"能自己填"这条需求的关键。

    「借 Key」这张卡（params.key_from）必须**去借出方拉列表**：
    我们手里只有 OpenRouter 的 Key，拿它去请求 Google 的端点
    只会得到 400 INVALID_ARGUMENT——这正是 Gemini 一直"拉不到列表"的原因。
    借出方返回的是全量模型（openai/gpt-5、google/gemini-2.5-pro …），
    所以还要按本卡当前 model_id 的 vendor 前缀筛一遍，否则列出来的是几百个别人的模型。
    """
    r = await _full_row(code)
    if not r:
        return [], "未知厂商"

    params = _params(r)
    borrow = str(params.get("key_from") or "")
    src = None
    if borrow and borrow != code:
        src = await _full_row(borrow)
        if not src:
            return [], f"借 Key 的来源厂商 {borrow} 不存在"

    target = src or r
    key = await _key_for(borrow or code)
    if not key:
        hint = f"还没有填 API Key" if not borrow else f"借 Key 的来源 {borrow} 没有可用 Key"
        return [], hint

    base = (target["list_url"] or (target["base_url"].rstrip("/") + "/models")).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    if target["kind"] == "gemini":
        base = f"{base}?key={key}"
        headers = {}

    ids: list[str] = []
    err = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as c:
            resp = await c.get(base, headers=headers)
            if resp.status_code != 200:
                err = f"HTTP {resp.status_code}: {resp.text[:160]}"
            else:
                data = resp.json()
                if isinstance(data.get("data"), list):
                    ids = [str(x.get("id")) for x in data["data"] if x.get("id")]
                elif isinstance(data.get("models"), list):
                    for x in data["models"]:
                        name = str(x.get("name") or "").removeprefix("models/")
                        if name:
                            ids.append(name)
                else:
                    err = "返回结构不认识：" + json.dumps(data)[:160]
    except httpx.HTTPError as exc:
        err = f"{type(exc).__name__}: {exc}"

    ids = sorted(set(i for i in ids if i))

    # 借 Key 的情况：只留下本卡那一家厂商的模型
    prefix = ""
    if borrow:
        prefix = str(params.get("vendor_prefix") or "")
        if not prefix:
            cur = str(r.get("model_id") or "")
            if "/" in cur:
                prefix = cur.split("/", 1)[0] + "/"
        if prefix:
            filtered = [i for i in ids if i.startswith(prefix)]
            if filtered:
                ids = filtered
            elif ids:
                err = err or f"来源 {borrow} 的列表里没有 {prefix}* 的模型"

    if ids:
        await db.execute("DELETE FROM provider_models WHERE provider_code=$1", code)
        for mid in ids:
            await db.execute(
                """INSERT INTO provider_models (provider_code, model_id) VALUES ($1,$2)
                   ON CONFLICT DO NOTHING""",
                code,
                mid,
            )
    await db.execute(
        "UPDATE providers SET last_fetch_at=now(), last_fetch_error=$2 WHERE code=$1",
        code,
        err or None,
    )
    _cache["at"] = 0.0
    return ids, err


async def cached_models(code: str) -> list[str]:
    rows = await db.fetch(
        "SELECT model_id FROM provider_models WHERE provider_code=$1 ORDER BY model_id", code
    )
    return [r["model_id"] for r in rows]


# ---------------------------------------------------------------- 写


async def save(code: str, **fields: Any) -> None:
    allowed = {"label", "base_url", "list_url", "model_id", "enabled", "params", "kind", "sort"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        args.append(v)
        # params 是 jsonb，传进去的是 json 字符串；不写 ::jsonb 时 asyncpg 推不出类型，
        # 报的是 "could not determine data type of parameter $1"，跟 params 一点关系都看不出来
        cast = "::jsonb" if k == "params" else ""
        sets.append(f"{k}=${len(args)}{cast}")
    if not sets:
        return
    await db.execute(
        f"UPDATE providers SET {', '.join(sets)} WHERE code=${len(args) + 1}", *args, code
    )
    _cache["at"] = 0.0


async def save_key(code: str, plain: str) -> None:
    await db.execute(
        "UPDATE providers SET api_key_enc=$2 WHERE code=$1", code, encrypt_key(plain.strip())
    )
    _cache["at"] = 0.0


# ---------------------------------------------------------------- 内部杂活用的模型

# 面板里没显式配 params.embed_model 时的已知可选项（维度必须和 schema 的 vector(1024) 一致）
_EMBED_FALLBACK = {
    "qwen": ("text-embedding-v3", 1024),
    "gpt": ("text-embedding-3-small", 1024),
}


async def chat_target() -> Optional[upstream.Target]:
    """任意一个可用的厂商，给"抽取 / 摘要 / 起标题"这类杂活用。

    顺序按 sort，也就是面板里的排列顺序——管理员把便宜的那家放前面即可。
    """
    for r in await all_rows():
        if r["enabled"] and r["model_id"] and r["has_key"]:
            return await target(r["code"])
    return None


async def embed_target() -> Optional[upstream.Target]:
    """找一个能出 embedding 的厂商。

    为什么单独挑而不是复用聊天模型：多数厂商的聊天模型不提供 /embeddings，
    拿错了会 404，而且维度不对会让整张 memories 表插入失败。
    """
    rows = await all_rows()

    for r in rows:
        if not (r["enabled"] and r["has_key"]):
            continue
        params = r.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        model = params.get("embed_model")
        if model:
            t = await target(r["code"])
            if t:
                return upstream.Target(
                    alias=t.alias, base=t.base, model=model, key=t.key,
                    provider=t.provider, dimensions=int(params.get("dimensions") or 1024),
                )

    for code, (model, dim) in _EMBED_FALLBACK.items():
        r = next((x for x in rows if x["code"] == code), None)
        if not r or not (r["enabled"] and r["has_key"]):
            continue
        t = await target(code)
        if t:
            return upstream.Target(
                alias=t.alias, base=t.base, model=model, key=t.key,
                provider=t.provider, dimensions=dim,
            )
    return None
