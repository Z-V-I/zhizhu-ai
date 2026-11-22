"""把备份里的图片/附件搬进 LibreChat 的图片目录。

## 为什么要单独一个模块

LibreChat 的图片和消息是两处存储：
  · 文件本体 → /app/client/public/images/<userId>/<file_id>__<filename>
  · 元信息   → 消息文档里的 files 数组

两者必须成对写入。只写 files 数组、文件不存在 → 前端裂图；
只放文件、不写数组 → 图在服务器上但对话里看不到。

`images` 目录已与 api 容器共享（docker-compose 里的 librechat_images 卷），
所以这里写进去，api 的静态路由立刻就能读。

## 命名规则

照抄 LibreChat 自己的写法：`<uuid>__<原文件名>`。
用双下划线分隔是它的约定，别改成别的，否则前端的解析可能对不上。
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("brain.images")

# 与 docker-compose 里 api/brain 共同的挂载点一致
IMAGES_ROOT = Path(os.getenv("CHAT_IMAGES_DIR", "/app/client/public/images"))

# 能直接当图片显示的格式
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# 纯文本类：这些能直接把内容塞进 files 记录的 `text` 字段，
# LibreChat 的预览弹窗读的就是它（GET /api/files/:id/preview）。
# 只搬图片的话，聊天记录里出现过的大量 .txt / .js / .md 会整批消失 ——
# 用户看到的是一段"我发了文件"的文字，却点不开任何东西。
TEXT_MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".yml": "text/yaml",
    ".yaml": "text/yaml",
    ".xml": "text/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".scss": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".py": "text/x-python",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".java": "text/x-java",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++",
    ".sh": "text/x-sh",
    ".sql": "text/x-sql",
    ".php": "text/x-php",
    ".rb": "text/x-ruby",
    ".toml": "text/plain",
    ".ini": "text/plain",
    ".env": "text/plain",
    ".cursorrules": "text/plain",
    ".gitignore": "text/plain",
}

# 文本预览最多存多少字。LibreChat 的弹窗就是「读一段文本」，
# 几百 KB 的日志全塞进 Mongo 会把文档撑爆，所以截断并标注。
MAX_TEXT_PREVIEW = 200_000

# 单个文件上限。LibreChat 默认上限也是这个量级，超了就不搬，
# 免得把一个 100MB 的视频塞进卷里。
MAX_BYTES = 8 * 1024 * 1024


def _safe_name(name: str) -> str:
    """清理文件名，只保留安全字符。

    为什么要清：备份里的文件名可能带中文、空格、括号，
    直接拼进 URL 会需要编码，前端某些地方处理不好就裂图。
    保留中日韩字母（它们 URL 编码后也是稳定的），其它一律换成下划线。
    """
    base = os.path.basename(name or "")
    base = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", base)
    base = base.strip(". ")
    if not base:
        base = "file"
    # 太长的名字会超过文件系统限制
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:100] + "." + ext) if dot else base[:120]
    return base


def save(user_id: str, name: str, data: bytes) -> Optional[dict[str, Any]]:
    """保存一个附件，返回可直接放进 message.files 的记录。

    返回 None 表示跳过（太小太大、或者不认识的类型）。
    """
    if not data:
        return None
    if len(data) > MAX_BYTES:
        log.info("skip attachment %s: %s bytes > limit", name, len(data))
        return None

    safe = _safe_name(name)
    ext = os.path.splitext(safe)[1].lower()
    mime = IMAGE_MIME.get(ext)
    is_text = False

    # 图片走"内嵌显示"，文本类走"附件 + 预览弹窗"。除此之外的二进制
    # （xlsx/zip/pdf…）LibreChat 需要走它自己的解析链路，硬塞只会变成
    # 一个点不开的壳子，所以仍然跳过 —— 但会记一条日志，别静默。
    if not mime:
        mime = TEXT_MIME.get(ext)
        is_text = bool(mime)
    if not mime:
        # 没有扩展名的情况（ChatGPT 的 file_xxx）：交给 save_any 去嗅探
        log.debug("skip attachment with unknown type: %s", name)
        return None

    file_id = str(uuid.uuid4())
    dir_path = IMAGES_ROOT / user_id
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot create image dir %s: %s", dir_path, exc)
        return None

    target = dir_path / f"{file_id}__{safe}"
    try:
        target.write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("write image failed %s: %s", target, exc)
        return None

    rec: dict[str, Any] = {
        "type": mime,
        "file_id": file_id,
        # 这个路径是相对站点根的，LibreChat 前端直接拿它当 src
        "filepath": f"/images/{user_id}/{file_id}__{safe}",
        "filename": safe,
        "embedded": False,
        "metadata": {"destinationChosen": False},
        "bytes": len(data),
    }
    if is_text:
        # 预览弹窗读的就是这两个字段（见 api/server/routes/files/files.js
        # GET /:file_id/preview）。没有它，文本附件点开是空白。
        text = _decode_text(data)
        if text is not None:
            rec["text"] = text[:MAX_TEXT_PREVIEW]
            rec["textFormat"] = "text"
            rec["status"] = "ready"
    return rec


def _decode_text(data: bytes) -> Optional[str]:
    """把文本文件的字节解码成字符串；不像文本就返回 None。

    UTF-8 优先，再退 GB18030（国内导出的 txt 大量是 GBK）。

    ★ 不加 latin-1 兜底。latin-1 能"解码"任何字节，
      于是 xlsx / zip / 图片都会被判成文本，然后被存成一个内容全是乱码的 .txt ——
      附件列表中会出现一堆打不开的假文本。宁可返回 None 让它被跳过。
    """
    if not data:
        return None
    # 二进制探测：NUL 字节基本只出现在非文本里
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def sniff_ext(data: bytes, fallback: str = ".jpg") -> str:
    """按文件头判断真实类型。

    为什么需要：ChatGPT 导出包里所有附件都叫 `file_xxx.dat`，
    按扩展名判断一律会被当成"不认识的类型"跳过 ——
    这正是"AI 生成的图片导进来全没了"的直接原因。
    文件头是唯一可信的线索。
    """
    if not data:
        return fallback
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return fallback


def save_any(user_id: str, name: str, data: bytes) -> Optional[dict[str, Any]]:
    """按内容识别类型后再存。给 `.dat` / `file_xxx` 这种没有扩展名线索的来源用。

    顺序：文件头认图片 → 认不出就当文本存成 .txt → 都不行才跳过。
    这一条是"AI 生成图和 txt 全都导不进来"的总闸门：
    ChatGPT 附件一律叫 `file_xxx.dat`，按名字判类型必然全灭。
    """
    if not data:
        return None
    base = _safe_name(name or "file")
    stem, dot, ext = base.rpartition(".")
    if not dot or not ext:
        # 没有扩展名：先按文件头猜图片，猜不出就当文本
        sniffed = sniff_ext(data, fallback="")
        if sniffed:
            return save(user_id, f"{base}{sniffed}", data)
        text = _decode_text(data)
        if text is None:
            return None
        return save(user_id, f"{base}.txt", data)
    # 有扩展名但类型不认识时，如果内容是文本，就按 .txt 存
    rec = save(user_id, base, data)
    if rec:
        return rec
    text = _decode_text(data)
    if text is None:
        return None
    return save(user_id, f"{stem}.txt", data)


def file_record(
    user_id: str,
    saved: dict[str, Any],
    *,
    conversation_id: str,
    message_id: str,
    source: str = "local",
) -> dict[str, Any]:
    """把 save() 的结果补成 LibreChat `files` 集合里的一条记录。

    ## 为什么必须写这张表

    LibreChat 的"附加文件"面板走 `GET /api/files` → 读 Mongo 的 `files` 集合，
    **不看 message.files**。我们原来只把附件塞进 message.files，
    于是：图片在消息里（用户侧）能显示，但"文件"面板永远是空的 ——
    用户会以为附件根本没导进来。

    字段照 `TFile` 补齐（packages/data-provider/src/types/files.ts），
    另外两个**必须照 Mongo schema 写**，否则查不出来（踩过）：

      · `user` 必须是 **ObjectId**。schema 里写的是
        `type: mongoose.Schema.Types.ObjectId`，而 `getFiles({user})` 用
        同样类型去匹配 —— 存成字符串就永远查不到，接口返回空数组，
        表现为"文件面板还是空的"，但库里明明有数据。
      · 字段名是 **`messageId`**，不是 `message`。
    """
    try:
        import bson

        uid: Any = bson.ObjectId(user_id) if len(user_id) == 24 else user_id
    except Exception:  # noqa: BLE001
        uid = user_id
    return {
        "user": uid,
        "conversationId": conversation_id,
        "messageId": message_id,
        "file_id": saved["file_id"],
        "filename": saved["filename"],
        "filepath": saved["filepath"],
        "type": saved["type"],
        "bytes": saved.get("bytes") or 0,
        "embedded": False,
        "object": "file",
        "usage": 0,
        "context": "message_attachment",
        "source": source,
        "metadata": {"destinationChosen": False},
        "createdAt": datetime.now(timezone.utc),
        # 有文本内容的（txt/js/md…）一并带过去，预览弹窗要用
        **(  # type: ignore[dict-item]
            {"text": saved["text"], "textFormat": saved.get("textFormat", "text"), "status": "ready"}
            if saved.get("text") is not None
            else {}
        ),
    }


def delete_for_conversations(user_id: str, files: list[dict[str, Any]]) -> int:
    """删对话时顺带清掉它的图片，避免卷里积累孤儿文件。"""
    n = 0
    for f in files or []:
        fp = (f or {}).get("filepath") or ""
        if not fp.startswith("/images/"):
            continue
        p = IMAGES_ROOT / fp[len("/images/"):]
        try:
            if p.exists():
                p.unlink()
                n += 1
        except Exception:  # noqa: BLE001
            pass
    return n
