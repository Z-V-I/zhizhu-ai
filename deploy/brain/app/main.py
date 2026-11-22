"""UMF Brain · 服务入口

职责边界（刻意保持很小）：
  · /v1/chat/completions  假装是模型，实际是"记忆注入 + 流式透传"的中间人
  · /api/umf/*            UMF 的读写接口，给记忆中心 UI 用
  · 其余一概不做 —— 聊天前端、用户体系、模型路由都有现成项目负责
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import (
    admin,
    auth,
    avatars,
    context,
    db,
    exporter_mongo,
    importer_mongo,
    importers,
    memory_build,
    mg,
    ops,
    providers,
    store,
    upstream,
)
from .compress import messages_tokens
from .config import settings
from .extract import extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("brain")

client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await auth.ensure_owner()
    # Mongo 只给后台"看"用：连不上也不拦启动，只是用户追踪那一块降级
    mg.connect()
    await providers.seed()
    log.info("brain up (mem0=%s, compress=%s)", settings.mem0_enabled, settings.compress_enabled)
    yield
    mg.close()
    await db.close()
    await client.aclose()


app = FastAPI(title="UMF Brain", version="0.4.0", lifespan=lifespan)
app.include_router(admin.router)
app.include_router(ops.router)


async def _attempts(body: dict[str, Any]) -> list[tuple[str, dict[str, str], dict[str, Any]]]:
    """排出这次请求的尝试顺序：[(url, headers, body), ...]。

    优先级：
      1. 厂商代号（deepseek / qwen / …）—— 用户面看到的那 8 个，由管理员配模型
      2. models.json 里的别名 —— 内部用（cheap-extract / embed），也兼容旧部署
      3. LiteLLM —— 只在上面都没有且配了 base 时兜底

    "用户切厂商"和"内部杂活"走两套命名空间，是为了不让抽取/摘要用的便宜模型
    出现在用户的模型下拉里。
    """
    model = str(body.get("model") or "")
    plan: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    target = await providers.target(model)
    if target:
        plan.append((target.url, target.headers(), upstream.router.prepare(body, target)))

    for t in upstream.router.chain(model):
        if target and t.model == target.model and t.base == target.base:
            continue
        plan.append((t.url, t.headers(), upstream.router.prepare(body, t)))

    if not plan and settings.litellm_base:
        plan.append(
            (
                f"{settings.litellm_base}/v1/chat/completions",
                {"Authorization": f"Bearer {settings.master_key}", "Content-Type": "application/json"},
                body,
            )
        )
    return plan


# ------------------------------------------------------------------ 鉴权

def _b64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def verify_token(token: str) -> Optional[dict[str, Any]]:
    """与 LibreChat 共用 JWT_SECRET，省掉两边同步用户表的麻烦。"""
    try:
        head, body, sig = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(settings.jwt_secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64(sig), expected):
        return None
    try:
        payload = json.loads(_b64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    if exp and float(exp) < time.time():
        return None
    return payload


async def current_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_debug_user: Optional[str] = Header(default=None),
) -> str:
    if x_debug_user and settings.debug:
        return x_debug_user
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing token")
    payload = verify_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(401, "invalid token")
    uid = payload.get("id") or payload.get("userId") or payload.get("sub")
    if not uid:
        raise HTTPException(401, "no user id in token")
    return str(uid)


# ------------------------------------------------------------------ 对话主链路

async def extract_and_store(user_id: str, agent_id: Optional[str], user_text: str, reply: str) -> None:
    """后台任务：抽取记忆 → 写入 UMF → （可选）同步 Mem0。失败只记日志，不影响用户。"""
    if len(reply) < 30:
        return
    try:
        items = await extract([{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])
        for item in items:
            await store.add_memory(
                user_id,
                item["text"],
                scope="agent" if item["scope_hint"] == "agent" and agent_id else "global",
                agent_id=agent_id if item["scope_hint"] == "agent" else None,
                kind=item["kind"],
                layer=item.get("layer"),
                entities=item["entities"],
                confidence=item["confidence"],
                source={"platform": "brain", "agent_id": agent_id},
            )
        if items:
            log.info("extracted %d memories for %s", len(items), user_id)
            # 攒够阈值就顺手重算画像。不 await：这是聊天回复之后的后台任务，
            # 让它再等一次 8000 token 的调用没有意义，失败了也不该影响用户。
            await memory_build.maybe_refresh_profile(user_id)
    except Exception as exc:
        log.warning("extract_and_store failed: %s", exc)


def _fix_sse_line(line: str) -> Optional[str]:
    """修一条 SSE 行，返回要发出去的行（None = 丢掉这行）。

    只做一件事：把 delta.content 的 null 变成 ""。
    DeepSeek 这类推理模型在思考阶段每个块都是 {"content": null, "reasoning_content": "..."}，
    LibreChat 的 agents 运行时拿到 null 会把这条消息解析成 undefined，
    最后在 `'tool_calls' in undefined` 处崩掉——报错跟内容毫无关系，极难定位。
    """
    if not line.startswith("data: "):
        return line
    payload = line[6:].strip()
    if payload in ("", "[DONE]"):
        return line
    try:
        obj = json.loads(payload)
    except Exception:
        return line
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return line
    touched = False
    for c in choices:
        if not isinstance(c, dict):
            continue
        d = c.get("delta")
        if not isinstance(d, dict):
            c["delta"] = d = {}
            touched = True
        if d.get("content") is None:
            d["content"] = ""
            touched = True
    return "data: " + json.dumps(obj, ensure_ascii=False) if touched else line


def _sse_text(chunks: list[bytes]) -> str:
    """从 SSE 原始字节里把 delta.content 拼出来，只为抽记忆用。"""
    out: list[str] = []
    for line in b"".join(chunks).decode("utf-8", "ignore").splitlines():
        if not line.startswith("data: ") or line[6:].strip() in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(line[6:])
            delta = obj["choices"][0].get("delta") or {}
            if delta.get("content"):
                out.append(delta["content"])
        except Exception:
            continue
    return "".join(out)


def _last_user(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


# ------------------------------------------------------------------ 附件片段
#
# 现状（2026-11）：LibreChat 把"文本类上传"编成 content 数组里的
# `{"type":"file","file":{"filename":…}}`，正文有时在内联的 file_data 里，
# 有时只剩文件名（文本已经落到 Mongo 的 files.text 里，请求里不再带）。
# DeepSeek / 通义这些厂商的 OpenAI 兼容层只认 text / image_url，
# 遇到 file 片段缺 file_id / file_data 就直接 400：
#   `.messages[1].content[1]: file must have a file_id or file_data`
# 结果是一条带附件的对话永远发不出去，而报错信息完全看不出是附件引起的。
#
# 处理原则：能还原正文就还原成 text 片段（模型真的读到附件），
# 实在拿不到就换成一句说明，绝不让 400 挡住整条对话。

_FILE_PART_TYPES = {"file", "document", "input_file"}
_TEXTUAL_MIME = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/x-sh",
)
_ATTACH_MAX_CHARS = 120_000


def _file_obj(part: dict[str, Any]) -> dict[str, Any]:
    obj = part.get("file")
    return obj if isinstance(obj, dict) else part


def _data_uri_text(uri: Any) -> tuple[str, str]:
    """data:text/plain;base64,xxx → (mime, 正文)。非文本返回 (mime, "")。"""
    if not isinstance(uri, str) or not uri.startswith("data:") or "," not in uri:
        return "", ""
    head, _, payload = uri.partition(",")
    mime = head[5:].split(";")[0].strip().lower()
    if not mime.startswith(_TEXTUAL_MIME):
        return mime, ""
    try:
        raw = base64.b64decode(payload) if ";base64" in head else payload.encode()
        return mime, raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return mime, ""


def _lookup_file_text(user_id: str, filename: str) -> str:
    """data 被摘掉时，按 (用户, 文件名) 从 LibreChat 的 files 集合找回已抽取的正文。"""
    if not (user_id and filename and mg.ready()):
        return ""
    try:
        doc = mg.col("files").find_one(
            {"user": __import__("bson").ObjectId(user_id), "filename": filename},
            {"text": 1, "textFormat": 1},
            sort=[("createdAt", -1)],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("file text lookup failed (%s): %s", filename, exc)
        return ""
    text = (doc or {}).get("text") or ""
    return text if isinstance(text, str) else ""


async def _flatten_attachments(messages: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        parts: list[dict[str, Any]] = []
        for p in content:
            if not isinstance(p, dict) or p.get("type") not in _FILE_PART_TYPES:
                parts.append(p)
                continue
            obj = _file_obj(p)
            name = str(
                obj.get("filename") or obj.get("name") or p.get("filename") or "附件"
            ).strip() or "附件"
            data = obj.get("file_data") or p.get("file_data") or obj.get("data") or p.get("data") or ""
            dmime, text = _data_uri_text(data)
            mime = str(
                obj.get("mime_type") or obj.get("mimeType") or p.get("mime_type") or dmime or ""
            ).lower()
            if not text and not data:
                text = await asyncio.to_thread(_lookup_file_text, user_id, name)
            if not text and isinstance(obj.get("text"), str):
                text = obj["text"]
            if text:
                text = text[:_ATTACH_MAX_CHARS]
                parts.append({"type": "text", "text": f'<attachment name="{name}">\n{text}\n</attachment>'})
                log.info("attachment inlined: %s (%d chars)", name, len(text))
            elif data and mime and not mime.startswith(_TEXTUAL_MIME):
                # 二进制且上游能自己处理（PDF 之类）时才原样保留
                parts.append(p)
                log.info("attachment kept as file part: %s (%s)", name, mime)
            else:
                parts.append({"type": "text", "text": f"[附件「{name}」的正文没能取到，本轮请按没有该附件回答]"})
                log.warning("attachment unreadable: %s (mime=%s data=%s)", name, mime or "?", bool(data))
        if not parts:
            parts = [{"type": "text", "text": ""}]
        out.append({**m, "content": parts})
    return out


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "brain"}


@app.get("/api/umf/healthz")
async def healthz_prefixed() -> dict:
    """Caddy 把 /api/umf/* 原样转进来，所以外部探测走这个路径。"""
    return await healthz()


@app.get("/v1/models")
async def list_models():
    """只暴露「厂商代号」。真实模型名和上游地址留在服务端。"""
    return JSONResponse(content=await providers.catalog())


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    x_umf_user: Optional[str] = Header(default=None),
    x_umf_agent: Optional[str] = Header(default=None),
    x_umf_conv: Optional[str] = Header(default=None),
):
    body = await request.json()
    model = body.get("model", "")
    user_text = _last_user(body.get("messages", []))
    if settings.debug:
        log.info("hdr user=%s agent=%s conv=%s", x_umf_user, x_umf_agent, x_umf_conv)
        # 排障用：只打结构，不打完整内容
        log.info(
            "chat req model=%s keys=%s stream=%s tools=%s max_tokens=%s",
            model, sorted(body.keys()), body.get("stream"),
            len(body.get("tools") or []), body.get("max_tokens"),
        )

    user_id = x_umf_user
    if not user_id and settings.debug:
        user_id = request.headers.get("x-debug-user")

    # ---- 「智能体只换风格」的实现点 ----
    # 智能体记录里的 model 统一写成隐藏别名 auto（见 providers.catalog），
    # 于是所有智能体对话都带着 model=auto 进来。这里把它换成
    # 「用户上一次亲手选的厂商」；普通对话则反过来，把这次的厂商记成偏好。
    # 结果：换智能体只换 instructions，不换模型。
    if user_id:
        try:
            if model == "auto":
                pref = await store.get_pref(user_id, "vendor")
                if not pref or not await providers.row(pref):
                    pref = "deepseek"
                log.info("agent chat -> vendor %s (user pref)", pref)
                model = pref
                body["model"] = pref
            elif await providers.row(model):
                await store.set_pref(user_id, "vendor", model)
        except Exception as exc:  # 偏好失败绝不能挡住聊天本身
            log.warning("vendor pref skipped: %s", exc)

    if user_id:
        body["messages"], meta = await context.build(
            body.get("messages", []),
            user_id=user_id,
            agent_id=x_umf_agent,
            conversation_id=x_umf_conv,
            model=model,
        )
    else:
        meta = {"recalled": 0, "tokens_after": messages_tokens(body.get("messages", []))}
        log.warning("no X-UMF-User, memory layer skipped")

    # 附件片段归一化要在写 headers / 选上游之前做：它会把 file 片段换成 text，
    # token 数也会变，late 做会让 x-umf-tokens 报错数。
    body["messages"] = await _flatten_attachments(body.get("messages", []), user_id or "")
    meta["tokens_after"] = messages_tokens(body["messages"])

    resp_headers = {
        "x-umf-recall": str(meta.get("recalled", 0)),
        "x-umf-tokens": str(meta.get("tokens_after", 0)),
        "x-umf-compressed": "1" if meta.get("compressed") else "0",
        "x-umf-summarized": "1" if meta.get("summarized") else "0",
    }

    plan = await _attempts(body)
    if not plan:
        code = str(body.get("model") or "")
        r = await providers.row(code)
        if r:
            detail = f"「{r['label']}」还没配置好：需要管理员在 /admin 里填 Key 并选定模型。"
        else:
            detail = f"没有可用的上游（model={code}）：检查 models.json 的别名和对应的 API Key。"
        raise HTTPException(503, detail)

    if not body.get("stream"):
        last_status, last_detail = 502, "no upstream attempt"
        for url, headers, payload in plan:
            try:
                r = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_status, last_detail = 599, f"{type(exc).__name__}: {exc}"
                log.warning("upstream %s failed: %s", url, last_detail)
                continue
            if r.status_code == 200:
                data = r.json()
                if user_id:
                    reply = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    asyncio.create_task(extract_and_store(user_id, x_umf_agent, user_text, reply))
                return JSONResponse(content=data, status_code=200, headers=resp_headers)
            last_status, last_detail = r.status_code, r.text[:300]
            log.warning("upstream %s -> %s: %s", url, r.status_code, last_detail)
        return JSONResponse(
            {"error": {"message": last_detail, "type": "upstream_error"}},
            status_code=last_status if 400 <= last_status < 600 else 502,
            headers=resp_headers,
        )

    async def relay():
        chunks: list[bytes] = []
        err = "no upstream attempt"
        try:
            for url, headers, payload in plan:
                try:
                    async with client.stream("POST", url, json=payload, headers=headers) as r:
                        if r.status_code != 200:
                            err = f"{r.status_code} {(await r.aread())[:300].decode('utf-8', 'ignore')}"
                            log.warning("upstream %s -> %s", url, err)
                            continue
                        # 逐行重发而不是逐字节透传：只为了把 delta.content 的 null 修成 ""。
                        # 不修的话，推理型模型（DeepSeek 全系）前几十个块 content 是 null，
                        # LibreChat 的 agents 运行时会在 'tool_calls' in undefined 上崩掉，
                        # 报错是 "Cannot use 'in' operator to search for 'tool_calls' in undefined"。
                        # 对一个只装一个人的站来说，这个 30 行比"换个模型"便宜。
                        pending: list[str] = []
                        async for line in r.aiter_lines():
                            if line == "":
                                if pending:
                                    block = ("\n".join(pending) + "\n\n").encode()
                                    chunks.append(block)
                                    yield block
                                    pending.clear()
                                continue
                            fixed = _fix_sse_line(line)
                            if fixed is not None:
                                pending.append(fixed)
                        if pending:
                            block = ("\n".join(pending) + "\n\n").encode()
                            chunks.append(block)
                            yield block
                        return
                except httpx.HTTPError as exc:
                    if chunks:
                        # 已经吐过内容再重试，前端会看到重复输出。只能就此收尾。
                        log.warning("stream broke after partial output: %s", exc)
                        return
                    err = f"{type(exc).__name__}: {exc}"
                    log.warning("stream connect failed %s: %s", url, err)
                    continue
            # 兜底也要是合法的 assistant 文本，不能只丢一个 error 对象：
            # 前端拿到没有 choices 的块会直接崩，用户看到的是一句莫名其妙的技术错误。
            yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': f'[上游出错] {err}'}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            if user_id and chunks:
                reply = _sse_text(chunks)
                asyncio.create_task(extract_and_store(user_id, x_umf_agent, user_text, reply))

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={**resp_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ UMF 接口

@app.get("/api/umf/session")
async def session_state(request: Request):
    """登录态还在不在？给前端做「被移出」的即时感知。

    不签发任何东西，也不当鉴权用 —— 只是把 mg.session_alive 的结论透出去。
    Mongo 不可用时一律返回 ok（宁可不踢，也不能因为库抖一下把所有人踢下线）。
    """
    if not mg.ready():
        return {"ok": True, "checked": False}
    uid = await asyncio.to_thread(mg.session_alive, request.cookies.get("refreshToken") or "")
    if not uid:
        raise HTTPException(401, "登录已失效")
    return {"ok": True, "checked": True}


@app.get("/api/umf/profile")
async def get_profile(uid: str = Depends(current_user)):
    return {"user_id": uid, "profile": await store.get_profile(uid)}


@app.patch("/api/umf/profile")
async def patch_profile(request: Request, uid: str = Depends(current_user)):
    """改画像。两种用法：

    `{"section": "traits", "items": [{"k","v"},…]}` —— 整组替换（前端用这个）
    `{...整个 profile 对象...}`                      —— 整体覆盖（兼容旧调用）
    """
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "profile must be an object")

    section = data.get("section")
    if section in ("basics", "traits", "interests", "values", "ongoing"):
        prof = await store.update_profile_section(
            uid,
            section=section,
            items=data.get("items"),
            headline=data.get("headline"),
            note=str(data.get("note") or ""),
        )
        return {"ok": True, "profile": prof}

    version = await store.set_profile(uid, data)
    return {"ok": True, "version": version}


@app.get("/api/umf/memories")
async def list_memories(
    scope: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    uid: str = Depends(current_user),
):
    return {"items": await store.list_memories(uid, scope=scope, q=q, limit=min(limit, 200))}


@app.post("/api/umf/memories")
async def create_memory(request: Request, uid: str = Depends(current_user)):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    memory_id = await store.add_memory(
        uid,
        text,
        scope=body.get("scope", "global"),
        agent_id=body.get("agent_id"),
        kind=body.get("kind", "fact"),
        layer=body.get("layer"),
        entities=body.get("entities"),
        source={"platform": "manual"},
    )
    # 手写一条就算变动一次。攒够 PROFILE_REFRESH_EVERY 条会自动重算画像，
    # 也就是说用户在记忆面板里加的东西，最终会反映到画像上 —— 而且因为
    # _build_profile 把手写条目单独成组排在最前，它一定进得去。
    refreshed = await memory_build.maybe_refresh_profile(uid)
    return {"id": memory_id, "profile_refresh_queued": refreshed}


@app.delete("/api/umf/memories/{memory_id}")
async def delete_memory(memory_id: str, uid: str = Depends(current_user)):
    if not await store.delete_memory(uid, memory_id):
        raise HTTPException(404, "not found")
    # 删掉的记忆不该继续留在画像里 —— 门槛压到 2：删一条可能就抽掉了画像的唯一依据，
    # 按"攒够 8 条"算的话，用户会看到"我明明删了，画像里还在说这件事"。
    await memory_build.maybe_refresh_profile(uid, threshold=2)
    return {"ok": True}


@app.patch("/api/umf/memories/{memory_id}")
async def edit_memory(memory_id: str, request: Request, uid: str = Depends(current_user)):
    """改一条记忆的正文或分层。

    改完会自动重算向量（不然召回还按老内容走），并把"原文 → 新文"
    记进更正表：下次整理时提示词里会带上"他更正过这个"。
    """
    body = await request.json()
    try:
        got = await store.update_memory(
            uid,
            memory_id,
            text=body.get("text"),
            layer=body.get("layer"),
            note=str(body.get("note") or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not got:
        raise HTTPException(404, "not found")
    await memory_build.maybe_refresh_profile(uid, threshold=2)
    return {"ok": True, **got}


# ---------------------------------------------------------------- 更正记录

@app.get("/api/umf/corrections")
async def list_corrections(uid: str = Depends(current_user)):
    """他做过的所有更正。给他自己看，用于建立"我改了，它记住了"的确定感。"""
    return {"corrections": await store.list_corrections(uid)}


@app.patch("/api/umf/topics/{topic_id}")
async def edit_topic(topic_id: str, request: Request, uid: str = Depends(current_user)):
    body = await request.json()
    got = await store.update_topic(
        uid,
        topic_id,
        title=body.get("title"),
        summary=body.get("summary"),
        note=str(body.get("note") or ""),
    )
    if not got:
        raise HTTPException(404, "not found")
    return {"ok": True, **got}


@app.delete("/api/umf/topics/{topic_id}")
async def remove_topic(topic_id: str, uid: str = Depends(current_user)):
    if not await store.delete_topic(uid, topic_id):
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.patch("/api/umf/digests/{conversation_id}")
async def edit_digest(conversation_id: str, request: Request, uid: str = Depends(current_user)):
    """改时间线上某一天里某段对话的标题/一句话摘要。"""
    body = await request.json()
    got = await store.update_digest(
        uid,
        conversation_id,
        title=body.get("title"),
        one_line=body.get("one_line"),
        note=str(body.get("note") or ""),
    )
    if not got:
        raise HTTPException(404, "not found")
    return {"ok": True, **got}


@app.get("/api/umf/timeline")
async def timeline(limit: int = 100, uid: str = Depends(current_user)):
    return {"items": await store.timeline(uid, min(limit, 500))}


@app.post("/api/umf/recall")
async def recall_preview(request: Request, uid: str = Depends(current_user)):
    """调试用：看看"这句话会召回什么"。记忆中心里做成"为什么它这样回答"的证据链。

    必须和真实链路一样先取必带层再补检索 —— 之前这里只调 search_memories，
    于是问"我该不该继续喜欢她"会显示"无命中"，而实际发给模型的上下文里
    明明带着"暗恋一位长发淑女"。调试视图说谎比没有调试视图更糟：
    看到空结果的人会去改阈值，而问题根本不在阈值。
    """
    body = await request.json()
    query = (body.get("query") or "").strip()
    agent_id = body.get("agent_id")

    core = await store.get_core_memories(uid, agent_id=agent_id)
    try:
        hits = await store.search_memories(
            uid, query, agent_id=agent_id, k=settings.memory_top_k,
            min_score=settings.memory_min_score,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("recall preview failed: %s", exc)
        hits = []

    seen = {c.get("text") for c in core}
    merged = core + [h for h in hits if h.get("text") not in seen][: settings.memory_top_k]
    profile = await store.get_profile(uid)
    return {
        "hits": merged,
        "core": len(core),
        "retrieved": len(merged) - len(core),
        "block": context.render_memory_block(profile, merged),
    }


# ------------------------------------------------------------------ 长期记忆板块

@app.get("/api/umf/insight")
async def insight(uid: str = Depends(current_user)):
    """记忆板块的全部内容：画像 / 长期记忆 / 话题 / 时间线 / 压缩摘要。

    和后台 `/api/ops/users/{uid}/insight` 走同一个函数（memory_build.insight），
    唯一差别是这里不带 review —— 那是对人的内部评价，不该给他自己看。
    """
    return await memory_build.insight(uid)


@app.post("/api/umf/memory/rebuild")
async def rebuild_memory(request: Request, uid: str = Depends(current_user)):
    """把历史对话整理成记忆。立刻返回 job_id，进度看下面的接口。

    同一时间只允许一个任务：重复点按钮不该把 414 个对话跑两遍，
    既费钱也会让两个任务互相覆盖同一批 convo 的 digest。
    """
    body = {}
    try:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        body = {}

    cur = await memory_build.job_status(uid)
    if cur and cur["status"] == "running":
        return {"job_id": cur["id"], "running": True, "job": cur}

    job_id = await memory_build.rebuild(uid, force=bool(body.get("force")))
    return {"job_id": job_id, "running": True, "job": await memory_build.job_status(uid)}


@app.get("/api/umf/memory/job")
async def memory_job(uid: str = Depends(current_user)):
    return {"job": await memory_build.job_status(uid)}


# ------------------------------------------------------------------ 记忆中心（主页面）

_PAGE = Path(__file__).with_name("memory.html")


@app.get("/memory", response_class=HTMLResponse)
async def memory_page():
    """记忆板块。故意做成独立页而不是塞进设置：
    记忆是"资产"，设置是"配置"，两者混在一起会让人找不到导入导出。"""
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 导入（从别的聊天搬记忆）

# 预览结果暂存在内存里，等用户点"写入"。进程重启就丢 —— 这是故意的：
# 半截的导入批次比没有更危险，用户重新点一次就行。
_PENDING: dict[str, dict[str, Any]] = {}


async def _extract_from_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for ch in chunks:
        got = await extract([{"role": "user", "content": ch["text"]}])
        for g in got:
            g["origin"] = ch["title"]
            items.append(g)
    # 批内去重：同一个事实在不同会话里出现很常见
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for it in items:
        key = it["text"].strip()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


@app.post("/api/umf/import")
async def import_memories(
    file: UploadFile = File(...),
    dry_run: int = 1,
    uid: str = Depends(current_user),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(413, "文件太大（上限 60MB）。ChatGPT 的完整导出包请只取 conversations.json")

    try:
        platform, convs = importers.collect(raw, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    chunks = importers.chunks_for_extract(convs)
    if not chunks:
        raise HTTPException(400, "文件里没有可用的对话内容")

    items = await _extract_from_chunks(chunks)
    batch = uuid.uuid4().hex[:12]
    stats = {
        "platform": platform,
        "conversations": len(convs),
        "chunks": len(chunks),
        "extracted": len(items),
        "file": file.filename,
    }

    if dry_run:
        _PENDING[batch] = {"items": items, "stats": stats, "user_id": uid, "at": time.time()}
        return {
            "dry_run": True,
            "batch": batch,
            "stats": stats,
            "samples": items[:12],
        }

    return await _commit_import(batch, uid, items, stats)


async def _commit_import(batch: str, uid: str, items: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, Any]:
    platform = stats.get("platform") or "import"
    await db.execute(
        """INSERT INTO import_batches (id, user_id, platform, file_name, status, stats)
             VALUES ($1,$2,$3,$4,'running',$5::jsonb)
           ON CONFLICT (id) DO NOTHING""",
        batch,
        uid,
        platform,
        stats.get("file"),
        json.dumps(stats, ensure_ascii=False),
    )
    written = 0
    for it in items:
        try:
            await store.add_memory(
                uid,
                it["text"],
                scope="global",
                kind=it.get("kind") or "fact",
                layer=it.get("layer"),
                entities=it.get("entities"),
                confidence=it.get("confidence") or 0.8,
                valid_from=it.get("valid_from"),
                source={"platform": platform, "batch": batch, "title": it.get("origin")},
                import_batch=batch,
            )
            written += 1
        except Exception as exc:
            log.warning("import item failed: %s", exc)
    await db.execute(
        "UPDATE import_batches SET status='done', stats=$2::jsonb WHERE id=$1",
        batch,
        json.dumps({**stats, "written": written}, ensure_ascii=False),
    )
    return {"dry_run": False, "batch": batch, "stats": {**stats, "written": written}}


@app.post("/api/umf/import/commit")
async def commit_import(request: Request, uid: str = Depends(current_user)):
    body = await request.json()
    batch = str(body.get("batch") or "")
    pending = _PENDING.pop(batch, None)
    if not pending or pending["user_id"] != uid:
        raise HTTPException(404, "预览已过期，请重新上传")
    return await _commit_import(batch, uid, pending["items"], pending["stats"])


@app.post("/api/umf/import/direct")
async def import_direct(file: UploadFile = File(...), uid: str = Depends(current_user)):
    """一步到位：解析 + 抽取 + 写入。给"我确定要导"的场景用。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    try:
        platform, convs = importers.collect(raw, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    chunks = importers.chunks_for_extract(convs)
    items = await _extract_from_chunks(chunks)
    stats = {
        "platform": platform,
        "conversations": len(convs),
        "chunks": len(chunks),
        "extracted": len(items),
        "file": file.filename,
    }
    return await _commit_import(uuid.uuid4().hex[:12], uid, items, stats)


# ------------------------------------------- 把备份写成"能继续聊的历史对话"

# 这一段是本功能的核心，也是全项目**唯一**会写 LibreChat Mongo 的地方。
# 其它所有后台代码都遵守"只读旁观"。这里的例外经过权衡：
#   · 用户要的是"导入的对话能接着聊"，LibreChat 没有批量导入接口
#   · 所以只写 conversations / messages 两个集合，且每条都带 umfImport 标记
#   · 提供 purge 接口，随时能把导入的东西全部撤掉


@app.post("/api/umf/history/import")
async def import_history(
    file: UploadFile = File(...),
    dry_run: int = 1,
    resync: int = 0,
    uid: str = Depends(current_user),
):
    """把备份里的对话写成历史对话。

    dry_run=1 只解析、不写库，返回"会导入多少个会话/消息"供用户确认。
    为什么默认 dry_run：一份 DeepSeek 备份可能有几十个会话，
    直接灌进去会把侧边栏刷满，必须先让用户看清代价。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    if len(raw) > 200 * 1024 * 1024:
        raise HTTPException(413, "文件太大（上限 200MB）")

    try:
        platform, convs = importers.collect(raw, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not convs:
        raise HTTPException(400, "备份里没有可导入的对话")

    total_msgs = sum(len(c.get("messages") or []) for c in convs)
    preview = [
        {
            "title": c.get("title"),
            "messages": len(c.get("messages") or []),
            "created": c.get("created"),
            "first": ((c.get("messages") or [{}])[0].get("text") or "")[:120],
        }
        for c in convs[:15]
    ]
    stats = {
        "platform": platform,
        "conversations": len(convs),
        "messages": total_msgs,
        "file": file.filename,
    }

    if dry_run:
        return {"dry_run": True, "stats": stats, "preview": preview}

    batch = uuid.uuid4().hex[:12]
    if not mg.ready():
        raise HTTPException(503, "后台未连上数据库，暂时不能写入历史对话")

    try:
        result = await asyncio.to_thread(
            importer_mongo.write_conversations,
            uid,
            convs,
            platform=platform,
            batch=batch,
            source=file.filename or "",
            # resync=1：对已导入过的对话重放一遍（沿用原 conversationId），
            # 用来补上解析规则升级后才支持的内容（AI 生成图、txt/js 附件）。
            resync=bool(resync),
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    await db.execute(
        """INSERT INTO import_batches (id, user_id, platform, file_name, status, stats)
             VALUES ($1,$2,$3,$4,'done',$5::jsonb)
           ON CONFLICT (id) DO NOTHING""",
        batch,
        uid,
        platform,
        file.filename,
        json.dumps({**stats, **result, "kind": "history"}, ensure_ascii=False),
    )
    return {"dry_run": False, "batch": batch, "stats": stats, "result": result}


@app.get("/api/umf/history")
async def list_history(uid: str = Depends(current_user)):
    """已导入的历史对话（用于展示与回滚）。"""
    if not mg.ready():
        return {"items": [], "mongo": False}
    items = await asyncio.to_thread(importer_mongo.list_imported, uid)
    return {"items": items, "mongo": True, "count": len(items)}


@app.delete("/api/umf/history")
async def purge_history(
    batch: Optional[str] = None,
    conversation_id: Optional[str] = None,
    uid: str = Depends(current_user),
):
    """撤销导入：只删带 umfImport 标记的记录，用户自己聊的对话不受影响。

    三种粒度：
      · 不带参数          → 撤销全部导入
      · ?batch=xxx        → 撤销某一批
      · ?conversation_id= → 撤销单个对话
    """
    if not mg.ready():
        raise HTTPException(503, "后台未连上数据库")
    got = await asyncio.to_thread(
        importer_mongo.purge_imported, uid, batch, conversation_id
    )
    return {"ok": True, **got}


@app.post("/api/umf/history/paste")
async def import_history_paste(request: Request, uid: str = Depends(current_user)):
    """粘贴文本导入。豆包/Kimi/通义大多数时候只能复制粘贴 ——
    这是它们最常见的"导出"方式，不能只支持文件。"""
    body = await request.json()
    text = str(body.get("text") or "")
    if len(text.strip()) < 10:
        raise HTTPException(400, "内容太短")
    try:
        platform, convs = importers.collect(text.encode("utf-8"), "paste.txt")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not convs:
        raise HTTPException(400, "没解析出对话")

    batch = uuid.uuid4().hex[:12]
    if not mg.ready():
        raise HTTPException(503, "后台未连上数据库")
    result = await asyncio.to_thread(
        importer_mongo.write_conversations,
        uid,
        convs,
        platform=platform,
        batch=batch,
        source="pasted",
    )
    return {"ok": True, "batch": batch, "result": result}


# ------------------------------------------------------------------ 导出（把记忆带走）

@app.get("/api/umf/export")
async def export_memories(
    format: str = "json",
    include: str = "all",
    uid: str = Depends(current_user),
):
    """导出全部资产。

    为什么要把"对话记录"也导出来：
      原来这里只导 profile / memories / events —— 但那三样在早期站点上几乎都是空的
      （记忆条目要用户聊够久才会长出来）。用户真正的资产是**聊天记录本身**，
      包括从别的平台搬过来的那些。只导记忆等于"备份了个空壳"。

    include 可取值：
      all            画像 + 记忆 + 时间线 + 全部对话（默认）
      conversations  只导对话
      memories       只导记忆与画像（等于旧行为）
    """
    only_convs = include == "conversations"
    with_convs = include in ("all", "conversations")

    profile = {} if only_convs else await store.get_profile(uid)
    memories = [] if only_convs else await store.list_memories(uid, limit=5000)
    events = [] if only_convs else await store.timeline(uid, 1000)

    conv_data = {"conversations": [], "conversationsCount": 0, "messagesCount": 0}
    conv_error = None
    if with_convs:
        try:
            conv_data = await asyncio.to_thread(exporter_mongo.export_conversations, uid)
        except Exception as exc:  # noqa: BLE001
            # 对话读不出来时不要整个导出失败：至少把记忆给用户
            log.warning("export conversations failed: %s", exc)
            conv_error = str(exc)

    convs = conv_data["conversations"]

    if format == "md":
        lines = [
            "# UMF 全量导出",
            "",
            f"用户：{uid}",
            f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC",
            "",
            f"- 画像：{'有' if profile else '无'}",
            f"- 记忆条目：{len(memories)}",
            f"- 对话：{conv_data['conversationsCount']} 个 / {conv_data['messagesCount']} 条消息",
            "",
        ]
        if profile:
            lines += ["## 画像", "", "```json",
                      json.dumps(profile, ensure_ascii=False, indent=2), "```", ""]
        if memories:
            lines += ["## 记忆条目", ""]
            for m in memories:
                layer = m.get("layer") or m.get("kind") or ""
                lines.append(f"- [{layer}] {m['text']}")
            lines.append("")
        if events:
            lines += ["## 时间线", ""]
            for e in events:
                at = e["at"].strftime("%Y-%m-%d") if e.get("at") else ""
                lines.append(f"- {at} {e['type']}：{e.get('summary') or ''}")
            lines.append("")

        if convs:
            lines += ["# 聊天记录", ""]
            for c in convs:
                tag = "（导入）" if c["source"] == "import" else ""
                lines += ["---", "", f"## {c['title']}{tag}", ""]
                if c.get("created"):
                    lines.append(f"*{c['created'][:19].replace('T', ' ')}*")
                    lines.append("")
                for m in c["messages"]:
                    who = "我" if m["role"] == "user" else "助手"
                    lines.append(f"**{who}：**")
                    lines.append("")
                    lines.append(m["text"])
                    lines.append("")
        return Response(
            content="\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="umf-export.md"'},
        )

    payload = {
        # 升到 umf/2：多了 conversations 字段。回灌端两个版本都认。
        "format": "umf/2",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user_id": uid,
        "stats": {
            "memories": len(memories),
            "events": len(events),
            "conversations": conv_data["conversationsCount"],
            "messages": conv_data["messagesCount"],
        },
        "profile": profile or None,
        "memories": [
            {
                "text": m["text"],
                "layer": m.get("layer"),
                "kind": m.get("kind"),
                "scope": m.get("scope"),
                "entities": m.get("entities") or [],
                "confidence": m.get("confidence"),
                "created_at": m["created_at"].isoformat() if m.get("created_at") else None,
            }
            for m in memories
        ],
        "events": [
            {
                "at": e["at"].isoformat() if e.get("at") else None,
                "type": e["type"],
                "summary": e.get("summary"),
            }
            for e in events
        ],
        "conversations": convs,
    }
    if conv_error:
        payload["conversations_error"] = conv_error
        payload["conversations_note"] = "对话记录读取失败，其余内容仍可恢复"

    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="umf-export.json"'},
    )


@app.post("/api/umf/import/umf")
async def import_umf(file: UploadFile = File(...), uid: str = Depends(current_user)):
    """回灌自己导出的 umf 文件 —— 换站/换机器时用。

    同时兼容两个版本：
      umf/1  只有画像 + 记忆（旧导出）
      umf/2  多了 conversations（本轮新增，能恢复聊天记录）

    对话走 importer_mongo.write_conversations，靠每条记录里的 id
    （内容哈希）做幂等 —— 同一份文件导两次不会变成两份。
    """
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise HTTPException(400, "文件结构不对")
    fmt = str(data.get("format") or "")
    if fmt not in ("umf/1", "umf/2"):
        raise HTTPException(400, "不是 UMF 导出文件（缺少 format: umf/1 或 umf/2）")

    # ---- 画像 ----
    profile = data.get("profile") or {}
    if profile:
        await store.set_profile(uid, profile)

    # ---- 记忆条目 ----
    written = 0
    batch = uuid.uuid4().hex[:12]
    for m in data.get("memories") or []:
        if not isinstance(m, dict):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        try:
            await store.add_memory(
                uid,
                text,
                scope=m.get("scope") or "global",
                kind=m.get("kind") or "fact",
                layer=m.get("layer"),
                entities=m.get("entities"),
                confidence=m.get("confidence") or 0.8,
                source={"platform": "umf-export", "batch": batch},
                import_batch=batch,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("umf import item failed: %s", exc)

    # ---- 对话（umf/2 才有）----
    conv_res = {"conversations": 0, "messages": 0, "skippedCount": 0}
    convs = data.get("conversations") or []
    if convs:
        if not mg.ready():
            raise HTTPException(503, "后台未连上数据库，暂时无法恢复对话")
        # 转成 importers 的中间结构，复用同一套写入逻辑
        norm = []
        for c in convs:
            if not isinstance(c, dict):
                continue
            msgs = [
                {"role": m.get("role"), "text": (m.get("text") or "")}
                for m in (c.get("messages") or [])
                if isinstance(m, dict) and (m.get("text") or "").strip()
            ]
            if not msgs:
                continue
            norm.append(
                {
                    "title": c.get("title") or "未命名对话",
                    "messages": msgs,
                    "created": c.get("created"),
                    "updated": c.get("updated"),
                    # 用导出文件里的内容哈希做幂等键，重复导入不会翻倍
                    "source_id": c.get("id") or "",
                }
            )
        if norm:
            try:
                conv_res = await asyncio.to_thread(
                    importer_mongo.write_conversations,
                    uid,
                    norm,
                    platform="umf-restore",
                    batch=batch,
                    source="umf-export",
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc

    return {
        "ok": True,
        "format": fmt,
        "written": written,
        "profile": bool(profile),
        "conversations": conv_res,
    }


# ------------------------------------------------------------------ 头像

_ALLOWED_IMAGE = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@app.post("/api/umf/avatar")
async def upload_avatar(file: UploadFile = File(...), kind: str = "user", uid: str = Depends(current_user)):
    ct = (file.content_type or "").split(";")[0].strip().lower()
    if ct not in _ALLOWED_IMAGE:
        raise HTTPException(400, "只支持 PNG / JPEG / WebP / GIF")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "头像不能超过 8MB")

    key = avatars.key_for_user(uid, data, _ALLOWED_IMAGE[ct])
    url = avatars.put(data, key, ct)
    await db.execute(
        """INSERT INTO umf_avatars (key, owner_id, kind, url, bytes) VALUES ($1,$2,$3,$4,$5)
           ON CONFLICT (key) DO UPDATE SET url = EXCLUDED.url, created_at = now()""",
        key,
        uid,
        "agent" if kind == "agent" else "user",
        url,
        len(data),
    )
    return {"url": url, "key": key, "backend": avatars.backend()}


@app.get("/api/umf/avatar/backend")
async def avatar_backend(uid: str = Depends(current_user)):
    return {"backend": avatars.backend(), "r2": settings.r2_ready}


@app.get("/api/umf/avatar/file/{key:path}")
async def avatar_file(key: str):
    """本地后端的读取口。R2 后端不会走到这里（URL 直接指向公开域名）。"""
    got = avatars.get(key)
    if not got:
        raise HTTPException(404, "not found")
    data, ctype = got
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/api/umf/avatar/bulk")
async def bulk_avatar(request: Request, x_admin: Optional[str] = Header(default=None)):
    """批量给智能体换头像（DiceBear 重刷用）。只要 ADMIN_TOKEN，不需要用户 JWT。"""
    if not settings.admin_token or x_admin != settings.admin_token:
        raise HTTPException(401, "admin token required")
    body = await request.json()
    items = body.get("items") or []
    done = 0
    for it in items:
        agent_id = str(it.get("agent_id") or "")
        svg = str(it.get("svg") or "")
        if not agent_id or not svg.startswith("<svg"):
            continue
        key = avatars.key_for_agent(agent_id)
        url = avatars.put(svg.encode(), key, "image/svg+xml")
        await db.execute(
            """INSERT INTO umf_avatars (key, owner_id, kind, url, bytes) VALUES ($1,$2,'agent',$3,$4)
               ON CONFLICT (key) DO UPDATE SET url = EXCLUDED.url, created_at = now()""",
            key,
            agent_id,
            url,
            len(svg),
        )
        done += 1
    return {"ok": True, "written": done, "backend": avatars.backend()}
