"""把别的聊天软件导出的数据，变成 UMF 能用的记忆。

为什么要有这一层：用户换站的唯一成本是"历史记忆带不走"。
ChatGPT 导出的是 conversations.json（一棵 mapping 树），豆包/DeepSeek 基本只能
导出纯文本或截图。三种格式统一收敛成同一个中间结构：

    {"title": str, "messages": [{"role": "user|assistant", "text": str}, ...]}

再交给 extract.py 那套已经调好的抽取 prompt。**不在这里造第二套抽取逻辑**：
导入的记忆和聊天里自动长出来的记忆必须是同一种东西，否则检索时会割裂。
"""

from __future__ import annotations

import html as html_mod
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("brain.importers")

MAX_CONVS = 40  # 单次最多处理多少个会话，控制一次导入的 token 成本

# ---------------------------------------------------------------- 解析

def _text_of_parts(content: Any) -> str:
    """ChatGPT 的 content 结构：{content_type, parts:[...]}，parts 里可能是 str 或 dict。"""
    if not isinstance(content, dict):
        return str(content or "")
    parts = content.get("parts")
    if isinstance(parts, list):
        out = []
        for p in parts:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                # 图片 part 没有 text，只有 asset_pointer；跳过，由 _image_pointers 单独取
                out.append(str(p.get("text") or ""))
        return "\n".join(x for x in out if x)
    return str(content.get("text") or "")


def _image_pointers(content: Any) -> list[str]:
    """从 content 里挑出图片资源指针（DALL·E 画的图就在这儿）。

    ChatGPT 导出里，AI 生成的图不是 base64、也不是 URL，而是一条指针：

        {"content_type": "image_asset_pointer",
         "asset_pointer": "sediment://file_0000000036fc722f8ca4fff23169a853",
         "height": 810, ...}

    真正的字节在 zip 里，文件名是 `file_0000000036fc722f8ca4fff23169a853.dat`。
    不去读它的话，导进来的 DALL·E 图就一个都不剩 —— 这是"AI 生成的图片
    没有正确呈现"最直接的原因。
    """
    if not isinstance(content, dict):
        return []
    out: list[str] = []
    for p in content.get("parts") or []:
        if not isinstance(p, dict):
            continue
        if p.get("content_type") != "image_asset_pointer":
            continue
        ptr = str(p.get("asset_pointer") or "")
        # sediment://file_xxx  /  file-service://file_xxx  都归一成 file_xxx
        if "://" in ptr:
            ptr = ptr.split("://", 1)[1]
        if ptr:
            out.append(ptr)
    return out


def _walk_chatgpt_mapping(
    mapping: dict[str, Any], current: str | None = None
) -> list[dict[str, Any]]:
    """mapping 是棵树，取出一条从根到叶的主链。

    ## 为什么不能只靠 children

    2026 年的 ChatGPT 导出里 `children` **全是 null**（实测一份 27 个对话的官方包，
    每个节点 children 都是 None）。只按 children 走，第一步就断在根节点上，
    整份文件解析出 0 条对话，前端看到的是「JSON 里没有可识别的对话结构」。

    可靠的方向是反的：`parent` 一直在，`current_node` 指向当前展示的分支叶子。
    所以从 current_node 沿 parent 回溯到根再反转 —— 顺带天然避开"重新生成"
    留下的废弃分支，比走 children 更准。children 走法保留作兜底（老版本导出没有
    current_node，那时它是对的）。
    """
    if not isinstance(mapping, dict) or not mapping:
        return []

    # ① 首选：从 current_node 沿 parent 回溯
    node_id = current if current in mapping else None
    if not node_id:

        def depth(nid: str) -> int:
            d, cur, guard = 0, nid, 0
            while cur in mapping and guard < 5000:
                cur = (mapping[cur] or {}).get("parent")
                d += 1
                guard += 1
                if not cur:
                    break
            return d

        cand = [k for k, v in mapping.items() if isinstance(v, dict)]
        if cand:
            node_id = max(cand, key=depth)

    if node_id:
        chain: list[dict[str, Any]] = []
        cur, guard = node_id, 0
        while cur in mapping and guard < 5000:
            node = mapping[cur]
            if isinstance(node, dict):
                chain.append(node)
            cur = node.get("parent") if isinstance(node, dict) else None
            guard += 1
        if len(chain) > 1:  # 只有根节点等于没走到，交给兜底
            chain.reverse()
            return chain

    # ② 兜底：老的 children 走法
    root = None
    for node in mapping.values():
        if isinstance(node, dict) and not node.get("parent"):
            root = node
            break
    if root is None:
        root = next(iter(mapping.values()))

    chain = []
    node = root
    seen: set[str] = set()
    while isinstance(node, dict):
        chain.append(node)
        children = [c for c in (node.get("children") or []) if c in mapping and c not in seen]
        if not children:
            break
        seen.update(children)
        node = mapping[children[-1]]  # 最后一个孩子 = 最后一次生成
    return chain


def _ext_from_mime(mime: str) -> str:
    """把 mime 换成一个扩展名，让后续按扩展名判类型那一层能工作。"""
    m = (mime or "").lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/json": ".json",
        "text/csv": ".csv",
        "text/javascript": ".js",
        "text/x-python": ".py",
    }.get(m, "")


def _cg_time(item: dict[str, Any], *keys: str) -> str | None:
    """ChatGPT 的时间是 Unix 秒（带小数）。转成 ISO —— 写库那边认这个。"""
    from datetime import datetime, timezone

    for k in keys:
        v = item.get(k)
        if isinstance(v, (int, float)) and v > 0:
            try:
                return datetime.fromtimestamp(float(v), timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                continue
    return None


def parse_chatgpt(data: Any, fetch_asset=None) -> list[dict[str, Any]]:
    """解析 ChatGPT 的 conversations.json。

    `fetch_asset(name) -> bytes | None`：可选，用来从 zip 里取 AI 生成图的字节。
    传了才搬图；不传就只保留对话文本（老调用方不受影响）。
    """
    convs: list[dict[str, Any]] = []
    items = data if isinstance(data, list) else data.get("conversations") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        mapping = item.get("mapping") or {}
        msgs: list[dict[str, Any]] = []
        for node in _walk_chatgpt_mapping(mapping, item.get("current_node")):
            msg = node.get("message") or {}
            author = (msg.get("author") or {}).get("role")
            if author not in ("user", "assistant"):
                continue
            content = msg.get("content")
            text = _text_of_parts(content)
            atts: list[dict[str, Any]] = []
            seen_ids: set[str] = set()

            if fetch_asset:
                # ① 首选 message.metadata.attachments
                #    这里同时有**原始文件名**和 mime_type，比 pointer 靠谱得多：
                #    实测这个包里 25 条附件，其中 20 个是 text/plain（他发的 txt），
                #    只按 pointer 取图片的话，这些 txt 会被整批丢掉。
                for a in ((msg.get("metadata") or {}).get("attachments") or []):
                    if not isinstance(a, dict):
                        continue
                    fid = str(a.get("id") or "")
                    if not fid or fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                    try:
                        blob = fetch_asset(fid)
                    except Exception:  # noqa: BLE001
                        blob = None
                    if not blob:
                        continue
                    name = str(a.get("name") or fid)
                    # 名字没有扩展名时，用 mime_type 补一个，导出端才能判类型
                    if "." not in name:
                        name += _ext_from_mime(str(a.get("mime_type") or ""))
                    atts.append({"name": name, "data": blob})

                # ② 兜底：老导出没有 metadata.attachments，只有 content 里的指针
                for ptr in _image_pointers(content):
                    if ptr in seen_ids:
                        continue
                    seen_ids.add(ptr)
                    try:
                        blob = fetch_asset(ptr)
                    except Exception:  # noqa: BLE001
                        blob = None
                    if blob:
                        atts.append({"name": ptr.rsplit("/", 1)[-1] or "image", "data": blob})

            if text.strip() or atts:
                m: dict[str, Any] = {"role": author, "text": text.strip()}
                if atts:
                    m["attachments"] = atts
                msgs.append(m)
        if msgs:
            # 时间必须带上：不带的话所有导入对话的 createdAt 都变成"现在"，
            # 时间线会把一年的聊天全堆在今天 —— 用户看到的是"我的历史哪去了"。
            convs.append(
                {
                    "title": item.get("title") or "导入的对话",
                    "messages": msgs,
                    "created": _cg_time(item, "create_time", "update_time"),
                    "updated": _cg_time(item, "update_time", "create_time"),
                    "source_id": item.get("conversation_id") or item.get("id"),
                }
            )
        if len(convs) >= MAX_CONVS:
            break
    return convs


def parse_generic(data: Any) -> list[dict[str, Any]]:
    """宽容格式：[{title, messages:[{role, content}]}] 或 {conversations:[...]}。"""
    items = data if isinstance(data, list) else (data.get("conversations") or data.get("data") or [])
    convs: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        raw_msgs = item.get("messages") or item.get("chat_messages") or []
        msgs: list[dict[str, str]] = []
        for m in raw_msgs if isinstance(raw_msgs, list) else []:
            if not isinstance(m, dict):
                continue
            role = m.get("role") or m.get("sender") or "user"
            if role in ("human", "user"):
                role = "user"
            elif role in ("bot", "ai", "assistant", "gpt"):
                role = "assistant"
            else:
                continue
            text = m.get("content") or m.get("text") or m.get("message") or ""
            if isinstance(text, dict):
                text = _text_of_parts(text)
            text = str(text).strip()
            if text:
                msgs.append({"role": role, "text": text})
        if msgs:
            convs.append({"title": item.get("title") or item.get("name") or "导入的对话", "messages": msgs})
        if len(convs) >= MAX_CONVS:
            break
    return convs


_SPEAKER = re.compile(r"^\s*(用户|我|user|human|助手|ai|assistant|gpt|bot)\s*[:：]\s*", re.I)


def parse_text(text: str) -> list[dict[str, Any]]:
    """纯文本对话：靠 `用户:` / `助手:` 这类前缀切分。

    没有前缀就当一整段"用户自述"，也照样能抽出身份/偏好 —— 这是豆包用户
    最常见的导出形态（复制粘贴）。
    """
    msgs: list[dict[str, str]] = []
    role = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _SPEAKER.match(line)
        if m:
            if buf and role:
                body = "\n".join(buf).strip()
                if body:
                    msgs.append({"role": role, "text": body})
            raw = m.group(1).lower()
            role = "user" if raw in ("用户", "我", "user", "human") else "assistant"
            buf = [_SPEAKER.sub("", line)]
        else:
            buf.append(line)
    if buf and role:
        body = "\n".join(buf).strip()
        if body:
            msgs.append({"role": role, "text": body})
    if not msgs:
        msgs = [{"role": "user", "text": text.strip()}]
    return [{"title": "文本导入", "messages": msgs}]


# ---------------------------------------------------------------- 豆包导出

def parse_doubao(data: Any, fallback_title: str = "") -> list[dict[str, Any]]:
    """豆包导出的单会话 JSON：{"messages":[{id, role, content}]}

    实测结构（2026-11 版）：
        {"messages": [
            {"id": "62497586502210306", "role": "user",      "content": "..."},
            {"id": "62497586502218498", "role": "assistant", "content": "..."}
        ]}

    特点：
      · **没有标题字段** —— 豆包是「一个文件一个会话」，所以从文件名取标题
      · role 直接就是 user / assistant（不像别家要映射 human/bot）
      · 没有时间戳，只能用导入时间

    返回空的 list 表示"这不是豆包格式"，让 collect() 继续尝试别的解析器。
    """
    if not isinstance(data, dict):
        return []
    raw_msgs = data.get("messages")
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return []

    msgs: list[dict[str, str]] = []
    for m in raw_msgs:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        if role in ("user", "human"):
            role = "user"
        elif role in ("assistant", "ai", "bot", "model"):
            role = "assistant"
        else:
            continue
        # 豆包正文可能在 content，也可能在 text（不同版本）
        text = m.get("content")
        if isinstance(text, dict):
            text = _text_of_parts(text)
        if not text:
            text = m.get("text") or m.get("message") or ""
        text = str(text).strip()
        if text:
            msgs.append({"role": role, "text": text})

    if not msgs:
        return []

    title = (fallback_title or "").strip() or _title_from_text(msgs)
    return [{"title": title, "messages": msgs, "platform": "doubao"}]


# ---------------------------------------------------------------- Gemini Takeout

# Google Takeout 的 Gemini 活动记录是**一个巨大的 HTML**，
# 每条记录一个 <div class="outer-cell">。结构如下：
#
#   <div class="outer-cell ..."><div class="mdl-grid">
#     <div class="header-cell ..."><p>Gemini Apps<br></p></div>
#     <div class="content-cell ... body-1">Prompted <提问正文> <日期></div>
#     <div class="content-cell ... text-right">可选：图片预览</div>
#     <div class="content-cell ... caption"><b>商品：</b>…
#         <b>详细信息：</b><br>&emsp;https://gemini.google.com/app/<对话id>: <a…>
#     </div></div></div>
#
# ★ 重要限制：Google 只导出**你发出的提问**，不包含 Gemini 的回答。
#   所以导入后的对话里只有 user 消息。这是数据源本身的限制，不是解析漏了。
#   实测 2459 条记录里：Prompted 2162 / Branched 116 / 其余是清反馈、建 gem 等噪音。
#   有回答的只有 Prompted 和 Branched（分叉也会带提问）。

# 记录自身的日期一定以「<br> 日期 <br>」紧邻包裹的形式出现。
# ★ 不能用"文本里出现的第一个日期"—— 提问里常粘着日志（2026-09-25T05:27:03Z INFO …），
#   那才是第一个匹配，会把日志之后的内容全当成回答。
_GE_DATE_BR = re.compile(
    r"<br\s*/?>\s*("
    r"\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}:\d{2}(?:\s+[A-Z]{2,5})?"
    r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?(?:\s+[A-Z]{2,5})?"
    r")\s*<br\s*/?>",
    re.I,
)

_GE_LEFT_OPEN = '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
_GE_RIGHT_OPEN = (
    '<div class="content-cell mdl-cell mdl-cell--6-col '
    'mdl-typography--body-1 mdl-typography--text-right">'
)
_GE_CAPTION_OPEN = '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
_GE_CONV = re.compile(r"gemini\.google\.com/app/([A-Za-z0-9_-]+)")
_GE_KEEP = ("Prompted", "Branched")

# 时区缩写 → 相对 UTC 的小时偏移
_GE_TZ = {
    "UTC": 0, "GMT": 0,
    "WIB": 7, "WITA": 8, "WIT": 9,
    "CST": 8, "HKT": 8, "SGT": 8, "JST": 9, "KST": 9,
    "PST": -8, "PDT": -7, "EST": -5, "EDT": -4,
    "CET": 1, "CEST": 2,
}

# 提问区里的装饰行，不该进正文
_GE_NOISE = [
    re.compile(r"^\s*\d+\s+generated\s+images?\.?\s*$", re.I),
    re.compile(r"^\s*Attached\s+\d+\s+files?\.?\s*$", re.I),
    re.compile(r"^\s*-\s*$"),
]

# 记录内的本地附件：`- <a href="xxx.jpg">xxx.jpg</a>`
# ★ 不能只认图片。实测 2459 条记录里还有 .txt 46 个、.js 49 个、.md 5 个，
#   它们和图片一样是"他发给模型的文件"，只搬图片等于把人家的代码附件全丢了。
#   仍然要排除活动记录里的外链（`https://…` 和 `gemini.google.com/app/…`），
#   判据是"裸文件名"：不含 `://`、不含 `/`。
_GE_ATTACH_HREF = re.compile(r'<a\s+href="([^"]+)"', re.I)

# Gemini **生成**的图不会出现在 HTML 的 <a href> 里，只会以文件形式躺在包里。
# 实测：一条记录的所有导出文件共享同一个 16 位十六进制后缀，
#   `4ae84f16…-7889f04c77f4113f.jpg`（他上传的）
#   `watermarked_img_1492…-7889f04c77f4113f.jpg`（它生成的，带 SynthID 水印）
# 所以判据是：**同后缀的文件里，除 href 提到的那几个之外，剩下的就是生成图**。
# 这一条同时覆盖三种命名（watermarked_img_* / Gemini_Generated_Image_* / 纯数字），
# 不靠文件名前缀猜，也就不怕 Google 改命名。
_GE_IMG_SRC = re.compile(
    r'<img[^>]+src="([^"]+\.(?:jpg|jpeg|png|webp|gif))"', re.I
)
_GE_SUFFIX = re.compile(r"-([0-9a-f]{16})\.[A-Za-z0-9]+$", re.I)


def _ge_html_to_text(fragment: str) -> str:
    """把回答的 HTML 转成可读文本（尽量保留 Markdown 结构）。

    为什么转 Markdown 而不是纯文本：
    Gemini 的回答大量用标题、列表、加粗、代码块。全拍平成一行，
    几百字的架构说明会变成一坨，导入后根本没法读。
    """
    s = fragment
    s = re.sub(r"<img[^>]*>", "", s, flags=re.I)          # 图片（附件另存，正文里不要）

    # 代码块先处理，避免内部 <code> 被后面的规则重复加反引号
    s = re.sub(r"<pre[^>]*>\s*<code[^>]*>", "\n```\n", s, flags=re.I)
    s = re.sub(r"</code>\s*</pre>", "\n```\n", s, flags=re.I)
    s = re.sub(r"<pre[^>]*>", "\n```\n", s, flags=re.I)
    s = re.sub(r"</pre>", "\n```\n", s, flags=re.I)

    # 标题
    for lvl in range(1, 7):
        s = re.sub(rf"<h{lvl}[^>]*>", "\n\n" + "#" * lvl + " ", s, flags=re.I)
        s = re.sub(rf"</h{lvl}>", "\n", s, flags=re.I)

    s = re.sub(r"<hr\s*/?>", "\n\n---\n\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"</li>", "", s, flags=re.I)
    s = re.sub(r"<blockquote[^>]*>", "\n> ", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<p[^>]*>", "", s, flags=re.I)

    # 粗体 / 行内代码
    s = re.sub(r"<strong[^>]*>", "**", s, flags=re.I)
    s = re.sub(r"</strong>", "**", s, flags=re.I)
    s = re.sub(r"<b[^>]*>", "**", s, flags=re.I)
    s = re.sub(r"</b>", "**", s, flags=re.I)
    s = re.sub(r"<code[^>]*>", "`", s, flags=re.I)
    s = re.sub(r"</code>", "`", s, flags=re.I)

    s = re.sub(r"<[^>]+>", "", s)                          # 其余标签一律去掉
    s = html_mod.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _ge_date_to_iso(raw: str) -> str | None:
    """`2026年4月26日 14:26:28 WIB` / ISO 串 → UTC ISO。

    必须换算：不换算的话导入后侧边栏排序会差几个小时，跨时区对话顺序会乱。
    """
    if not raw:
        return None
    raw = raw.strip()
    tz_name = ""
    m = re.search(r"([A-Z]{2,5})\s*$", raw)
    if m:
        tz_name = m.group(1)
        raw = raw[: m.start()].strip()
    off = _GE_TZ.get(tz_name, 0)

    dt = None
    mc = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2}):(\d{2})", raw)
    if mc:
        y, mo, d, hh, mm, ss = (int(x) for x in mc.groups())
        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
    else:
        mi = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", raw)
        if mi:
            y, mo, d, hh, mm, ss = (int(x) for x in mi.groups())
            dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
    if dt is None:
        return None
    return (dt - timedelta(hours=off)).isoformat().replace("+00:00", "Z")


def _ge_suffix(name: str) -> str:
    """取导出文件名的 16 位后缀（一条活动记录内所有文件共享它）。"""
    m = _GE_SUFFIX.search(name or "")
    return m.group(1) if m else ""


def parse_gemini_activity(
    raw_html: str,
    fetch_attach=None,
    files_by_suffix: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """解析 Google Takeout 的 Gemini 活动记录 HTML。

    一条记录 = 一次问答：
        左侧单元格：`Prompted` + 提问 + `<br>` + 日期 + `<br>` + 回答HTML
        右侧单元格：图片预览（没有则空）
        底部单元格：商品信息，含 https://gemini.google.com/app/<对话id>

    所以：按 `<br>日期<br>` 切分，前面是提问、后面是回答；
    再按对话 id 把多次问答聚合成一条对话。

    fetch_attach(name) -> bytes | None
        可选回调，用来从 zip 里取文件二进制。
    files_by_suffix: {16位后缀: [文件名, …]}
        可选。给了才能找到 **AI 生成的图** —— 它们不出现在 HTML 里，
        只能靠"同后缀、但没被 href 引用过"认出来。
    """
    body = raw_html[raw_html.find("<body"):] if "<body" in raw_html else raw_html
    blocks = body.split('<div class="outer-cell')[1:]

    # cid -> [(iso_time, question, answer, attachments)]
    by_conv: dict[str, list[tuple[str, str, str, list, list]]] = {}

    for b in blocks:
        ai = b.find(_GE_LEFT_OPEN)
        if ai < 0:
            continue
        # 左单元格到右单元格（或底部信息）之间就是它的内容
        end = b.find(_GE_RIGHT_OPEN, ai)
        if end < 0:
            end = b.find(_GE_CAPTION_OPEN, ai)
        if end < 0:
            continue
        left = b[ai + len(_GE_LEFT_OPEN): end]
        # 切分点之前的内容末尾会带一个闭合 </div>
        if left.rstrip().endswith("</div>"):
            left = left.rstrip()[: -len("</div>")]

        dm = _GE_DATE_BR.search(left)
        if not dm:
            continue

        before, after = left[: dm.start()], left[dm.end():]

        # 附件：直接从原始 HTML 里抓 <a href>。
        # ★ 不能等标签剥完再从文本行里认 —— 剥完只剩 "- 名字.jpg"，
        #   和提问正文里提到的文件名就分不清了。
        attaches: list[dict[str, Any]] = []
        generated: list[dict[str, Any]] = []
        if fetch_attach:
            # ① 他发出去/上传的文件：href 里的裸文件名（排除活动外链）
            seen_names: set[str] = set()
            for href in _GE_ATTACH_HREF.findall(before):
                if "://" in href or "/" in href:
                    continue                      # 外链，不是附件
                if href in seen_names:
                    continue
                seen_names.add(href)
                try:
                    blob = fetch_attach(href)
                except Exception:  # noqa: BLE001
                    blob = None
                if blob:
                    attaches.append({"name": href, "data": blob})

            # ② 右侧预览格里的 <img src>（没在 href 里出现过的，也算他上传的）
            for src in _GE_IMG_SRC.findall(b):
                if "://" in src or "/" in src or src in seen_names:
                    continue
                seen_names.add(src)
                try:
                    blob = fetch_attach(src)
                except Exception:  # noqa: BLE001
                    blob = None
                if blob:
                    attaches.append({"name": src, "data": blob})

            # ③ AI 生成的图：同后缀、但没被上面收走的那些文件
            #    生成图不在 HTML 里出现，只能这样反推。
            if files_by_suffix:
                sx = ""
                for nm in list(seen_names) or [x.rsplit("/", 1)[-1] for x in _GE_IMG_SRC.findall(b)]:
                    sx = _ge_suffix(nm)
                    if sx:
                        break
                for nm in files_by_suffix.get(sx, []) if sx else []:
                    if nm in seen_names:
                        continue
                    seen_names.add(nm)
                    try:
                        blob = fetch_attach(nm)
                    except Exception:  # noqa: BLE001
                        blob = None
                    if blob:
                        generated.append({"name": nm, "data": blob})

        # ★ 保留换行再清洗。
        #   之前先把 \s+ 压成空格，结果 "1 generated image." 这类装饰行
        #   和被压进同一行，行首锚定的过滤规则匹配不到，全留在提问里了。
        head_lines = re.sub(r"<br\s*/?>", "\n", before, flags=re.I)
        head_lines = re.sub(r"<[^>]+>", " ", head_lines)
        head_lines = html_mod.unescape(head_lines).replace("\xa0", " ")

        # 类型判定用压平后的文本（只取第一个词）
        flat = re.sub(r"\s+", " ", head_lines).strip()
        kind = flat.split(" ", 1)[0] if flat else ""
        if kind not in _GE_KEEP:
            continue

        # 先去掉类型前缀（它可能占一整行，也可能和提问同一行）
        body_lines = head_lines.split("\n")
        if body_lines:
            body_lines[0] = body_lines[0].replace(kind, "", 1)

        kept: list[str] = []
        for ln in body_lines:
            t = ln.strip()
            if not t:
                continue
            if any(p.match(t) for p in _GE_NOISE):
                continue
            # 纯附件链接行（"- xxx.jpg"）也丢掉
            if re.fullmatch(r"-\s*[\w\-.]+\.(?:jpg|jpeg|png|webp|gif|mp4|txt|json|xlsx?)", t, re.I):
                continue
            kept.append(t)

        question = "\n".join(kept).strip()
        if not question:
            continue

        answer = _ge_html_to_text(after)

        cid_m = _GE_CONV.search(b)
        if not cid_m:
            continue

        iso = _ge_date_to_iso(dm.group(1)) or ""
        by_conv.setdefault(cid_m.group(1), []).append((iso, question, answer, attaches, generated))

    convs: list[dict[str, Any]] = []
    for cid, pairs in by_conv.items():
        # 同一对话内按时间正序（HTML 是新→旧）
        pairs.sort(key=lambda x: x[0])

        # 去重：完全相同的 时间+提问 只留一条
        seen: set[tuple[str, str]] = set()
        uniq: list[tuple[str, str, str, list, list]] = []
        for iso, q, a, at, gen in pairs:
            key = (iso, q)
            if key in seen:
                continue
            seen.add(key)
            uniq.append((iso, q, a, at, gen))
        if not uniq:
            continue

        msgs: list[dict[str, Any]] = []
        for _iso, q, a, at, gen in uniq:
            um: dict[str, Any] = {"role": "user", "text": q}
            if at:
                um["attachments"] = at
            msgs.append(um)
            if a or gen:
                am: dict[str, Any] = {"role": "assistant", "text": a}
                if gen:
                    am["attachments"] = gen
                msgs.append(am)

        convs.append(
            {
                "title": _title_from_text([{"role": "user", "text": uniq[0][1]}]),
                "messages": msgs,
                "created": uniq[0][0] or None,
                "updated": uniq[-1][0] or None,
                "source_id": cid,
                "platform": "gemini",
            }
        )

    convs.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return convs


def _title_from_text(msgs: list[dict[str, str]]) -> str:
    """用第一条用户消息生成标题。

    只取第一行、去掉前后空白、截断 —— 因为是代码/长文时，
    标题里塞一堆换行会让侧边栏很难看。
    """
    for m in msgs:
        if m.get("role") != "user":
            continue
        t = (m.get("text") or "").strip()
        if not t:
            continue
        first = t.split("\n", 1)[0].strip()
        first = re.sub(r"\s+", " ", first)
        if len(first) > 42:
            first = first[:42] + "…"
        if first:
            return first
    return "导入的对话"


# ---------------------------------------------------------------- DeepSeek 官方导出

def _frag_text(fragments: Any, want: str) -> str:
    """从 fragments 里挑出指定类型的内容。

    DeepSeek 把一条消息拆成多个 fragment：
      REQUEST（用户提问）/ RESPONSE（回答）/ THINK（思考）/ TOOL_SEARCH / TOOL_OPEN ...
    只有 REQUEST 和 RESPONSE 是真正要保留的对话内容 ——
    思考过程和搜索记录属于"过程噪音"，导进来会把历史对话弄得很脏。
    """
    out: list[str] = []
    if not isinstance(fragments, list):
        return ""
    for f in fragments:
        if not isinstance(f, dict) or f.get("type") != want:
            continue
        c = f.get("content")
        if isinstance(c, str) and c.strip():
            out.append(c.strip())
    return "\n\n".join(out)


def _parse_deepseek_session(data: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any] | None:
    msgs: list[dict[str, str]] = []
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").upper()
        frags = m.get("fragments") or []
        if role == "USER":
            text, r = _frag_text(frags, "REQUEST"), "user"
        elif role == "ASSISTANT":
            text, r = _frag_text(frags, "RESPONSE"), "assistant"
        else:
            continue
        if text:
            msgs.append({"role": r, "text": text})
    if not msgs:
        return None
    return {
        "title": data.get("title") or (meta or {}).get("title") or "未命名对话",
        "messages": msgs,
        "created": data.get("inserted_at") or (meta or {}).get("inserted_at"),
        "updated": data.get("updated_at") or (meta or {}).get("updated_at"),
        "source_id": data.get("id") or (meta or {}).get("id"),
    }


def parse_deepseek_zip(raw: bytes) -> list[dict[str, Any]]:
    """DeepSeek 官方导出是一个 zip，里面有：
         chat_session_index.json   [{id,title,message_count,inserted_at,updated_at}]
         chat_sessions/<id>.json   {id,title,messages:[...]}
         user.json / manifest.json

    为什么按索引排序而不是按文件名：文件名是 uuid，没有任何时间含义；
    索引里的 updated_at 才是"最近聊过"的真实顺序，用户期望先看到最近的。
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()

        # zip 里可能套一层目录，取最短的公共前缀
        prefix = ""
        cand = [n for n in names if n.endswith("chat_session_index.json")]
        if cand:
            prefix = cand[0][: -len("chat_session_index.json")]

        order: list[dict[str, Any]] = []
        idx_name = prefix + "chat_session_index.json"
        if idx_name in names:
            try:
                idx = json.loads(zf.read(idx_name).decode("utf-8", "ignore"))
                if isinstance(idx, list):
                    order = sorted(
                        [x for x in idx if isinstance(x, dict)],
                        key=lambda x: x.get("updated_at") or "",
                        reverse=True,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("deepseek index parse failed: %s", exc)

        session_names = [n for n in names if n.startswith(prefix + "chat_sessions/") and n.endswith(".json")]

        convs: list[dict[str, Any]] = []
        seen: set[str] = set()

        def take(path: str, meta: dict[str, Any] | None) -> None:
            try:
                data = json.loads(zf.read(path).decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                return
            if not isinstance(data, dict):
                return
            cid = data.get("id") or (meta or {}).get("id")
            if cid and cid in seen:
                return
            got = _parse_deepseek_session(data, meta)
            if got:
                if cid:
                    seen.add(cid)
                convs.append(got)

        # 先按索引顺序
        for item in order:
            cid = item.get("id")
            if not cid:
                continue
            path = f"{prefix}chat_sessions/{cid}.json"
            if path in names:
                take(path, item)
            if len(convs) >= 300:      # 单次导入上限，防超大包把内存吃穿
                break

        # 再兜一遍索引没提到的（比如索引损坏）
        for n in session_names:
            if len(convs) >= 300:
                break
            take(n, None)

    return convs


def collect(raw: bytes, filename: str) -> tuple[str, list[dict[str, Any]]]:
    """把上传的文件变成中间结构。返回 (平台名, 会话列表)。

    支持的来源（按识别顺序）：
      .zip  → DeepSeek 官方导出 / Google Takeout / ChatGPT 导出包
      .html → Google Takeout 的「我的活动记录」
      .json → ChatGPT conversations.json / 豆包单会话 / 通用结构
      其它  → 当纯文本，按「用户:」「助手:」切分
    """
    name = (filename or "").lower()
    stem = (filename or "").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0]

    # ---------------- zip ----------------
    if name.endswith(".zip") or raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()

            # ① DeepSeek 官方导出
            if any(n.endswith("chat_session_index.json") for n in names):
                convs = parse_deepseek_zip(raw)
                if convs:
                    return "deepseek", convs
                raise ValueError("DeepSeek 导出包里没有可用对话（可能全是空会话）")

            # ② Google Takeout：Gemini 的活动记录 HTML
            gem = _find_gemini_html(zf, names)
            if gem:
                text = zf.read(gem).decode("utf-8", "ignore")
                fetcher = _make_zip_fetcher(zf, names, gem)
                # 建"后缀 → 同组文件"索引：AI 生成的图不在 HTML 里，
                # 只能靠同后缀反推（见 parse_gemini_activity 的注释）
                base_dir = gem.rsplit("/", 1)[0] + "/" if "/" in gem else ""
                by_sfx: dict[str, list[str]] = {}
                for n in names:
                    if not n.startswith(base_dir):
                        continue
                    short = n.rsplit("/", 1)[-1]
                    sx = _ge_suffix(short)
                    if sx:
                        by_sfx.setdefault(sx, []).append(short)
                convs = parse_gemini_activity(text, fetch_attach=fetcher, files_by_suffix=by_sfx)
                if convs:
                    return "gemini", convs
                raise ValueError(
                    "找到了 Gemini 活动记录，但里面没有提问记录。"
                    "请确认导出时勾选了「我的活动 → Gemini Apps」"
                )

            # ③ ChatGPT 导出
            target = next((n for n in names if n.endswith("conversations.json")), None)
            if target:
                raw = zf.read(target)
                name = "conversations.json"
            else:
                # ④ 兜底：包里若有一个明显是对话数组的 json
                best = None
                for n in names:
                    if not n.endswith(".json") or "/chat_sessions/" in n:
                        continue
                    try:
                        head = zf.read(n)[:200]
                    except Exception:  # noqa: BLE001
                        continue
                    if head[:1] in (b"[", b"{"):
                        best = n
                        break
                if not best:
                    raise ValueError(
                        "压缩包里没找到可识别的对话文件。\n"
                        "支持：DeepSeek 官方导出、Google Takeout（Gemini）、"
                        "ChatGPT 的 conversations.json、豆包单会话 JSON"
                    )
                raw = zf.read(best)
                name = best

            # ChatGPT 的 AI 生成图（DALL·E）在包里是 `file_xxx.dat`，
            # 靠 asset_pointer "sediment://file_xxx" 对上。这里把 zip 包一层，
            # 让解析器能取到字节 —— 不接这一步，DALL·E 的图一张都导不进来。
            if name.endswith("conversations.json"):
                try:
                    data = json.loads(raw.decode("utf-8", "ignore"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"conversations.json 解析失败：{exc}") from exc

                def _asset(ptr: str) -> bytes | None:
                    key = ptr.rsplit("/", 1)[-1]
                    for cand in (f"{key}.dat", f"{key}.png", f"{key}.jpg", f"{key}.webp"):
                        if cand in names:
                            return zf.read(cand)
                    return None

                convs = parse_chatgpt(data, fetch_asset=_asset)
                if convs:
                    return "chatgpt", convs
                raise ValueError("ChatGPT 导出包里没有可用对话")

    # ---------------- 直接上传 HTML（Takeout 的 html 单独上传）----------------
    head_bytes = raw[:400].lstrip()
    if name.endswith((".html", ".htm")) or head_bytes[:5].lower() in (b"<!doc", b"<html"):
        text = raw.decode("utf-8", "ignore")
        # 必须真的是 Takeout 的活动记录，避免把任意网页当对话
        if "outer-cell" in text and "gemini.google.com/app/" in text:
            convs = parse_gemini_activity(text)
            if convs:
                return "gemini", convs
            raise ValueError("这个 HTML 里没有 Gemini 提问记录")
        raise ValueError("这个 HTML 不是 Google Takeout 的 Gemini 活动记录")

    # ---------------- JSON ----------------
    if raw[:1] in (b"[", b"{"):
        try:
            data = json.loads(raw.decode("utf-8", "ignore"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败：{exc}") from exc

        convs = parse_chatgpt(data)
        if convs:
            return "chatgpt", convs

        # 豆包：{"messages":[{role, content}]}，用它自己的解析器（带文件名做标题）
        convs = parse_doubao(data, fallback_title=stem)
        if convs:
            return "doubao", convs

        convs = parse_generic(data)
        if convs:
            return "generic", convs

        raise ValueError("JSON 里没有可识别的对话结构")

    # ---------------- 纯文本 ----------------
    text = raw.decode("utf-8", "ignore")
    if not text.strip():
        raise ValueError("文件是空的")
    low = text[:2000].lower()
    platform = "deepseek" if "deepseek" in low else "text"
    return platform, parse_text(text)


def _attach_stem(name: str) -> str:
    """附件名的「主体」：去掉扩展名，再去掉末尾的短哈希。

    Takeout 里的命名形如 `<主体>-<16位哈希>.<ext>`，但活动记录 HTML 里写的
    可能和实际文件不一致（实测两种偏差）：
      · 扩展名不同：HTML 写 .jpeg，文件是 .jpg
      · 哈希不同：HTML 写 image_0618cd-772ff49450f358c3.jpg，
                 文件是 image_0618cd-9e72deb69e1a6998.jpg
    这两种情况下精确匹配都会失败（实测 217 个引用里丢了 6 张）。
    用主体去匹配能把它们捞回来。
    """
    base = name.rsplit("/", 1)[-1]
    stem, dot, _ext = base.rpartition(".")
    if not dot:
        stem = base
    # 去掉末尾 `-<hash>`（6 位以上十六进制）
    return re.sub(r"-[0-9a-fA-F]{6,}$", "", stem)


def _make_zip_fetcher(zf: zipfile.ZipFile, names: list[str], html_path: str):
    """构造一个「按文件名从 zip 里取附件」的回调。

    Takeout 里附件和活动记录 HTML 在同一目录，链接里只写文件名（不带路径），
    所以先按 HTML 所在目录拼，再全局找，最后按主体宽松匹配。
    """
    base = html_path.rsplit("/", 1)[0] + "/" if "/" in html_path else ""
    by_name: dict[str, str] = {}
    by_stem: dict[str, list[str]] = {}
    for n in names:
        short = n.rsplit("/", 1)[-1]
        by_name.setdefault(short, n)
        by_stem.setdefault(_attach_stem(short), []).append(n)

    IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def _read(path: str):
        try:
            return zf.read(path)
        except Exception:  # noqa: BLE001
            return None

    def fetch(href: str):
        name = href.rsplit("/", 1)[-1]

        # ① 精确：先同目录，再全局
        for cand in (base + name, name):
            if cand in zf.namelist():
                got = _read(cand)
                if got:
                    return got
        real = by_name.get(name)
        if real:
            got = _read(real)
            if got:
                return got

        # ② 宽松：同主体，扩展名必须同属图片
        want_ext = os.path.splitext(name)[1].lower()
        if want_ext not in IMG_EXT:
            return None
        for cand in by_stem.get(_attach_stem(name), []):
            if os.path.splitext(cand)[1].lower() not in IMG_EXT:
                continue
            got = _read(cand)
            if got:
                return got
        return None

    return fetch


def _find_gemini_html(zf: zipfile.ZipFile, names: list[str]) -> str | None:
    """在 Takeout 包里找 Gemini 活动记录 HTML。

    文件名在不同语言环境下不一样（「我的活动记录.html」/「My Activity.html」），
    所以**不能按文件名找**。改成按内容特征：读前 2MB，看有没有
    outer-cell 和 gemini.google.com/app/ 这两个标记。

    实测 Takeout 里有几百个文件、上百兆图片，所以：
      · 只检查 .html
      · 跳过看起来很大的文件（活动记录一般 5-30MB，正常范围内）
      · 一旦命中立刻返回，不做全量扫描
    """
    for n in names:
        if not n.lower().endswith((".html", ".htm")):
            continue
        try:
            info = zf.getinfo(n)
        except KeyError:
            continue
        # 活动记录可能很大，但超过 200MB 基本不可能是它
        if info.file_size > 200 * 1024 * 1024:
            continue
        try:
            with zf.open(n) as fh:
                head = fh.read(2 * 1024 * 1024).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        if "outer-cell" in head or ("Gemini" in head and "mdl-grid" in head):
            return n
    return None


# ---------------------------------------------------------------- 切成抽取块

def chunks_for_extract(convs: list[dict[str, Any]], limit: int = 40, size: int = 2800) -> list[dict[str, Any]]:
    """把会话切成送给抽取器的块。每块带来源信息，方便写进 source 字段。"""
    out: list[dict[str, Any]] = []
    for conv in convs:
        buf: list[str] = []
        size_now = 0
        for m in conv["messages"]:
            line = f"{m['role']}: {m['text'][:1200]}"
            buf.append(line)
            size_now += len(line)
            if size_now >= size:
                out.append({"title": conv["title"], "text": "\n".join(buf)})
                buf, size_now = [], 0
                if len(out) >= limit:
                    return out
        if buf:
            out.append({"title": conv["title"], "text": "\n".join(buf)})
        if len(out) >= limit:
            return out
    return out
