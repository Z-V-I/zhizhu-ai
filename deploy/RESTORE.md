# 知猪（UMF）完整备份 · 还原说明

> 这是**含敏感信息**的私有备份：`.env` 里有全部厂商 Key、JWT 密钥、后台口令、IMA 凭证。
> 只放自己手上，**不要**推到任何公开仓库。
> 打包时间：2026-11-22

---

## 包里有什么

| 路径 | 内容 |
|---|---|
| `deploy/` | 全部部署文件（compose、Caddyfile、librechat.yaml、patches、brain 源码、`web/` 已编译前端） |
| `deploy/.env` | **全部密钥**。没有它，光有代码也跑不起来 |
| `deploy/brain/models.json` | 模型别名表 |
| `scripts/` | 抽取静态文件、生成图标、抓 vendor 的脚本 |
| `docs/` | 设计与运维文档 |
| `dumps/mongo-LibreChat.archive.gz` | Mongo 全库（2026-11-22 快照，含测试数据） |
| `dumps/pg-umf.sql.gz` | Postgres 全库（记忆/话题/画像/登录/导入记录） |

**不含**：`vendor/`（448 MB，是上游源码参考，用 `scripts/fetch-vendor.sh` 可再生）、
镜像本身（从 registry 拉）。

---

## 从零还原（新机器，约 15 分钟）

前提：装好 Docker + Compose v2，域名 A 记录指向新机器，80/443 放行。

```bash
# 1. 解包
mkdir -p /opt/umf && tar -xzf umf-full-*.tar.gz -C /opt/umf --strip-components=1
cd /opt/umf/deploy

# 2. 起容器（brain 会现场构建，2C 机器约 3-5 分钟）
docker compose -f docker-compose.lite.yml up -d

# 3. 等证书签发 + 自检
sleep 20 && docker compose -f docker-compose.lite.yml ps
curl -sI https://$DOMAIN | head -1
```

到这一步站点已经能开，但**数据库是空的**。要恢复数据继续：

```bash
# 4. 恢复 Mongo
docker exec -i umf-chat-lite-mongodb-1 \
  mongorestore --drop --archive --gzip < /opt/umf/dumps/mongo-LibreChat.archive.gz

# 5. 恢复 Postgres
docker exec -i umf-chat-lite-postgres-1 \
  psql -U umf -d umf < <(gunzip -c /opt/umf/dumps/pg-umf.sql.gz)

# 6. 重启让两边重新建连接
docker compose -f docker-compose.lite.yml restart api brain
```

> **想从"干净站"开始**（推荐，因为备份里全是测试数据）：
> 跳过第 4-6 步，直接跑 §「清空数据」那段 SQL。

---

## 图片与会话附件

聊天里上传的图片存在 Docker 卷 `umf-chat-lite_librechat_images`（约 170 MB），
**不在 tar 包里**（卷没法直接打包成文件）。要一并搬走：

```bash
# 旧机器导出
docker run --rm -v umf-chat-lite_librechat_images:/v -v /root:/out alpine \
  tar -czf /out/images.tar.gz -C /v .

# 新机器导入（先让 compose 建好卷）
docker run --rm -v umf-chat-lite_librechat_images:/v -v /root:/in alpine \
  sh -c 'cd /v && tar -xzf /in/images.tar.gz'
```

不做这步也能跑，只是历史对话里的图片会 404。

---

## 清空数据（保留表结构与后台账号）

```bash
# Mongo：删用户、会话、消息、附件、会话标签，保留角色/权限/配置
docker exec umf-chat-lite-mongodb-1 mongosh LibreChat --quiet --eval '
  ["users","conversations","messages","files","sessions","conversationtags",
   "sharedlinks","promptgroups","toolfavorites","balances","transactions",
   "memoryentries","agents","aclentries","agentcategories"].forEach(function(c){
    var n = db.getCollection(c).deleteMany({}).deletedCount;
    print(c + " -" + n);
  });'

# Postgres：清业务数据，保留 providers（模型配置）与后台账号
docker exec umf-chat-lite-postgres-1 psql -U umf -d umf -c '
  TRUNCATE memories, memory_events, memory_corrections, memory_jobs,
           topics, conv_digests, conversation_summaries, conversation_state,
           login_events, import_batches, import_items, audit_log,
           umf_profiles, umf_prefs, umf_avatars, user_reviews,
           ima_sync_state, ima_chats, ima_chat_msgs
  RESTART IDENTITY CASCADE;'
```

---

## 容易踩的坑

1. **`.env` 里的 `CREDS_KEY` / `CREDS_IV` 绝不能换**。换了之后数据库里加密的厂商 Key 全部解不开，
   表现是"所有模型 401"，看起来像 Key 过期。
2. `ALLOW_REGISTRATION` 现在是 `true`，但注册口由 Caddy 转到 brain 校验邀请码 + Turnstile，
   直接在 LibreChat 侧改这个开关不会绕开邀请码。
3. 换域名要同步改三处：`.env` 的 `DOMAIN`、`librechat.yaml` 里的 `baseURL`（容器内部地址，一般不用改）、
   Cloudflare Turnstile 的域名白名单。
4. `deploy/web/` 是**已编译**的前端产物（42 MB）。升级 LibreChat 镜像后必须重跑
   `scripts/extract-static.sh`，否则新前端配旧后端会白屏。
