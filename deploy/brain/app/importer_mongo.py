"""把导入的对话写进 LibreChat 的 Mongo，使它们出现在侧边栏历史里、可以继续聊。

## 为什么必须直写 Mongo

LibreChat 没有"批量导入对话"的接口。它的 conversations / messages 两张表
就是既成事实的存储结构，`POST /api/convos` 是"新建空对话"，
没有批量追加消息的公开入口。要让导入的对话在侧边栏可见、点进去能接着聊，
只能按它的 schema 写入。

## 与「后台只读」原则的关系

`mg.py` 的注释写明"后台是只读旁观者，不写库"。这里是有意的例外，
并且把风险收在三个点上：

  1. **只写 conversations / messages 两个集合**，绝不碰 users / agents / configs
  2. **每条写入都带 umfImport 标记**，可以一键回滚（见 purge_imported）
  3. **只写自己 user 名下的记录**，永远按 user 过滤

## 消息链

LibreChat 靠 parentMessageId 串成一棵树。导入时按顺序首尾相接，
第一条的父指向全零 UUID（`ROOT`）—— 和它自己新建对话时的写法一致。
链断掉的话，点进对话会显示空白或报错，所以这条必须严格。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import chat_images, mg

log = logging.getLogger("brain.importer_mongo")

ROOT_PARENT = "00000000-0000-0000-0000-000000000000"

# 导入的对话统一挂在这个端点上（我们自己代理的 Brain），
# 这样"继续聊"能走同一条链路，模型选择、计费、日志都一致。
IMPORT_ENDPOINT = "Brain"
IMPORT_MODEL = "deepseek"


def _user_oid(user_id: str):
    """conversations.user / messages.user 存的是 ObjectId 字符串。
    有的版本是字符串，有的被强转成 ObjectId —— 先按字符串写，
    Mongo 会自己匹配（LibreChat 查询时也做同样处理）。"""
    return user_id


def _clean_text(t: Any) -> str:
    return str(t or "").strip()


def _signature(title: str, created: Any, msg_count: int) -> str:
    """对话的内容签名，用于幂等判断。

    为什么不能只靠 importSourceId：
      · 用户自己聊出来的对话根本没有这个字段
      · 从 DeepSeek 导入的对话，它的值原本是 DeepSeek 的会话 id，
        而"导出再回灌"时带的是 umf1- 内容哈希 —— 两者对不上

    ★ 为什么 msg_count 必须用「非空消息数」：
      第一版用 len(conversations.messages) 当条数，结果回灌时去重全失效 ——
      导出会过滤掉 text 为空的消息（416 → 370），两边口径不一致，
      签名永远对不上，于是又写了一份（实测多出 33 个重复对话）。
      所以调用方必须传**非空条数**，见 write_conversations 里的统计。
    """
    dt = _parse_dt(created)
    # ★ 没有时间戳时用固定哨兵 "none"，绝不能用"当前时间"——
    #   否则豆包这种无时间戳的来源，每次导入算出的签名都不同，去重永远失效
    #   （实测：重复导入豆包多出了一份）。
    stamp = dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else "none"
    return f"{(title or '').strip()}|{stamp}|{msg_count}"


def write_conversations(
    user_id: str,
    convs: list[dict[str, Any]],
    *,
    platform: str,
    batch: str,
    source: str = "",
    resync: bool = False,
) -> dict[str, Any]:
    """把会话列表写进 Mongo。返回统计。

    resync=True：对**已经导入过**的对话重新写一遍（保留原 conversationId）。
    为什么需要它：解析规则升级后（比如原来只搬图片、现在连 txt 一起搬），
    老数据必须重放一遍才能补上。删掉重建会让 conversationId 全变，
    而记忆是以 `conv:<conversationId>` 为批次号存的 —— id 一变，
    已有记忆就指向了不存在的对话，下次整理还会重复抽取。
    所以这里复用 id：删消息、重写，对话本身留着。

    convs 是 importers 的中间结构：
        {"title", "messages":[{"role","text"}], "created", "updated", "source_id"}
    """
    if not mg.ready():
        raise RuntimeError("Mongo 未连接，无法写入历史对话")

    conversations = mg.col("conversations")
    messages = mg.col("messages")
    # 附件要同时登记进 files 集合，否则"附加文件"面板永远是空的（面板只读这张表）
    files_col = mg.col("files")

    now = datetime.now(timezone.utc)
    written_convs = 0
    written_msgs = 0
    written_files = 0
    resynced = 0
    skipped: list[str] = []

    # 预加载该用户已有对话的签名。
    #
    # 关键：必须先统计每个对话的「非空消息数」，不能用 messages 数组长度。
    # 数组里含空消息（AI 出错时会产生空回复），而导入/导出两边都只认非空文本，
    # 口径不一致就会导致去重失效。
    #
    # 两步查询（对话 + 按对话聚合非空数），只读、有上限，不会拖死。
    rows = list(
        conversations.find(
            {"user": _user_oid(user_id)},
            {"conversationId": 1, "title": 1, "createdAt": 1, "umfSig": 1},
        )
    )
    conv_ids = [r.get("conversationId") for r in rows if r.get("conversationId")]
    nonempty: dict[str, int] = {}
    if conv_ids:
        agg = mg.col("messages").aggregate(
            [
                {"$match": {"conversationId": {"$in": conv_ids}}},
                {
                    "$group": {
                        "_id": "$conversationId",
                        "n": {
                            # 口径必须和导出/写入完全一致：
                            #   算「text 非空」或「content 里存在 type=text 且非空的 part」。
                            # 不能用 "$size(content) > 0" —— 错误消息的 content 里
                            # 只有一个 error part，没有正文，导出时会跳过它。
                            # 两边算法不一致，签名就对不上，去重再次失效。
                            "$sum": {
                                "$cond": [
                                    {
                                        "$or": [
                                            {
                                                "$gt": [
                                                    {
                                                        "$strLenCP": {
                                                            "$trim": {
                                                                "input": {"$ifNull": ["$text", ""]}
                                                            }
                                                        }
                                                    },
                                                    0,
                                                ]
                                            },
                                            {
                                                "$gt": [
                                                    {
                                                        "$size": {
                                                            "$filter": {
                                                                "input": {"$ifNull": ["$content", []]},
                                                                "cond": {
                                                                    "$and": [
                                                                        {"$eq": ["$$this.type", "text"]},
                                                                        {
                                                                            "$gt": [
                                                                                {
                                                                                    "$strLenCP": {
                                                                                        "$trim": {
                                                                                            "input": {
                                                                                                "$ifNull": ["$$this.text", ""]
                                                                                            }
                                                                                        }
                                                                                    }
                                                                                },
                                                                                0,
                                                                            ]
                                                                        },
                                                                    ]
                                                                },
                                                            }
                                                        }
                                                    },
                                                    0,
                                                ]
                                            },
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
            ]
        )
        for g in agg:
            if g.get("_id"):
                nonempty[g["_id"]] = int(g.get("n") or 0)

    existing_sigs: set[str] = set()
    # 本次导入要写进 files 集合的记录，等对话写成功再一次性入库 ——
    # 对话被去重跳过时，附件也不该留在库里当孤儿。
    files_records: list[dict[str, Any]] = []
    for row in rows:
        cid = row.get("conversationId")
        # 优先用写入时存下的签名（最可靠：它就是当初入参算出来的）。
        # 老数据没有这个字段，才用库里的 title/createdAt/非空条数现算。
        stored = row.get("umfSig")
        if stored:
            existing_sigs.add(stored)
        else:
            existing_sigs.add(
                _signature(
                    row.get("title") or "",
                    row.get("createdAt"),
                    nonempty.get(cid, 0),
                )
            )

    for conv in convs:
        # 只带附件、没有正文的消息也要留下。
        # Gemini 里"只生成了一张图、没写一个字"的回答很常见，
        # 按"必须有文本"过滤会把它们整条丢掉 —— 图就跟着没了。
        msgs = [
            m for m in conv.get("messages") or []
            if _clean_text(m.get("text")) or (m.get("attachments") or [])
        ]
        if not msgs:
            skipped.append(conv.get("title") or "?")
            continue

        src_id = conv.get("source_id") or ""

        # 幂等（两道）：
        #   ① 来源 id 相同 → 同一次来源重复导入
        #   ② 内容签名相同 → 跨来源的重复（比如导出再回灌）
        reuse_id: str | None = None
        reuse_oid = None
        if src_id:
            exists = conversations.find_one(
                {"user": _user_oid(user_id), "importSourceId": src_id},
                {"_id": 1, "conversationId": 1},
            )
            if exists:
                if not resync:
                    skipped.append((conv.get("title") or "?") + "（已导入）")
                    continue
                # 重同步：沿用原 id，把旧消息与旧文件记录清掉后重写
                reuse_oid = exists.get("_id")
                reuse_id = exists.get("conversationId")
                if reuse_id:
                    messages.delete_many({"conversationId": reuse_id})
                    files_col.delete_many({"conversationId": reuse_id})
                    resynced += 1

        sig = _signature(conv.get("title") or "", conv.get("created"), len(msgs))
        if not reuse_id and sig in existing_sigs:
            skipped.append((conv.get("title") or "?") + "（已存在）")
            continue

        # id 必须在幂等判断之后才能定：resync 时要沿用老 id
        conv_id = reuse_id or str(uuid.uuid4())

        # 时间：尽量保留原对话的时间，侧边栏排序才对
        created = _parse_dt(conv.get("created")) or now
        updated = _parse_dt(conv.get("updated")) or created

        msg_oids = []
        # 这个对话的附件记录先收集在局部，对话真写成了才并入 files_records
        conv_files: list[dict[str, Any]] = []
        parent = ROOT_PARENT
        for m in msgs:
            mid = str(uuid.uuid4())
            is_user = m.get("role") == "user"
            text = _clean_text(m["text"])

            # 附件：把图片写进共享卷，并在消息上留 files 记录。
            # 只写能当图片显示的格式（见 chat_images.IMAGE_MIME），
            # 其它类型硬塞进来只会变成打不开的附件。
            #
            # 用 save_any 而不是 save：ChatGPT 的图叫 `file_xxx`（没有扩展名）、
            # 包里是 `.dat`，按名字判断类型一律会被跳过 —— 图片就这么静默丢了。
            # 按文件头识别才可靠。
            files_meta: list[dict[str, Any]] = []
            for att in m.get("attachments") or []:
                got = chat_images.save_any(user_id, att.get("name") or "file", att.get("data") or b"")
                if got:
                    files_meta.append(got)
                    conv_files.append(
                        chat_images.file_record(
                            user_id, got, conversation_id=conv_id, message_id=mid
                        )
                    )

            # ★ 助手侧的附件必须写进正文。
            #
            # LibreChat 的 MessageRow 有这行：
            #     const hasFiles = isUserSide && ...
            # 附件区**只对用户侧消息渲染**。助手消息上的 files 会被整块忽略，
            # 于是"AI 画的图""AI 生成的文件"在导入后全都看不见 ——
            # 用户看到的就是一堆只有文字的回答。
            #
            # 而正文的 Markdown 走另一个渲染器（MarkdownComponents），对任何角色都生效，
            # 还能被搜索、复制、导出。所以助手侧：图片用 ![]()，其它文件用 [](链接)。
            if not is_user and files_meta:
                parts = []
                for f in files_meta:
                    fp = f["filepath"]
                    nm = f["filename"]
                    if str(f.get("type", "")).startswith("image/"):
                        parts.append(f"![{nm}]({fp})")
                    else:
                        # 非图片：链到 LibreChat 自己的下载接口。
                        # 不能链 /images/<file>：那条静态路由只服务图片，
                        # .txt 会落到前端 index.html 上（拿到的是一页 HTML，不是文件）。
                        dl = f"/api/files/download/{user_id}/{f['file_id']}"
                        parts.append(f"📎 [{nm}]({dl})")
                block = "\n\n".join(parts)
                text = (text + "\n\n" + block).strip() if text else block

            doc = {
                "messageId": mid,
                "conversationId": conv_id,
                "user": _user_oid(user_id),
                "__v": 0,
                "_meiliIndex": False,
                "createdAt": created,
                "updatedAt": created,
                "endpoint": IMPORT_ENDPOINT,
                "error": False,
                "expiredAt": None,
                "isCreatedByUser": is_user,
                "isTemporary": False,
                "model": None if is_user else IMPORT_MODEL,
                "parentMessageId": parent,
                "sender": "User" if is_user else "DeepSeek",
                "text": text,
                # 中文没有空格，len//2 是个够用的粗略估计，只用于 UI 显示
                "tokenCount": max(1, len(text) // 2),
                "unfinished": False,
                # ---- 标记（回滚依据）----
                "umfImport": True,
                "umfImportBatch": batch,
                "umfImportPlatform": platform,
            }
            if files_meta:
                # LibreChat 前端读 message.files 渲染附件缩略图
                doc["files"] = files_meta
                # 老版本镜像还会看 attachments，两个都写上更保险
                doc["attachments"] = files_meta
            try:
                res = messages.insert_one(doc)
                msg_oids.append(res.inserted_id)
                written_msgs += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("insert message failed (conv=%s): %s", conv_id, exc)
                break
            parent = mid

        if not msg_oids:
            continue

        # 对话级的 files 字段是 file_id 列表，侧边栏/文件面板靠它统计
        conv_file_ids = [
            f["file_id"]
            for docs in messages.find({"conversationId": conv_id}, {"files": 1})
            for f in (docs.get("files") or [])
            if f.get("file_id")
        ]

        conv_doc = {
            "conversationId": conv_id,
            "user": _user_oid(user_id),
            "title": (conv.get("title") or "导入的对话")[:120],
            "endpoint": IMPORT_ENDPOINT,
            "endpointType": "custom",
            "model": IMPORT_MODEL,
            "spec": IMPORT_MODEL,
            "messages": msg_oids,
            "createdAt": created,
            "updatedAt": updated,
            "isArchived": False,
            "isTemporary": False,
            "tags": ["导入"],
            "files": conv_file_ids,
            "greeting": "",
            "pinned": False,
            "resendFiles": True,
            "expiredAt": None,
            "__v": 0,
            # ---- 标记 ----
            "umfImport": True,
            "umfImportBatch": batch,
            "umfImportPlatform": platform,
            "importSourceId": src_id or None,
            "importSourceName": source or None,
            # 存下签名，后续导入直接比对，不必再从内容反推
            "umfSig": sig,
        }
        try:
            if reuse_oid is not None:
                # 重同步：对话文档已存在（_id 相同），必须更新而不是插入。
                # 用 insert_one 会撞主键 → 抛异常 → 下面的清理把刚写的消息删光。
                # 这一步踩过一次，Gemini 354 个对话的消息全被清空过。
                conversations.update_one({"_id": reuse_oid}, {"$set": conv_doc})
            else:
                conversations.insert_one(conv_doc)
                existing_sigs.add(sig)   # 同批次内的重复也要拦住
            written_convs += 1
            if conv_files:
                files_records.extend(conv_files)
        except Exception as exc:  # noqa: BLE001
            log.warning("write conversation failed (conv=%s, resync=%s): %s", conv_id, bool(reuse_oid), exc)
            # 只有"新建"失败才清孤儿消息。重同步失败时绝不能删 ——
            # 那条对话在库里是好的，删了就是把人家历史清了。
            if reuse_oid is None:
                messages.delete_many({"conversationId": conv_id})

    # 附件元信息入 files 集合：侧边栏"文件"面板只读这张表，
    # 不写进去的话，附件虽然在对话里显示，面板里永远是空的。
    if files_records:
        try:
            files_col.insert_many(files_records, ordered=False)
            written_files = len(files_records)
        except Exception as exc:  # noqa: BLE001
            # 部分重复（同 file_id）会整批失败，退回逐条写
            log.warning("bulk insert files failed, fallback one-by-one: %s", exc)
            for r in files_records:
                try:
                    files_col.insert_one(r)
                    written_files += 1
                except Exception:  # noqa: BLE001
                    pass

    return {
        "conversations": written_convs,
        "messages": written_msgs,
        "files": written_files,
        "resynced": resynced,
        "skipped": skipped[:20],
        "skippedCount": len(skipped),
    }


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def list_imported(user_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """列出已导入的对话，供页面展示/回滚。"""
    if not mg.ready():
        return []
    cur = mg.col("conversations").find(
        {"user": _user_oid(user_id), "umfImport": True},
        {
            "_id": 0,
            "conversationId": 1,
            "title": 1,
            "umfImportPlatform": 1,
            "umfImportBatch": 1,
            "createdAt": 1,
            "updatedAt": 1,
            "messages": 1,
        },
    ).sort("createdAt", -1).limit(limit)
    out = []
    for c in cur:
        out.append(
            {
                "conversationId": c.get("conversationId"),
                "title": c.get("title"),
                "platform": c.get("umfImportPlatform"),
                "batch": c.get("umfImportBatch"),
                "messageCount": len(c.get("messages") or []),
                "createdAt": c.get("createdAt").isoformat() if c.get("createdAt") else None,
            }
        )
    return out


def purge_imported(
    user_id: str,
    batch: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> dict[str, int]:
    """回滚：删掉导入的对话及其消息。

    只删带 umfImport 标记、且属于该用户的记录 —— 用户自己聊出来的对话
    永远不受影响。可以按批次、也可以按单个 conversationId 精准撤销。
    """
    if not mg.ready():
        raise RuntimeError("Mongo 未连接")
    q: dict[str, Any] = {"user": _user_oid(user_id), "umfImport": True}
    if batch:
        q["umfImportBatch"] = batch
    if conversation_id:
        # 双保险：即便传了个不属于导入的 id，umfImport 过滤也会拦住
        q["conversationId"] = conversation_id

    convs = list(mg.col("conversations").find(q, {"conversationId": 1}))
    ids = [c["conversationId"] for c in convs if c.get("conversationId")]
    if not ids:
        return {"conversations": 0, "messages": 0}

    # 先按 conversation 收集图片，删记录前把文件也清掉，别在卷里留孤儿
    imgs: list[dict[str, Any]] = []
    for d in mg.col("messages").find({"conversationId": {"$in": ids}}, {"files": 1}):
        imgs.extend(d.get("files") or [])
    removed = chat_images.delete_for_conversations(user_id, imgs)

    mres = mg.col("messages").delete_many({"conversationId": {"$in": ids}})
    cres = mg.col("conversations").delete_many({"conversationId": {"$in": ids}})
    return {
        "conversations": cres.deleted_count,
        "messages": mres.deleted_count,
        "images": removed,
    }
