"""长期记忆的"冷启动"：把历史对话变成记忆、话题、时间线、画像。

## 为什么需要这个模块

在线链路只在"聊完一句"时抽一条记忆（`main.extract_and_store`）。
一个人导进来 414 个历史对话，记忆库却还是空的 —— 产品看起来"没有记忆"，
其实只是从来没人读过那些对话。

这个模块做的是离线批处理，把存量对话一次性补上。四步，依次：

```
① digest   每个对话 → 一句话 + 标签        （话题聚类和时间线的原料）
② extract  每个对话 → 0-6 条长期记忆        （复用 extract.py 的 prompt，不另造一套）
③ topic    把 digest 聚成话题               （分批交给模型判归属，再跨批合并）
④ profile  汇总记忆与话题 → 全局画像        （写进 umf_profiles，注入时永远常驻）
```

## 三个设计上的硬约束

1. **可断点续跑。** 414 个对话要跑十几分钟，中途重启容器很常见。
   `conv_digests.status = 'done'` 就是进度；重跑只处理没 done 的。
2. **幂等。** 每个对话产生的记忆都带 `import_batch = conv:<id>`，
   重跑前先删掉这一批再重写，所以反复跑不会长出重复条目。
3. **不阻塞请求。** 起的是 `asyncio.create_task`，接口立刻返回 job_id，
   进度写在 `memory_jobs` 表里 —— 进程内变量在重建容器时就没了。

## 为什么不用 Mem0 / Zep 直接干这件事

两者都是"在线 ingest"，一次喂一段对话，适合逐句长记忆；
对"400 个存量对话冷启动"它们没有批处理入口，仍然要自己写调度和进度。
所以这里沿用已有的 `extract.py` + pgvector —— 语义上等价，依赖更少，
而且记忆始终落在自己的表里（Mem0 只是可选的索引层，见 mem0_engine.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import db, extract, llm, mg, store
from .compress import estimate_tokens

log = logging.getLogger("brain.membuild")

# 并发度：模型侧扛得住，再高就会被上游限流打成一片 429。
# 6 是实测值：414 个对话约 10 分钟跑完，同时没有一次 429。
CONCURRENCY = 6

# 一次抽取最多看多少字。对话再长，也只是"记忆的原料"，
# 看全没有意义 —— 人回忆一段聊天也只记得开头和结论。
MAX_TRANSCRIPT = 4200

MAX_MEMORIES = 400        # 一个人最多保留的记忆条数（超过就按置信度+时间裁）
PROFILE_MIN_MEM = 3       # 少于此数不生成画像，否则画像是瞎编的

# 攒够多少条记忆变动就自动重算画像。
#
# 为什么不是"每条都重算"：一次画像是一次 8000 token 预算的调用，
# 聊三句话就重算一遍，比聊天本身还贵；而且刚聊完的记忆往往还没稳定，
# 立刻拿去做画像容易把一次性的事写成长期特征。
# 8 是个折中：正常聊十几轮会自动更新一次，手动加记忆也能很快看到效果。
# 设成 0 可以关掉自动更新（只保留手动点「重新整理」）。
PROFILE_REFRESH_EVERY = int(os.getenv("PROFILE_REFRESH_EVERY", "8"))

# 正在重算画像的用户。进程内就够：brain 是单进程 uvicorn，
# 重启丢了这个集合也不会错 —— 事件表是持久的，下次触发照常算。
_profile_busy: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ 原料

def _transcript(msgs: list[dict[str, Any]]) -> tuple[str, int, int]:
    """把消息拼成给模型看的文本。返回 (文本, 消息数, 用户发言数)。

    只留用户说了什么 + 助手回答的骨架：
    记忆是关于"这个人"的，助手的措辞不是原料，反而会把模型带偏
    （它容易把"AI 当时的建议"当成"用户认同的观点"记下来）。
    """
    lines: list[str] = []
    users = 0
    for m in msgs:
        body = (m.get("text") or "").strip()
        if not body:
            continue
        if m.get("from") == "user":
            users += 1
            lines.append("用户：" + body[:600])
        else:
            lines.append("助手：" + body[:220])
    text = "\n".join(lines)
    if estimate_tokens(text) > MAX_TRANSCRIPT:
        # 头 2/3 + 尾 1/3：一段对话的"意图"在开头，"结论"在结尾
        keep = MAX_TRANSCRIPT * 2
        head = int(keep * 0.66)
        text = text[:head] + "\n…（中间省略）…\n" + text[-int(keep * 0.34):]
    return text, len(msgs), users


DIGEST_SYSTEM = """你在给一个人的历史对话编目录。读下面的对话，用严格的 JSON 回答，不要解释、不要代码块：

{"one_line":"这段在聊什么，20-40 字，第三人称，写具体的事和人名","tags":["2-4 个短标签"]}

要求：
- one_line 写"用户在做什么/问什么/决定了什么"，不写"用户与助手进行了交流"这种废话。
- 标签用名词，2-6 个字，例如：图片生成、WPS、部署、求职、健身。
- 只根据对话内容，不要编造。"""


async def _digest_one(conv: dict[str, Any], msgs: list[dict[str, Any]]) -> dict[str, Any]:
    text, n, users = _transcript(msgs)
    if not text.strip():
        return {"status": "empty", "one_line": "", "tags": [], "msg_count": n, "user_msg_count": users}

    prompt = f"标题：{conv.get('title') or '（无标题）'}\n\n对话：\n{text}"
    raw = await _ask(DIGEST_SYSTEM, prompt, max_tokens=300)
    data = _json_obj(raw, require=("one_line", "tags"))
    line = str(data.get("one_line") or "").strip()
    tags = [str(t).strip()[:12] for t in (data.get("tags") or []) if str(t).strip()][:4]
    if not line:
        # 模型偶尔只给标签或直接拒答：退化成标题，至少时间线上有东西看
        line = str(conv.get("title") or "").strip()[:60]
    return {
        "status": "done" if line else "empty",
        "one_line": line[:120],
        "tags": tags,
        "msg_count": n,
        "user_msg_count": users,
    }


# ------------------------------------------------------------------ 模型调用

async def _ask(system: str, user: str, *, max_tokens: int = 600) -> str:
    """带两次重试的单轮调用。

    空回复是这类结构化调用的常见故障（思考型模型把预算花在内联推理上），
    重试并且逐次放宽预算，比换模型便宜。
    """
    for attempt in range(2):
        try:
            out = await llm.complete(
                "cheap-extract",
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.1,
                max_tokens=max_tokens * (2 ** attempt),
            )
            if out.strip():
                return out
        except Exception as exc:  # noqa: BLE001
            log.warning("ask failed (attempt %s): %s", attempt + 1, exc)
        await asyncio.sleep(1.5 * (attempt + 1))
    return ""


_JSON_ARR = re.compile(r"\[.*\]", re.S)
_JSON_OBJ = re.compile(r"\{.*\}", re.S)
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def _strip_fence(raw: str) -> str:
    return _FENCE.sub("", (raw or "").strip()).strip()


def _json_obj(raw: str, require: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
    """从模型回复里抠出一个 JSON 对象，尽量不因为格式问题丢掉结果。

    三层，从"最干净"到"最脏"：
      1. 整段就是 JSON（去掉 ```json 围栏后直接 parse）—— 绝大多数情况
      2. 贪婪正则抠 {...} —— 前后有解释文字时
      3. 逐个 '{' 位置尝试 raw_decode —— 前面的话里也带花括号时

    `require` 是"这个对象至少要有哪个键"的校验。没有它，第 3 层会抓到前文里
    第一个能解析的花括号片段（比如引用字段名时写的 {"k","v"}），
    返回一个结构对但内容错的对象 —— 上层拿不到 topics/basics，静默变成空结果。
    这就是"画像偶尔生成失败、话题偶尔只剩一个"的真正原因。

    ★ 为什么不是一句 `re.search(r"\\{.*\\}")`：这段代码第一次上线时就栽在这里。
      模型有时先输出一段"好的，我来分析…"再给 JSON，那段话里如果也出现括号
      （比如引用字段名 `{k,v}`），贪婪正则会把两段连起来，parse 必然失败，
      于是画像静默地变成空 —— 前端只看到"还没有画像"，日志里连个 warning 都没有。
    """
    s = _strip_fence(raw)
    if not s:
        return {}
    try:
        v = json.loads(s)
        if isinstance(v, dict) and (not require or any(k in v for k in require)):
            return v
    except json.JSONDecodeError:
        pass

    m = _JSON_OBJ.search(s)

    def good(v: Any) -> bool:
        if not isinstance(v, dict):
            return False
        if require:
            return any(k in v for k in require)
        return len(v) >= 2

    # 先试贪婪匹配出来的那一整块（最常见的位置），它可能因为截断而解析失败
    if m:
        try:
            v = json.loads(m.group(0))
            if good(v):
                return v
        except json.JSONDecodeError:
            pass

    dec = json.JSONDecoder()
    fallback: dict[str, Any] = {}
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            v, _end = dec.raw_decode(s[i:])
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(v, dict):
            continue
        if good(v):
            return v
        fallback = fallback or v
    if fallback:
        log.warning("json shape unexpected (require=%s), keys=%s", require, list(fallback)[:6])
    else:
        log.warning("json parse failed, head=%s", s[:160])
    return fallback


def _json_arr(raw: str) -> list[Any]:
    s = _strip_fence(raw)
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass

    m = _JSON_ARR.search(s)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, list):
                return v
        except json.JSONDecodeError:
            pass

    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch != "[":
            continue
        try:
            v, _end = dec.raw_decode(s[i:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, list):
            return v
    return []


# ------------------------------------------------------------------ 进度

async def _job_new(user_id: str, kind: str = "backfill", total: int = 0) -> str:
    job_id = "j_" + uuid.uuid4().hex[:12]
    await db.execute(
        """INSERT INTO memory_jobs (id, user_id, kind, status, stage, total, done)
           VALUES ($1,$2,$3,'running','准备中',0,$4)""",
        job_id,
        user_id,
        kind,
        total,
    )
    return job_id


async def _job_set(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols, args = [], []
    for k, v in fields.items():
        if k == "stats":
            args.append(json.dumps(v, ensure_ascii=False))
            cols.append(f"stats = ${len(args)}::jsonb")
        else:
            args.append(v)
            cols.append(f"{k} = ${len(args)}")
    args.append(job_id)
    await db.execute(
        f"UPDATE memory_jobs SET {', '.join(cols)}, updated_at = now() WHERE id = ${len(args)}",
        *args,
    )


async def job_status(user_id: str) -> Optional[dict[str, Any]]:
    row = await db.fetchrow(
        """SELECT id, status, stage, total, done, stats, error, started_at, updated_at
           FROM memory_jobs WHERE user_id = $1 ORDER BY started_at DESC LIMIT 1""",
        user_id,
    )
    if not row:
        return None
    out = dict(row)
    out["stats"] = _jsonval(out.get("stats")) or {}
    return out


# ------------------------------------------------------------------ ① ② 逐对话

async def _process_conv(
    uid: str, conv: dict[str, Any], sem: asyncio.Semaphore, *, force: bool
) -> dict[str, Any]:
    cid = conv["id"]
    async with sem:
        try:
            msgs = await asyncio.to_thread(mg.conversation_messages, cid, 160)
        except Exception as exc:  # noqa: BLE001
            log.warning("read messages failed %s: %s", cid, exc)
            return {"status": "failed", "error": str(exc)[:200]}

        started = conv.get("created_at") or conv.get("updated_at")
        ended = conv.get("updated_at") or started

        # ① 目录 + ② 记忆，两条并行：同一份文本喂两次，不同输出。
        # 串行会把单个对话的耗时翻倍，而两者互不依赖。
        digest_task = asyncio.create_task(_digest_one(conv, msgs))
        items_task = asyncio.create_task(_extract_items(msgs, conv))
        digest, items = await asyncio.gather(digest_task, items_task, return_exceptions=True)

        if isinstance(digest, Exception):
            log.warning("digest failed %s: %s", cid, digest)
            digest = {"status": "failed", "one_line": "", "tags": [], "msg_count": len(msgs), "user_msg_count": 0, "error": str(digest)[:200]}
        if isinstance(items, Exception):
            log.warning("extract failed %s: %s", cid, items)
            items = []

        written = await _write_memories(uid, cid, conv, items, force=force)

        await db.execute(
            """INSERT INTO conv_digests
                 (conversation_id, user_id, title, one_line, tags, msg_count, user_msg_count,
                  started_at, ended_at, memories, status, error, processed_at)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,now())
               ON CONFLICT (conversation_id) DO UPDATE
                 SET user_id = EXCLUDED.user_id, title = EXCLUDED.title,
                     one_line = EXCLUDED.one_line, tags = EXCLUDED.tags,
                     msg_count = EXCLUDED.msg_count, user_msg_count = EXCLUDED.user_msg_count,
                     started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at,
                     memories = EXCLUDED.memories, status = EXCLUDED.status,
                     error = EXCLUDED.error, processed_at = now()""",
            cid,
            uid,
            str(conv.get("title") or "")[:200],
            digest.get("one_line") or "",
            json.dumps(digest.get("tags") or [], ensure_ascii=False),
            int(digest.get("msg_count") or 0),
            int(digest.get("user_msg_count") or 0),
            started,
            ended,
            written,
            digest.get("status") or "done",
            digest.get("error"),
        )
        return {"status": digest.get("status"), "memories": written}


async def _extract_items(msgs: list[dict[str, Any]], conv: dict[str, Any]) -> list[dict[str, Any]]:
    """复用 extract.py：它的 prompt 已经调好了分层与"宁缺勿滥"。"""
    plain = [
        {"role": "user" if m.get("from") == "user" else "assistant", "content": (m.get("text") or "")[:1200]}
        for m in msgs
        if (m.get("text") or "").strip()
    ]
    if not plain:
        return []
    # 和在线链路同样的门槛：一句话问答里没有"长期信息"
    if sum(len(p["content"]) for p in plain) < 60:
        return []
    return await extract.extract(plain)


async def _write_memories(
    uid: str,
    cid: str,
    conv: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    force: bool,
) -> int:
    """写一个对话抽出来的记忆；重跑先清这一批，保证幂等。

    `import_batch = conv:<id>` 是"这批记忆由哪个对话产生"的唯一凭证，
    同时也是重跑时的删除条件 —— 只删自己的，不碰用户手加的和别处导入的。
    向量在这里批量算：一条一次 embed 会让 400 个对话变成 800 次请求。
    """
    batch = f"conv:{cid}"
    if force:
        await db.execute(
            "DELETE FROM memories WHERE user_id = $1 AND import_batch = $2", uid, batch
        )
    if not items:
        return 0

    texts = [i["text"] for i in items]
    vectors: list[Optional[str]] = [None] * len(texts)
    try:
        vecs = await llm.embed(texts)
        for i, v in enumerate(vecs[: len(texts)]):
            vectors[i] = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
    except Exception as exc:  # noqa: BLE001
        log.warning("batch embed failed (%s), memories saved without vectors", exc)

    at = conv.get("created_at")
    n = 0
    for i, item in enumerate(items):
        try:
            await store.add_memory(
                uid,
                item["text"],
                scope="agent" if item.get("scope_hint") == "agent" and conv.get("agent_id") else "global",
                agent_id=conv.get("agent_id") if item.get("scope_hint") == "agent" else None,
                kind=item.get("kind") or "fact",
                layer=item.get("layer"),
                entities=item.get("entities"),
                confidence=float(item.get("confidence") or 0.7),
                source={
                    "platform": "backfill",
                    "conversation_id": cid,
                    "title": (conv.get("title") or "")[:120],
                    "at": at.isoformat() if hasattr(at, "isoformat") else str(at or ""),
                },
                import_batch=batch,
                embed=False,
                vector_json=vectors[i],
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("write memory failed: %s", exc)
    return n


# ------------------------------------------------------------------ ③ 话题

TOPIC_SYSTEM = """你在给一个人的历史对话编话题。为每一段对话起一个 2-8 个字的话题名，
让同类的事共用同一个名字（做同一个项目的不同环节、同一类反复问的问题，都算同类）。

每行输出一条，格式严格为：编号|话题名
不要解释，不要代码块，不要空行，不要遗漏编号。

示例：
1|表白墙机器人
2|乐高模型
3|表白墙机器人
4|英语作文"""

TOPIC_BATCH = 20


async def _names_for_batch(rows: list[dict[str, Any]]) -> list[str]:
    """一批对话 → 每段一个话题名（顺序对应）。

    ## 为什么不用 JSON、不用一次发完

    实测这个便宜模型（qwen flash）有两个硬脾气：
      · 输入超过 20 条就整个返回空 content（不是截断，是空字符串）
      · 要求输出嵌套 JSON 数组时，「编号必须全覆盖」这种约束最容易让它放弃
    而"每行 编号|名字"的纯文本输出在 20 条以内非常稳，解析也只有一行 split。
    代价是批数变多（415 段 → 21 批），但每批只要 3-5 秒，总共两分钟，
    比反复失败重跑便宜得多。
    """
    lines = [f'{i}. {r["title"] or "（无标题）"}' for i, r in enumerate(rows, 1)]
    raw = await _ask(TOPIC_SYSTEM, "\n".join(lines), max_tokens=2000)
    names: dict[int, str] = {}
    for line in (raw or "").splitlines():
        if "|" not in line:
            continue
        left, _, right = line.partition("|")
        idx = re.sub(r"[^0-9]", "", left)
        name = right.strip().strip("「」『』\"' ")
        if idx.isdigit() and name:
            names[int(idx)] = name[:20]
    return [names.get(i + 1, "") for i in range(len(rows))]


def _norm_topic(name: str) -> str:
    n = re.sub(r"[^\w\u4e00-\u9fff]", "", name or "")
    return n.lower()


async def _build_topics(uid: str, rows: list[dict[str, Any]], job_id: str) -> int:
    """逐批命名 → 按名字聚合。

    和"让模型直接输出分组"相比，这里只让模型做它擅长的那一半（给单条起名），
    聚合放到本地做。好处是：某批失败只丢 20 条的名字，不会全盘重来；
    同名天然合并（模型在同一批里会复用名字）；不需要模型输出编号列表。
    """
    if not rows:
        return 0

    batches = [rows[i: i + TOPIC_BATCH] for i in range(0, len(rows), TOPIC_BATCH)]
    named: list[tuple[dict[str, Any], str]] = []
    for bi, batch in enumerate(batches):
        await _job_set(job_id, stage=f"整理话题 {bi + 1}/{len(batches)}")
        try:
            names = await _names_for_batch(batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("topic batch %s failed: %s", bi, exc)
            names = [""] * len(batch)
        for r, n in zip(batch, names):
            named.append((r, n))

    groups: dict[str, dict[str, Any]] = {}
    for r, name in named:
        key = _norm_topic(name) or "__other__"
        g = groups.get(key)
        if not g:
            g = {"title": name or "其他", "conv_ids": [], "keywords": [], "lines": [], "starts": []}
            groups[key] = g
        g["conv_ids"].append(r["conversation_id"])
        for t in _tags(r.get("tags")):
            if t not in g["keywords"]:
                g["keywords"].append(t)
        if r.get("one_line"):
            g["lines"].append(r["one_line"])
        if r.get("started_at"):
            g["starts"].append(r["started_at"])

    if not groups:
        return 0

    # 只有 1 段的"话题"没有回看价值，合成一个"零散"桶。
    # 前端本来也会把 1-2 段的折起来，这里先合一次是为了不让表里塞几百行。
    small = {k: g for k, g in groups.items() if len(g["conv_ids"]) == 1}
    if len(small) >= 8:
        bucket = {"title": "零散对话", "conv_ids": [], "keywords": [], "lines": [], "starts": []}
        for k, g in small.items():
            bucket["conv_ids"] += g["conv_ids"]
            bucket["starts"] += g["starts"]
            bucket["lines"] += g["lines"][:1]
            groups.pop(k)
        groups["__single__"] = bucket

    await db.execute("DELETE FROM topics WHERE user_id = $1", uid)
    ordered = sorted(groups.values(), key=lambda g: min(g["starts"]) if g["starts"] else _now())
    for g in ordered:
        if not g["conv_ids"]:
            continue
        tid = "t_" + uuid.uuid4().hex[:12]
        summary = "；".join(g["lines"][:3])[:180]
        await db.execute(
            """INSERT INTO topics (id, user_id, title, status, summary, started_at,
                                   ended_at, conv_ids, conv_count, keywords)
               VALUES ($1,$2,$3,'active',$4,$5,$6,$7::jsonb,$8,$9::jsonb)""",
            tid,
            uid,
            g["title"][:40],
            summary,
            min(g["starts"]) if g["starts"] else _now(),
            max(g["starts"]) if g["starts"] else None,
            json.dumps(g["conv_ids"], ensure_ascii=False),
            len(g["conv_ids"]),
            json.dumps(g["keywords"][:6], ensure_ascii=False),
        )
        await db.execute(
            "UPDATE conv_digests SET topic_id = $1 WHERE user_id = $2 AND conversation_id = ANY($3::text[])",
            tid,
            uid,
            g["conv_ids"],
        )
        await store.log_event(uid, "topic_start", tid, f"话题：{g['title']}（{len(g['conv_ids'])} 段对话）")
    log.info("topics built: %s conversations -> %s topics", len(rows), len(ordered))
    return len(ordered)


# ------------------------------------------------------------------ ④ 画像

PROFILE_SYSTEM = """你在为一个人写"用户画像"，供 AI 助手在对话中理解他。只根据给出的事实，禁止编造。

只输出 JSON，不要解释：
{
 "headline":"一句话说清这个人是谁、在忙什么（40 字内）",
 "basics":[{"k":"身份","v":"…"},{"k":"所在","v":"…"}],
 "traits":[{"k":"沟通习惯","v":"…"},{"k":"做事方式","v":"…"}],
 "interests":[{"k":"…","v":"…"}],
 "values":[{"k":"…","v":"…"}],
 "ongoing":[{"k":"正在做","v":"…"}]
}

规则：
- 每项 v 都是具体的一句话，不要"热爱学习""积极向上"这种空话。
- 没有证据的字段直接省略，不要填空字符串凑数。
- basics/traits 各 2-5 条，interests/values/ongoing 各 0-4 条。
- 用第三人称（"他"），不要用"用户"。

输入分四部分，可信度从高到低：
0. 「他亲自更正过的」—— 他本人在记忆面板里编辑/删除时留下的裁决，**最高优先级**。
   标了"已删除（本人否认）"的内容绝对不能出现在画像里，哪怕下面的记忆里有；
   标了"已更正为"的就照那个说法写。
1. 「他自己写下的」—— 用户亲手录入的，是本人明确声明的事实。
2. 「系统从对话里推断的」—— 可能过时或有误，只作补充。
3. 「话题」「最近聊过」—— 用来判断"他正在忙什么"。

关于第 1 部分怎么用（很容易做错）：
- 只在**同一条事实**冲突时以它为准。比如手写说"我在深圳"、推断说"他在广州"，取深圳。
- 不冲突的事实**必须都保留**，不要因为手写了三条就拿手写覆盖全部画像。
  一个人手写"我是高中生、INTJ"，同时也可以是公司创始人 —— 这两件事不矛盾。
- headline 要挑**证据最充分、最能代表他**的那条身份，不要默认只用手写那句。
- 「他自己写下的」为空时，完全按第 2、3 部分写，并且不要提"用户没有手写记忆"这件事。

一个常见错误，务必避免：对话里提到某个人（朋友、同事、采访对象、他读到的案例），
不等于他自己是那个人。只有明确是他本人的属性才写进画像；
聊天里描述的第三方人物，即使描述得很详细，也不要写成"他是…"。"""


async def _build_profile(uid: str, job_id: Optional[str] = None) -> dict[str, Any]:
    """汇总记忆库 + 话题 + 近期对话 → 全局画像。

    ## 用户手写的记忆必须排在最前

    记忆库里有两个来源：机器从对话里推断的（`source.platform` = brain/import），
    和用户自己在记忆面板里手打的（`platform` = manual）。
    两者的可信度不是一个量级 —— 手打的是他**明确告诉你**的事实，
    推断的是模型从聊天里猜的。

    但原来的取数只有一条 `ORDER BY confidence DESC, created_at DESC LIMIT 120`：
    手写记忆的 confidence 默认 0.8，会被一堆 0.95/1.0 的推断记忆挤出前 120，
    于是"我自己加了 30 条，画像里一条都看不见"。用户会以为这个功能没生效。

    所以手写记忆单独取一份、单独成组、并在 prompt 里说明权重，
    剩下的位置再按置信度补推断记忆。
    """
    # ① 用户亲手写的 —— 全取（上限 40），不参与置信度竞争
    manual = await db.fetch(
        """SELECT layer, kind, text FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL
             AND coalesce(source->>'platform', '') = 'manual'
           ORDER BY created_at DESC LIMIT 40""",
        uid,
    )
    manual_texts = {r["text"] for r in manual}

    # ② 机器推断的 —— 按置信度取，跳过已入选的手写条目
    mem = await db.fetch(
        """SELECT layer, text FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL AND scope = 'global'
           ORDER BY confidence DESC, created_at DESC LIMIT 160""",
        uid,
    )
    mem = [r for r in mem if r["text"] not in manual_texts][:120]

    if len(manual) + len(mem) < PROFILE_MIN_MEM:
        return {}

    topics = await db.fetch(
        "SELECT title, summary FROM topics WHERE user_id = $1 ORDER BY started_at DESC LIMIT 20", uid
    )
    digests = await db.fetch(
        """SELECT one_line FROM conv_digests
           WHERE user_id = $1 AND status = 'done' AND one_line <> ''
           ORDER BY started_at DESC LIMIT 60""",
        uid,
    )
    by_layer: dict[str, list[str]] = {}
    for r in mem:
        by_layer.setdefault(r["layer"] or "context", []).append(r["text"])

    payload: dict[str, Any] = {}
    if manual:
        by_layer_m: dict[str, list[str]] = {}
        for r in manual:
            by_layer_m.setdefault(r["layer"] or "context", []).append(r["text"])
        payload["他自己写下的（最可信，优先采纳）"] = {k: v[:20] for k, v in by_layer_m.items()}
    payload["系统从对话里推断的（按层）"] = {k: v[:30] for k, v in by_layer.items()}
    payload["话题"] = [{"标题": t["title"], "摘要": t["summary"]} for t in topics]
    payload["最近聊过"] = [d["one_line"] for d in digests]

    # 用户的更正排在最前面：这是他亲口裁决过的对错，
    # 优先级高于任何推断，也高于他自己早年手写的记忆（可能是错的）。
    corrections = await store.correction_digest(uid)
    if corrections:
        payload = {"他亲自更正过的（最高优先级，必须遵守）": corrections, **payload}

    if job_id:
        await _job_set(job_id, stage="生成用户画像")
    raw = await _ask(PROFILE_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=8000)
    data = _json_obj(raw, require=("headline", "basics", "traits"))
    if not data:
        log.warning("profile empty after parse, head=%s", (raw or "")[:200])
        return {}

    def _clean_list(v: Any, limit: int) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in v if isinstance(v, list) else []:
            if not isinstance(item, dict):
                continue
            k = str(item.get("k") or item.get("key") or "").strip()[:16]
            val = str(item.get("v") or item.get("value") or "").strip()[:120]
            if k and val:
                out.append({"k": k, "v": val})
        return out[:limit]

    profile = {
        "headline": str(data.get("headline") or "").strip()[:80],
        "basics": _clean_list(data.get("basics"), 6),
        "traits": _clean_list(data.get("traits"), 6),
        "interests": _clean_list(data.get("interests"), 5),
        "values": _clean_list(data.get("values"), 5),
        "ongoing": _clean_list(data.get("ongoing"), 5),
        "built_from": {
            "memories": len(mem) + len(manual),
            "manual": len(manual),
            "topics": len(topics),
            "conversations": len(digests),
        },
        "generated_at": _now().isoformat(),
    }
    if not profile["basics"] and not profile["traits"]:
        log.warning("profile fields empty after clean, keys=%s", list(data)[:8])
        return {}
    await store.set_profile(uid, profile)
    return profile


# ------------------------------------------------------------------ 增量更新

# 哪些事件算"记忆库变了"。memory_merged 也要算：合并是同一条被改写，
# 内容可能已经不同了，画像该跟上。
_MEM_CHANGES = ("memory_added", "preference_change", "memory_merged", "memory_deleted")


async def pending_changes(user_id: str) -> int:
    """上次生成画像之后，记忆库变动了多少次。

    用事件表而不是"记忆条数差"：条数会被去重合并吃掉（加 3 条、合并掉 2 条，
    净增只有 1），而删除又会让条数变少 —— 拿差值判断永远算不准。
    `memory_events` 里每一次添加/合并/删除都有一行，是最可靠的计数口径。
    """
    row = await db.fetchrow(
        """SELECT count(*) AS n FROM memory_events
           WHERE user_id = $1
             AND type = ANY($2::text[])
             AND at > COALESCE(
                   (SELECT max(at) FROM memory_events
                     WHERE user_id = $1 AND type = 'profile_updated'),
                   '-infinity'::timestamptz)""",
        user_id,
        list(_MEM_CHANGES),
    )
    return int(row["n"] or 0) if row else 0


async def maybe_refresh_profile(
    user_id: str, *, force: bool = False, threshold: Optional[int] = None
) -> bool:
    """记忆攒够了就在后台重算画像。返回是否真的起了任务。

    调用点有三处：在线抽取记忆后、用户手动加记忆后、手动删记忆后 ——
    也就是说不管记忆从哪来（聊出来的还是自己敲的），都会走到这里。

    `threshold` 允许调用方压低门槛。删除用比添加更低的门槛：
    用户主动删掉一条，说明他不想让这条继续影响回答；如果还按 8 条算，
    他会看到"我明明删了，画像里还在提"。删两条就重算，代价可以接受
    （一次便宜模型调用，后台跑，不挡请求）。
    """
    limit = PROFILE_REFRESH_EVERY if threshold is None else threshold
    if limit <= 0 and not force:
        return False
    if user_id in _profile_busy:
        return False

    try:
        n = await pending_changes(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("pending_changes failed: %s", exc)
        return False

    if not force and n < limit:
        log.debug("profile refresh skipped user=%s pending=%s/%s", user_id, n, limit)
        return False

    _profile_busy.add(user_id)

    async def _run() -> None:
        try:
            profile = await _build_profile(user_id)
            if profile:
                log.info(
                    "profile refreshed user=%s pending=%s memories=%s manual=%s",
                    user_id, n, profile["built_from"]["memories"], profile["built_from"]["manual"],
                )
            else:
                # 生成失败不写库，也不写 profile_updated —— 否则计数被清零，
                # 下一次要再攒 8 条才会重试，等于把一次失败放大成很久的沉默。
                log.warning("profile refresh produced nothing user=%s", user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("profile refresh failed user=%s: %s", user_id, exc)
        finally:
            _profile_busy.discard(user_id)

    asyncio.create_task(_run())
    return True


# ------------------------------------------------------------------ 主线

async def rebuild(
    user_id: str,
    *,
    force: bool = False,
    limit: int = 1200,
    rebuild_topics: bool = True,
) -> str:
    """起一个后台任务，返回 job_id。调用方（接口）立刻返回，不阻塞。"""
    job_id = await _job_new(user_id, "backfill")
    asyncio.create_task(_run(user_id, job_id, force=force, limit=limit, rebuild_topics=rebuild_topics))
    return job_id


async def _run(
    uid: str,
    job_id: str,
    *,
    force: bool,
    limit: int,
    rebuild_topics: bool,
) -> None:
    try:
        if not mg.ready():
            mg.connect()
        convs = await asyncio.to_thread(mg.list_conversations, uid, limit)
        if not convs:
            await _job_set(job_id, status="done", stage="没有可读的历史对话", total=0, done=0)
            return

        # 已处理过的跳过（force 时全部重做）
        if not force:
            done_ids = {
                r["conversation_id"]
                for r in await db.fetch(
                    "SELECT conversation_id FROM conv_digests WHERE user_id = $1 AND status = 'done'", uid
                )
            }
            todo = [c for c in convs if c["id"] not in done_ids]
        else:
            todo = list(convs)

        await _job_set(job_id, total=len(todo), done=0, stage="读取历史对话")
        log.info("mem-build start user=%s total=%s todo=%s", uid, len(convs), len(todo))

        sem = asyncio.Semaphore(CONCURRENCY)
        finished = 0
        total_mem = 0
        for chunk_start in range(0, len(todo), CONCURRENCY * 2):
            chunk = todo[chunk_start: chunk_start + CONCURRENCY * 2]
            results = await asyncio.gather(
                *[_process_conv(uid, c, sem, force=force) for c in chunk], return_exceptions=True
            )
            for r in results:
                if isinstance(r, dict):
                    total_mem += int(r.get("memories") or 0)
            finished += len(chunk)
            await _job_set(
                job_id,
                done=finished,
                stage=f"读对话并抽取记忆 {finished}/{len(todo)}",
                stats={"memories": total_mem},
            )

        # ---- 话题 ----
        n_topics = 0
        if rebuild_topics:
            # 已有话题且没有新对话 → 只做归并，不重判归属。
            # 重判一次要读 414 条摘要、跑 14 次模型调用、约九分钟，
            # 而结果和上一次几乎一样 —— 用户点第二次按钮时没必要再等一遍。
            have = await db.fetchrow(
                "SELECT count(*) AS n FROM topics WHERE user_id = $1", uid
            )
            # 阈值 2：只剩 1 个话题说明上次归并把它压塌了，要重判
            if not force and have and int(have["n"]) > 2 and not todo:
                n_topics = int(have["n"])
                await _job_set(job_id, stage=f"整理话题（{n_topics} 个）")
            else:
                rows = await db.fetch(
                    """SELECT conversation_id, title, one_line, tags, started_at, ended_at
                       FROM conv_digests
                       WHERE user_id = $1 AND status = 'done'
                       ORDER BY started_at ASC""",
                    uid,
                )
                n_topics = await _build_topics(uid, [dict(r) for r in rows], job_id)
            await _job_set(job_id, stage="生成用户画像", stats={"memories": total_mem, "topics": n_topics})

        # ---- 画像 ----
        profile = await _build_profile(uid, job_id)

        total = await db.fetchrow(
            """SELECT count(*) AS n FROM memories
               WHERE user_id = $1 AND deleted_at IS NULL AND scope = 'global'""",
            uid,
        )
        # 最后再查一次话题数：中途那次 stats 会被画像阶段的覆盖掉，
        # 结果就是"整理完成但界面上不显示有几个话题"。
        trow = await db.fetchrow("SELECT count(*) AS n FROM topics WHERE user_id = $1", uid)
        crow = await db.fetchrow("SELECT count(*) AS n FROM conv_digests WHERE user_id = $1", uid)
        await _job_set(
            job_id,
            status="done",
            stage="完成",
            done=finished,
            stats={
                "memories": int(total["n"]) if total else total_mem,
                "new_memories": total_mem,
                "conversations": int(crow["n"]) if crow else finished,
                "new_conversations": finished,
                "topics": int(trow["n"]) if trow else n_topics,
                "profile": bool(profile),
            },
        )
        await store.log_event(uid, "import", job_id, f"从 {finished} 个历史对话整理了记忆")
        log.info("mem-build done user=%s convs=%s mem=%s profile=%s", uid, finished, total_mem, bool(profile))
    except Exception as exc:  # noqa: BLE001
        log.exception("mem-build failed")
        await _job_set(job_id, status="failed", stage="失败", error=str(exc)[:400])


# ------------------------------------------------------------------ 两端共用的读模型

async def insight(user_id: str, *, with_review: bool = False) -> dict[str, Any]:
    """记忆板块的全部内容。用户端与后台共用这一个函数，保证"看到的完全一致"。

    唯一按角色裁掉的是 review（站长对这个人的内部评价）——
    它是对人的判断，不该出现在被评价者自己的页面上。
    """
    profile = await store.get_profile(user_id)
    long_term = await db.fetch(
        """SELECT id, layer, kind, text, confidence, scope, topic_id, entities,
                  valid_from, valid_to, created_at
           FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL AND scope = 'global'
           ORDER BY created_at DESC LIMIT 400""",
        user_id,
    )
    topics = await db.fetch(
        """SELECT id, title, status, summary, started_at, ended_at, conv_count, keywords
           FROM topics WHERE user_id = $1 ORDER BY started_at DESC LIMIT 200""",
        user_id,
    )
    topic_mem = await db.fetch(
        """SELECT id, topic_id, layer, text, created_at FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL AND scope <> 'global'
           ORDER BY created_at DESC LIMIT 200""",
        user_id,
    )
    digests = await db.fetch(
        """SELECT conversation_id, title, one_line, tags, started_at, ended_at,
                  msg_count, memories, topic_id
           FROM conv_digests WHERE user_id = $1
           ORDER BY started_at DESC LIMIT 500""",
        user_id,
    )
    events = await store.timeline(user_id, limit=200)
    summaries = await db.fetch(
        """SELECT conversation_id, summary, covered_messages, tokens, updated_at
           FROM conversation_summaries WHERE user_id = $1
           ORDER BY updated_at DESC LIMIT 30""",
        user_id,
    )

    by_layer: dict[str, int] = {}
    for r in long_term:
        by_layer[r["layer"] or "context"] = by_layer.get(r["layer"] or "context", 0) + 1

    days = _by_day(digests, long_term, events)

    out: dict[str, Any] = {
        "profile": profile,
        "long_term": [dict(r) for r in long_term],
        "topics": [dict(r) for r in topics],
        "topic_memories": [dict(r) for r in topic_mem],
        "digests": [dict(r) for r in digests],
        "timeline": [dict(r) for r in events],
        "days": days,
        "short_term": [dict(r) for r in summaries],
        "by_layer": by_layer,
        "stats": {
            "memories": len(long_term),
            # 用真实总数，不用 len(topics)：列表最多带回 200 条做展示，
            # 拿它的长度当"话题数"会在超过上限时永远少报（曾经 61 报成 60，
            # 而同一页面的整理统计写的是 61，自己跟自己打架）。
            "topics": int((await db.fetchrow(
                "SELECT count(*) AS n FROM topics WHERE user_id = $1", user_id
            ))["n"] or 0),
            "conversations": len(digests),
            "messages": sum(int(d["msg_count"] or 0) for d in digests),
            "days": len(days),
            "first_at": min((d["started_at"] for d in digests if d["started_at"]), default=None),
            "last_at": max((d["started_at"] for d in digests if d["started_at"]), default=None),
        },
        "job": await job_status(user_id),
        "compression": {
            "enabled": True,
            "summaries": len(summaries),
            "covered_messages": sum(int(s["covered_messages"] or 0) for s in summaries),
        },
        # 他做过的更正。两端都返回：用户要看到"我改了什么都记着"，
        # 站长要看到"这个人纠正过哪些"，否则两边对同一份数据的理解会分叉。
        "corrections": await store.list_corrections(user_id, limit=60),
    }
    if with_review:
        rev = await db.fetchrow(
            "SELECT summary, tags, generated_at, model FROM user_reviews WHERE user_id = $1", user_id
        )
        out["review"] = (
            {
                "summary": rev["summary"],
                "tags": _tags(rev["tags"]),
                "generated_at": rev["generated_at"],
                "model": rev["model"],
            }
            if rev
            else None
        )
    return _normalize(out)


def _jsonval(v: Any) -> Any:
    """jsonb 列经 asyncpg 出来是**字符串**，不是对象。

    这一步不补，前端就得人人自己 JSON.parse：画像面板里会直接印出
    `[{"k":"身份","v":"..."}]` 这种原始 JSON，整理统计会读成 0
    （`"{\"memories\": 277}"` 上的 `.memories` 是 undefined）。
    用户端和后台是两个前端，与其各写一遍，不如在这里统一成真对象。"""
    if v is None or isinstance(v, (list, dict, int, float, bool)):
        return v
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return v


def _normalize(out: dict[str, Any]) -> dict[str, Any]:
    for r in out.get("long_term") or []:
        r["entities"] = _jsonval(r.get("entities")) or []
    for t in out.get("topics") or []:
        t["keywords"] = _jsonval(t.get("keywords")) or []
    for d in out.get("digests") or []:
        d["tags"] = _jsonval(d.get("tags")) or []
    job = out.get("job")
    if isinstance(job, dict):
        job["stats"] = _jsonval(job.get("stats")) or {}
    return out


def _tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        v = json.loads(raw or "[]")
        return [str(t) for t in v] if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _by_day(
    digests: list[Any], memories: list[Any], events: list[Any]
) -> list[dict[str, Any]]:
    """时间线：把会话和记忆按天装桶。

    为什么按天而不是按会话：414 个对话平铺是"列表"，不是"时间线"。
    人回看自己的一年，单位是"那阵子在忙什么"，天是最小的可读刻度；
    同一天里聊了很多，就聚成一条，点开看细节。
    """
    buckets: dict[str, dict[str, Any]] = {}

    def _day(v: Any) -> str:
        if not v:
            return ""
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        return str(v)[:10]

    for d in digests:
        day = _day(d["started_at"]) or _day(d["ended_at"])
        if not day:
            continue
        b = buckets.setdefault(day, {"date": day, "conversations": [], "memories": 0, "topics": set()})
        b["conversations"].append(
            {
                "id": d["conversation_id"],
                "title": d["title"],
                "one_line": d["one_line"],
                "tags": _tags(d["tags"]),
                "msgs": d["msg_count"],
            }
        )
        b["memories"] += int(d["memories"] or 0)

    for e in events:
        day = _day(e["at"])
        if not day or day not in buckets:
            continue
        buckets[day]["topics"].add(e["type"])

    out = []
    for day in sorted(buckets, reverse=True):
        b = buckets[day]
        b["topics"] = sorted(b["topics"])
        out.append(b)
    return out
