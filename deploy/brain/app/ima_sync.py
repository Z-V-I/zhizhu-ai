"""把站内数据整理成文档，推送到 IMA 知识库。

## 用户要的是什么

「把我们的数据库（全量，含用户数据、运维数据）传进 ima 知识库（使用 apikey）、
然后要查询时使用 apikey 问 ima 得到回答。」

所以这里的职责只有一件：**把 Postgres / Mongo 里的东西变成 IMA 读得懂的 markdown**。
不自己建索引、不做向量检索 —— 那是 IMA 的活。

## 分档策略（为什么不是一个大 SQL dump）

直接把 `pg_dump` 甩进去，IMA 检索出来的是几百行 INSERT 语句，答不了"这个用户最近在忙什么"。
人读得懂的文档，模型也才读得懂。所以按"一问一答能用上"的单位切：

  站点总览.md        用户数/消息量/日活/登录情况——回答"站点整体怎么样"
  用户-<昵称>.md     单人档案：画像、记忆、话题、时间线、活跃度——回答"这个人怎么样"
  运维日志-<月>.md   登录记录、整理任务、导入批次——回答"最近有没有异常"

单文件按 60KB 左右切分，避免单篇过大导致检索命中但读不完。

## 幂等

用 `import_batches` 之外的独立表 `ima_sync_state` 记每个文档的 media_id 与内容哈希：
内容没变就跳过，变了就重新上传（IMA 没有"更新文件"接口，只能重传）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import db, ima, llm, mg, store

log = logging.getLogger("brain.imasync")

MAX_DOC_BYTES = 60_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS ima_sync_state (
    id           BIGSERIAL PRIMARY KEY,
    doc_key      TEXT NOT NULL UNIQUE,     -- 例如 user:6b01… / site:overview
    kb_id        TEXT NOT NULL,
    title        TEXT NOT NULL,
    media_id     TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    bytes        INT NOT NULL DEFAULT 0,
    synced_at    TIMESTAMPTZ,
    error        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ima_state_kb ON ima_sync_state (kb_id);
"""

# 问答会话：后台「问 AI」是一块能连续聊的聊天区，所以问题与回答要落库。
# 放 Postgres 而不是浏览器 localStorage：换台机器、换个浏览器还能翻回去看，
# 而且站长问过的结论本身也是站内资料的一部分。
CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS ima_chats (
    id         TEXT PRIMARY KEY,
    kb_id      TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ima_chat_msgs (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL REFERENCES ima_chats(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,                                -- user | assistant
    content    TEXT NOT NULL,
    sources    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ima_msgs_chat ON ima_chat_msgs (chat_id, id);
CREATE INDEX IF NOT EXISTS idx_ima_chats_up ON ima_chats (updated_at DESC);
"""


async def ensure_schema() -> None:
    await db.execute(SCHEMA)
    await db.execute(CHAT_SCHEMA)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)


def _json(v: Any) -> dict:
    """asyncpg 把 JSONB 当字符串返回（没注册 codec），直接 .get 会全变成 "?"。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}
    return {}


# ------------------------------------------------------------------ 文档生成

async def _site_overview() -> str:
    """全站运维总览。回答"站点最近怎么样、有没有异常"。"""
    L: list[str] = ["# 站点总览", ""]
    rows = await db.fetch(
        """SELECT
             (SELECT count(*) FROM memories WHERE deleted_at IS NULL) AS memories,
             (SELECT count(DISTINCT user_id) FROM memories WHERE deleted_at IS NULL) AS mem_users,
             (SELECT count(*) FROM topics) AS topics,
             (SELECT count(*) FROM conv_digests) AS digests,
             (SELECT count(*) FROM login_events) AS logins,
             (SELECT count(*) FROM login_events WHERE ok) AS logins_ok,
             (SELECT count(*) FROM invites) AS invites,
             (SELECT count(*) FROM invites WHERE used_at IS NOT NULL) AS invites_used,
             (SELECT count(*) FROM memory_jobs) AS jobs"""
    )
    r = rows[0]
    L += [
        "## 规模",
        f"- 记忆条目：{r['memories']} 条，分布在 {r['mem_users']} 个用户",
        f"- 话题：{r['topics']} 个；已归纳的对话摘要：{r['digests']} 段",
        f"- 登录记录：{r['logins']} 次（成功 {r['logins_ok']} 次）",
        f"- 邀请码：{r['invites']} 个，已使用 {r['invites_used']} 个",
        f"- 记忆整理任务：{r['jobs']} 次",
        "",
    ]

    # 最近 30 天日活
    daily = await db.fetch(
        """SELECT to_char(at::date, 'YYYY-MM-DD') AS d,
                  count(*) AS n,
                  count(DISTINCT user_id) AS users,
                  count(*) FILTER (WHERE NOT ok) AS fails
           FROM login_events
           WHERE at > now() - interval '30 days'
           GROUP BY 1 ORDER BY 1 DESC LIMIT 30"""
    )
    if daily:
        L += ["## 最近 30 天登录", "", "| 日期 | 次数 | 人数 | 失败 |", "|---|---|---|---|"]
        for d in daily:
            L.append(f"| {d['d']} | {d['n']} | {d['users']} | {d['fails']} |")
        L.append("")

    # 登录失败原因
    fails = await db.fetch(
        """SELECT coalesce(nullif(reason,''),'未说明') AS reason, count(*) AS n
           FROM login_events WHERE NOT ok GROUP BY 1 ORDER BY n DESC LIMIT 8"""
    )
    if fails:
        L += ["## 登录失败原因", ""]
        L += [f"- {f['reason']}：{f['n']} 次" for f in fails]
        L.append("")

    # 整理任务
    jobs = await db.fetch(
        """SELECT id, status, stage, done, total, error, started_at, updated_at
           FROM memory_jobs ORDER BY started_at DESC LIMIT 10"""
    )
    if jobs:
        L += ["## 最近的记忆整理任务", ""]
        for j in jobs:
            L.append(
                f"- {_fmt(j['started_at'])}：{j['status']}，进度 {j['done']}/{j['total']}"
                + (f"，错误：{j['error']}" if j.get("error") else "")
            )
        L.append("")

    # 导入批次
    batches = await db.fetch(
        """SELECT DISTINCT ON (platform, file_name) platform, file_name, status, stats, created_at
           FROM import_batches
           ORDER BY platform, file_name, created_at DESC"""
    )
    if batches:
        L += ["## 导入的备份（每份只列最近一次）", ""]
        for b in sorted(batches, key=lambda x: x["created_at"], reverse=True)[:10]:
            st = _json(b["stats"])
            L.append(
                f"- {_fmt(b['created_at'])}：{b['platform']} / {b['file_name']}，"
                f"{st.get('conversations', '?')} 段对话、{st.get('messages', '?')} 条消息"
            )
        L.append("")

    # 模型配置
    provs = await db.fetch("SELECT code, label, enabled, model_id FROM providers ORDER BY code")
    if provs:
        L += ["## 已配置的模型厂商", ""]
        for p in provs:
            L.append(
                f"- {p['label'] or p['code']}：{'启用' if p['enabled'] else '停用'}"
                + (f"，当前模型 {p['model_id']}" if p.get("model_id") else "")
            )
        L.append("")

    # 刻意不写"生成时间"：它每次都不一样，会让内容哈希每次都变，
    # 于是每次同步都往 IMA 里多传一份同内容的文档（IMA 没有更新/删除接口）。
    return "\n".join(L)


def _user_identity(uid: str) -> tuple[str, str]:
    """(显示名, 邮箱)。只给文档标题用：标题里带名字，IMA 的检索才找得到人。"""
    if not mg.ready():
        return "", ""
    try:
        doc = mg.col("users").find_one({"_id": _oid(uid)}, {"name": 1, "email": 1})
    except Exception:  # noqa: BLE001
        return "", ""
    if not doc:
        return "", ""
    return str(doc.get("name") or ""), str(doc.get("email") or "")


def _user_doc_name(uid: str) -> str:
    """用户档案的文件名。

    ★ 必须把姓名/邮箱写进标题：IMA 的 OpenAPI 检索是按**文件名**匹配的
      （正文不参与），标题只写 uuid 的话，问"示例用户最近在忙什么"永远 0 命中。
      尾部保留 8 位 id，重名时不会互相覆盖，也方便按用户 id 定位。
    """
    name, email = _user_identity(uid)
    label = (name or email.split("@")[0] or "用户").replace("/", "_")[:24]
    return f"用户档案-{label}-{uid[:8]}.md"


async def _user_doc(uid: str) -> str:
    """单人档案。回答"这个人最近在忙什么、状态如何"。"""
    prof = await store.get_profile(uid)
    who = ""
    name, email = _user_identity(uid)
    if name or email:
        who = f"{name or ''} / {email or ''}".strip(" /")

    L: list[str] = [f"# 用户档案：{who or uid}", ""]
    if who:
        L += [f"- 用户 ID：{uid}", ""]
    if prof.get("headline"):
        L += ["## 一句话画像", prof["headline"], ""]

    for key, label in (
        ("basics", "身份与状态"),
        ("traits", "性格与习惯"),
        ("interests", "在意的东西"),
        ("values", "看重什么"),
        ("ongoing", "正在做"),
    ):
        items = prof.get(key) or []
        if not items:
            continue
        L.append(f"## {label}")
        for it in items:
            if isinstance(it, dict):
                L.append(f"- **{it.get('k', '')}**：{it.get('v', '')}")
        L.append("")

    # 记忆（按层）
    mem = await db.fetch(
        """SELECT layer, text FROM memories
           WHERE user_id = $1 AND deleted_at IS NULL ORDER BY confidence DESC, created_at DESC LIMIT 120""",
        uid,
    )
    if mem:
        LABEL = {"identity": "身份", "preference": "偏好", "experience": "经验", "activity": "进行中", "context": "上下文"}
        L += ["## 长期记忆", ""]
        by: dict[str, list[str]] = {}
        for m in mem:
            by.setdefault(LABEL.get(m["layer"], "其它"), []).append(m["text"])
        for layer, texts in by.items():
            L.append(f"### {layer}")
            L += [f"- {t}" for t in texts]
            L.append("")

    # 话题
    tps = await db.fetch(
        """SELECT title, summary, conv_count, started_at, ended_at FROM topics
           WHERE user_id = $1 ORDER BY started_at DESC LIMIT 40""",
        uid,
    )
    if tps:
        L += ["## 聊过的话题", ""]
        for t in tps:
            L.append(
                f"- **{t['title']}**（{t['conv_count']} 段，"
                f"{_fmt(t['started_at'])} ~ {_fmt(t['ended_at'])}）：{(t['summary'] or '')[:300]}"
            )
        L.append("")

    # 时间线（按月汇总，太细没用）
    days = await db.fetch(
        """SELECT to_char(started_at, 'YYYY-MM') AS m, count(*) AS n
           FROM conv_digests WHERE user_id = $1 AND started_at IS NOT NULL
           GROUP BY 1 ORDER BY 1 DESC LIMIT 24""",
        uid,
    )
    if days:
        L += ["## 活跃月份（对话数）", ""]
        L += [f"- {d['m']}：{d['n']} 段" for d in days]
        L.append("")

    # 登录活跃
    lg = await db.fetch(
        """SELECT to_char(at, 'YYYY-MM-DD') AS d, count(*) AS n
           FROM login_events WHERE user_id = $1 AND at > now() - interval '60 days'
           GROUP BY 1 ORDER BY 1 DESC LIMIT 30""",
        uid,
    )
    if lg:
        L += ["## 最近 60 天登录", ""]
        L += [f"- {x['d']}：{x['n']} 次" for x in lg]
        L.append("")

    # 他做过的更正 —— 这些是他亲自裁决的对错，回答时不能违背
    corr = await store.list_corrections(uid, limit=30)
    if corr:
        L += ["## 他本人更正过的内容（以此为准）", ""]
        for c in corr:
            if c["kind"] in ("memory_delete", "topic_delete"):
                L.append(f"- 已否认，不要再提：{c['before_text'][:160]}")
            else:
                L.append(f"- 已更正为：{c['after_text'][:160]}")
        L.append("")

    # 站内评价（如果有）
    rev = await db.fetchrow("SELECT summary, tags, generated_at FROM user_reviews WHERE user_id = $1", uid)
    if rev:
        L += ["## 站长此前的评价", rev["summary"] or "", ""]

    # 刻意不写"生成时间"：它每次都不一样，会让内容哈希每次都变，
    # 于是每次同步都往 IMA 里多传一份同内容的文档（IMA 没有更新/删除接口）。
    return "\n".join(L)


def _oid(uid: str):
    try:
        import bson

        return bson.ObjectId(uid)
    except Exception:  # noqa: BLE001
        return uid


async def _data_doc() -> str:
    """站点数据量：账号、会话、消息、附件。

    为什么单独一份、不并进「站点总览」：IMA 只能新增、不能改也不能删，
    任何一次内容变化都会在知识库里多出一份**同名**文档，用户根本分不清哪份是新的。
    单独成文至少保证"改结构"这一步不产生同名重复。
    """
    L: list[str] = ["# 站点数据量", ""]
    if not mg.ready():
        return "# 站点数据量\n\n（Mongo 未连接，无法统计账号与会话）\n"

    try:
        users = mg.col("users").count_documents({})
        convs = mg.col("conversations").count_documents({})
        msgs = mg.col("messages").count_documents({})
        user_msgs = mg.col("messages").count_documents({"isCreatedByUser": True})
        files = mg.col("files").count_documents({})
        L += [
            "## 总量（LibreChat 数据库）",
            f"- 账号：{users} 个",
            f"- 会话：{convs} 段",
            f"- 消息：{msgs} 条（其中用户发出 {user_msgs} 条）",
            f"- 附件：{files} 个",
            "",
        ]

        L += ["## 每个人有多少会话、多少消息", "", "| 账号 | 姓名 | 会话 | 消息 |", "|---|---|---|---|"]
        for u in mg.col("users").find({}, {"name": 1, "email": 1}).limit(50):
            uid = str(u["_id"])
            # ★ conversations.user / messages.user 有两种写法：LibreChat 原生写 ObjectId，
            #   我们导入的历史写字符串。只按一种查，统计出来会是 0（踩过）。
            key = {"$in": [uid, _oid(uid)]}
            c = mg.col("conversations").count_documents({"user": key})
            m = mg.col("messages").count_documents({"user": key})
            L.append(f"| {u.get('email') or uid[:8]} | {u.get('name') or '—'} | {c} | {m} |")
        L.append("")

        # 导入进来的四家平台各有多少段（按 importSourceId 前缀统计）
        try:
            agg = mg.col("conversations").aggregate(
                [
                    {"$match": {"importSourceId": {"$exists": True, "$ne": ""}}},
                    {"$group": {"_id": "$importSourceId", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}},
                    {"$limit": 20},
                ]
            )
            rows = list(agg)
            if rows:
                L += ["## 导入来源", ""]
                L += [f"- {r['_id']}：{r['n']} 段" for r in rows]
                L.append("")
        except Exception as exc:  # noqa: BLE001
            log.debug("import source agg failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("data doc failed: %s", exc)
        L += [f"（统计失败：{exc}）", ""]

    return "\n".join(L)


async def _ops_doc() -> str:
    """运维明细。总览只给汇总数，问"哪次登录失败了""上次导入是什么时候"要看这份。"""
    L: list[str] = ["# 运维明细", ""]

    # 最近的登录（成功与失败都列：失败原因往往是排查的第一现场）
    lg = await db.fetch(
        """SELECT at, actor, email, ok, reason, ip
           FROM login_events ORDER BY at DESC LIMIT 120"""
    )
    if lg:
        L += ["## 最近 120 次登录", "", "| 时间 | 类型 | 账号 | 结果 | 原因 | IP |", "|---|---|---|---|---|---|"]
        for x in lg:
            L.append(
                f"| {_fmt(x['at'])} | {x['actor'] or 'user'} | {x['email'] or '—'} | "
                f"{'成功' if x['ok'] else '失败'} | {(x['reason'] or '—')[:40]} | {x['ip'] or '—'} |"
            )
        L.append("")

    # 导入批次：哪个平台、多少条、成功没有
    bt = await db.fetch(
        """SELECT DISTINCT ON (b.platform, b.file_name)
                  b.created_at, b.platform, b.file_name, b.status, b.stats,
                  (SELECT count(*) FROM import_items i WHERE i.batch_id = b.id) AS items
           FROM import_batches b
           ORDER BY b.platform, b.file_name, b.created_at DESC"""
    )
    if bt:
        # 同一个备份反复重导会产生十几条一模一样的批次；只留每份备份的最新一次，
        # 否则文档里 28 行全是"cg.zip / takeout.zip"，模型也读不出重点。
        L += ["## 导入批次（每份备份只列最近一次）", ""]
        for b in sorted(bt, key=lambda x: x["created_at"], reverse=True):
            st = _json(b["stats"])
            L.append(
                f"- {_fmt(b['created_at'])}：{b['platform']} / {b['file_name'] or '—'}，"
                f"状态 {b['status']}，{st.get('conversations', '?')} 段对话 / "
                f"{st.get('messages', '?')} 条消息，新增去重条目 {b['items']}，"
                f"重导 {st.get('resynced', 0)} 段"
            )
        L.append("")

    # 记忆整理任务（含失败的，错误原文保留）
    jobs = await db.fetch(
        """SELECT id, kind, status, stage, done, total, error, started_at
           FROM memory_jobs ORDER BY started_at DESC LIMIT 40"""
    )
    if jobs:
        L += ["## 记忆整理任务", ""]
        for j in jobs:
            L.append(
                f"- {_fmt(j['started_at'])} `{j['id']}` {j['kind']}：{j['status']}，"
                f"{j['stage'] or '—'}（{j['done']}/{j['total']}）"
                + (f"，错误：{j['error']}" if j.get("error") else "")
            )
        L.append("")

    # 用户更正：站长最该知道的"哪些信息不能再说"
    corr = await db.fetch(
        """SELECT user_id, kind, before_text, after_text, at
           FROM memory_corrections ORDER BY at DESC LIMIT 60"""
    )
    if corr:
        L += ["## 用户更正记录（以此为准）", ""]
        for c in corr:
            if c["kind"] in ("memory_delete", "topic_delete"):
                L.append(f"- {_fmt(c['at'])} 用户 {c['user_id']} 否认：{c['before_text'][:120]}")
            else:
                L.append(f"- {_fmt(c['at'])} 用户 {c['user_id']} 更正为：{(c['after_text'] or '')[:120]}")
        L.append("")

    # 刻意不写"生成时间"：它每次都不一样，会让内容哈希每次都变，
    # 于是每次同步都往 IMA 里多传一份同内容的文档（IMA 没有更新/删除接口）。
    return "\n".join(L)


async def _all_user_ids(limit: int = 60) -> list[str]:
    """站点上所有真实用户，不止"有记忆的那几个"。

    为什么要并集：新号、只导入过聊天没被整理过的人，在 memories 表里是空的，
    但他们有对话、有登录记录 —— 只按 memories 取会漏掉一半人。
    """
    ids: list[str] = []
    if mg.ready():
        try:
            for d in mg.col("users").find({}, {"_id": 1}).limit(200):
                uid = str(d.get("_id") or "")
                if uid:
                    ids.append(uid)
        except Exception as exc:  # noqa: BLE001
            log.debug("list mongo users failed: %s", exc)
    try:
        rows = await db.fetch(
            "SELECT DISTINCT user_id FROM memories WHERE deleted_at IS NULL AND user_id <> '__site__'"
        )
        for r in rows:
            uid = str(r["user_id"] or "")
            if uid and uid not in ids:
                ids.append(uid)
    except Exception as exc:  # noqa: BLE001
        log.debug("list pg users failed: %s", exc)
    return ids[:limit]


async def collect_docs(only_user: Optional[str] = None) -> list[tuple[str, str, str]]:
    """生成待上传文档。返回 [(doc_key, 文件名, 正文)]"""
    docs: list[tuple[str, str, str]] = []
    if only_user:
        docs.append((f"user:{only_user}", f"用户档案-{only_user[:8]}.md", await _user_doc(only_user)))
        return docs

    docs.append(("site:overview", "站点总览.md", await _site_overview()))
    docs.append(("site:data", "站点数据量.md", await _data_doc()))
    # 标题保持不变：IMA 没有删除接口，改一次名知识库里就多一份旧文档。
    # 运维类问句靠下面 ask() 的关键词路由定位，不依赖文件名带关键词。
    docs.append(("site:ops", "运维明细.md", await _ops_doc()))

    for uid in await _all_user_ids():
        try:
            body = await _user_doc(uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("build user doc failed %s: %s", uid, exc)
            continue
        docs.append((f"user:{uid}", _user_doc_name(uid), body))

    return docs


# ------------------------------------------------------------------ 推送

async def sync(
    kb_id: str,
    *,
    only_user: Optional[str] = None,
    force: bool = False,
    on_progress=None,
) -> dict[str, Any]:
    """把文档推到 IMA。返回统计。

    内容哈希没变就跳过：IMA 没有"更新文档"接口，每次都是新上传一份，
    不跳过的话知识库里会堆满同名文件，检索结果全是重复。
    """
    await ensure_schema()
    docs = await collect_docs(only_user)
    stats = {"total": len(docs), "uploaded": 0, "skipped": 0, "failed": 0, "errors": []}

    for i, (key, name, body) in enumerate(docs, 1):
        if on_progress:
            on_progress(i, len(docs), name)
        raw = body.encode("utf-8")
        if len(raw) > MAX_DOC_BYTES:
            body = body[: MAX_DOC_BYTES // 3] + "\n\n…（内容过长，已截断）\n"
            raw = body.encode("utf-8")
        h = _h(body)

        prev = await db.fetchrow(
            "SELECT content_hash, media_id FROM ima_sync_state WHERE doc_key = $1 AND kb_id = $2",
            key,
            kb_id,
        )
        if prev and prev["content_hash"] == h and not force:
            stats["skipped"] += 1
            continue

        try:
            got = await ima.upload_text(kb_id, name, body, media_type=ima.MT["markdown"])
            media_id = str(got.get("media_id") or got.get("id") or "")
            await db.execute(
                """INSERT INTO ima_sync_state (doc_key, kb_id, title, media_id, content_hash, bytes, synced_at)
                   VALUES ($1,$2,$3,$4,$5,$6,now())
                   ON CONFLICT (doc_key) DO UPDATE
                     SET kb_id=$2, title=$3, media_id=$4, content_hash=$5, bytes=$6,
                         synced_at=now(), error=NULL, updated_at=now()""",
                key,
                kb_id,
                name,
                media_id,
                h,
                len(raw),
            )
            stats["uploaded"] += 1
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:200]
            log.warning("ima upload failed %s: %s", name, msg)
            stats["failed"] += 1
            stats["errors"].append(f"{name}: {msg}")
            await db.execute(
                """INSERT INTO ima_sync_state (doc_key, kb_id, title, content_hash, error, updated_at)
                   VALUES ($1,$2,$3,$4,$5,now())
                   ON CONFLICT (doc_key) DO UPDATE
                     SET error=$5, updated_at=now()""",
                key,
                kb_id,
                name,
                h,
                msg,
            )

    return stats


# ------------------------------------------------------------------ 提问

ASK_SYSTEM = """你在回答站长关于本站的问题。资料来自 IMA 知识库（本站数据库同步上去的文档）。

规则：
- 只根据给出的资料回答。资料里没有的，直说"资料里没有"，不要推测。
- 回答要具体：带上数字、日期、名字，不要泛泛而谈。
- 引用时说清来自哪份文档（资料里标注了来源文件名）。
- 用中文，简洁，能用列表就用列表。最多 400 字。"""

# 意图 → 文档。IMA 的 OpenAPI 检索是按文件名匹配的，光靠问句原文
# （"最近登录有没有异常"）一个字也命中不了文件名，所以先按关键词把
# 该读哪几份文档定下来，再回 IMA 读正文 —— 检索范围仍然在 IMA 里。
_OPS_WORDS = ("登录", "导入", "任务", "运维", "异常", "失败", "报错", "备份", "同步", "邀请码", "模型")
_SITE_WORDS = ("站点", "整体", "总览", "多少人", "多少个", "活跃", "用户数", "全站", "数据量")


async def _inventory(kb_id: str) -> list[dict[str, Any]]:
    """本地记的"哪些文档已经在 IMA 里、media_id 是多少"。"""
    await ensure_schema()
    rows = await db.fetch(
        """SELECT doc_key, title, media_id FROM ima_sync_state
           WHERE kb_id = $1 AND media_id IS NOT NULL""",
        kb_id,
    )
    return [dict(r) for r in rows]


def _pick(inv: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    picked: dict[str, dict[str, Any]] = {}
    q = question

    def take(pred) -> None:
        for d in inv:
            if pred(d) and d["media_id"] not in picked:
                picked[d["media_id"]] = d

    # ① 问句里提到了谁的姓名 / 邮箱前缀 / 用户 id
    for d in inv:
        key = d["doc_key"]
        if not key.startswith("user:"):
            continue
        uid = key.split(":", 1)[1]
        title = d["title"]
        label = title.split("-")[1] if title.count("-") >= 2 else ""
        if uid[:8] in q or (label and label in q):
            picked[d["media_id"]] = d

    # ② 运维类问题
    if any(w in q for w in _OPS_WORDS):
        take(lambda d: d["doc_key"] == "site:ops")
    # ③ 站点整体
    if any(w in q for w in _SITE_WORDS):
        take(lambda d: d["doc_key"] == "site:overview")
    # ③b 数据量（账号/会话/消息/附件）
    if any(w in q for w in ("账号", "会话", "消息", "附件", "数据量", "多少人", "多少条", "多少段")):
        take(lambda d: d["doc_key"] == "site:data")

    # ④ 什么都没命中：给"总览 +（只看到一个用户时）这个人"
    if not picked:
        take(lambda d: d["doc_key"] == "site:overview")
        take(lambda d: d["doc_key"] == "site:ops")
        users = [d for d in inv if d["doc_key"].startswith("user:")]
        if len(users) <= 3:
            for d in users:
                picked[d["media_id"]] = d
    return list(picked.values())


async def ask(
    kb_id: str,
    question: str,
    *,
    top_k: int = 6,
    with_content: bool = True,
    history: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """向 IMA 提问：在知识库里检索 → 取回命中内容 → 让模型基于它作答。

    ## 为什么不是"直接让 IMA 回答"

    IMA OpenAPI 只给了「按名字搜条目 / 取条目正文」，没有对话接口。
    所谓"问 IMA 得到回答"，在 API 层面就是：从 IMA 找出相关条目、
    把 IMA 里的正文取回来，再让模型基于它组织答案 —— 检索和资料都在 IMA 侧。

    ## 为什么还要「按意图挑文档」

    实测（2026-11）search_knowledge 只匹配**文件名**，问句原文几乎命中不了；
    而我们的文档是自己同步上去的，本地有 doc_key → media_id 的清单。
    于是先用问句里的姓名/关键词定位到几份文档，再去 IMA 读正文。
    命中数不如语义检索漂亮，但每条依据都真实存在于 IMA 知识库里。

    ## history 做什么

    后台那块聊天区要能追问（"那第二个人呢""再细说失败原因"），
    所以把前几轮问答一起交给模型；资料仍然每轮重新从 IMA 取，
    不会因为上一轮取过就省掉检索 —— 追问换话题时也能对上。
    """
    hits: list[dict[str, Any]] = []
    try:
        hits = await ima.search(kb_id, question, limit=top_k)
    except Exception as exc:  # noqa: BLE001
        log.warning("ima search failed: %s", exc)

    docs: list[dict[str, Any]] = []
    inv = await _inventory(kb_id)
    for h in hits:
        mid = str(h.get("media_id") or "")
        if mid:
            docs.append({"media_id": mid, "title": h.get("title") or "(无标题)"})
    # 追问里往往不带姓名（"他最近呢"），只有第一轮有 ——
    # 把最近几轮用户原话拼起来一起判断，才不会退回"什么文档都没命中"。
    probe = question
    if history:
        prev = " ".join(str(h.get("content") or "") for h in history if h.get("role") == "user")[-300:]
        probe = prev + " " + question
    for d in _pick(inv, probe):
        if d["media_id"] not in {x["media_id"] for x in docs}:
            docs.append(d)
    docs = docs[:6]

    if not docs:
        return {
            "answer": "知识库里没有相关文档。先在上面点一次「开始同步」，把站内数据推进 IMA。",
            "hits": [],
            "sources": [],
        }

    sources: list[dict[str, Any]] = []
    blocks: list[str] = []
    for d in docs:
        title = d["title"]
        mid = d["media_id"]
        body = ""
        if with_content and mid:
            try:
                body = await ima.read_entry(mid)
            except Exception as exc:  # noqa: BLE001
                log.warning("read entry failed %s: %s", mid, exc)
        snippet = (body or "")[:6000]
        sources.append({"media_id": mid, "title": title})
        blocks.append(f"### 来源：{title}\n{snippet or '（只找到条目，正文未取回）'}")

    prompt = "以下是 IMA 知识库里的资料：\n\n" + "\n\n".join(blocks) + f"\n\n---\n\n问题：{question}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": ASK_SYSTEM}]
    for h in (history or [])[-8:]:
        role = "user" if h.get("role") == "user" else "assistant"
        text = str(h.get("content") or "")[:4000]
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": prompt})

    try:
        answer = await llm.complete("cheap-extract", messages, temperature=0.2, max_tokens=1600)
    except Exception as exc:  # noqa: BLE001
        log.warning("ask complete failed: %s", exc)
        answer = ""

    return {"answer": answer.strip() or "（模型没有返回内容，请重试）", "hits": hits[:top_k], "sources": sources}


# ------------------------------------------------------------------ 会话

def _title_of(question: str) -> str:
    t = " ".join(question.split())
    return (t[:24] + "…") if len(t) > 24 else (t or "新对话")


async def list_chats(limit: int = 60) -> list[dict[str, Any]]:
    await ensure_schema()
    rows = await db.fetch(
        """SELECT c.id, c.title, c.kb_id, c.created_at, c.updated_at,
                  (SELECT count(*) FROM ima_chat_msgs m WHERE m.chat_id = c.id) AS turns
           FROM ima_chats c ORDER BY c.updated_at DESC LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def create_chat(kb_id: str, title: str = "") -> dict[str, Any]:
    await ensure_schema()
    cid = "chat_" + uuid.uuid4().hex[:12]
    row = await db.fetchrow(
        """INSERT INTO ima_chats (id, kb_id, title) VALUES ($1,$2,$3)
           RETURNING id, kb_id, title, created_at, updated_at""",
        cid,
        kb_id,
        title or "新对话",
    )
    return dict(row)


async def get_chat(chat_id: str) -> Optional[dict[str, Any]]:
    await ensure_schema()
    row = await db.fetchrow(
        "SELECT id, kb_id, title, created_at, updated_at FROM ima_chats WHERE id = $1", chat_id
    )
    return dict(row) if row else None


def _arr(v: Any) -> list:
    """JSONB 数组同理：asyncpg 给的是字符串 '[]'，直接当 list 用会炸。"""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


async def chat_messages(chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
    await ensure_schema()
    rows = await db.fetch(
        """SELECT id, role, content, sources, created_at FROM ima_chat_msgs
           WHERE chat_id = $1 ORDER BY id ASC LIMIT $2""",
        chat_id,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = _arr(d.get("sources"))
        out.append(d)
    return out


async def delete_chat(chat_id: str) -> None:
    await ensure_schema()
    await db.execute("DELETE FROM ima_chats WHERE id = $1", chat_id)


async def chat_ask(chat_id: str, question: str) -> dict[str, Any]:
    """在某个会话里问一句：落库两条消息，带上历史交给 ask()。"""
    chat = await get_chat(chat_id)
    if not chat:
        raise KeyError("会话不存在")
    history = await chat_messages(chat_id)
    result = await ask(chat["kb_id"], question, history=history)

    await db.execute(
        "INSERT INTO ima_chat_msgs (chat_id, role, content) VALUES ($1,'user',$2)",
        chat_id,
        question,
    )
    await db.execute(
        """INSERT INTO ima_chat_msgs (chat_id, role, content, sources) VALUES ($1,'assistant',$2,$3::jsonb)""",
        chat_id,
        result["answer"],
        json.dumps(result.get("sources") or [], ensure_ascii=False),
    )
    # 第一句问话当标题，之后不再改 —— 标题一直跟着话题走会让人找不到历史。
    await db.execute(
        """UPDATE ima_chats SET updated_at = now(),
                  title = CASE WHEN title IS NULL OR title = '' OR title = '新对话'
                               THEN $2 ELSE title END
           WHERE id = $1""",
        chat_id,
        _title_of(question),
    )
    return {"chat": await get_chat(chat_id), "answer": result["answer"], "sources": result.get("sources") or []}
