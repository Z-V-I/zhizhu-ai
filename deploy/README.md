# deploy · 跑起来

## 先选一条路线

| | 完整版 `docker-compose.yml` | **精简版 `docker-compose.lite.yml`** |
|---|---|---|
| 适用 | 4C8G 以上，想开 LLMLingua / Mem0 / Meilisearch | **2C4G（阿里云新加坡 ¥50–100 档）** |
| 组件 | caddy + api + client + brain + litellm + mongo + pg + redis + meili | caddy + api + brain + mongo + pg（5 个） |
| 常驻内存 | 2.2–2.7 GB | **实测 1.3 GB（峰值）** |
| 模型路由 | LiteLLM（有后台、额度、日志） | brain 内置 `upstream.py`（别名 + 兜底，无后台） |
| 前端 | 可 fork 自建 | **官方镜像产物抽出，caddy 直接托管**（`extract-static.sh`，零构建、零额外容器） |

两条路线共用同一份 `librechat.yaml` / `.env` / 数据库 schema，**随时可切**。
精简版的完整步骤（含阿里云安全组、PWA 零构建、故障排查）在 `docs/06-低成本部署.md`。

下面的命令以**完整版**为例；精简版把 `docker compose` 换成 `docker compose -f docker-compose.lite.yml` 即可。

```bash
cp .env.example .env
openssl rand -hex 32        # 跑三次，填进 CREDS_KEY / CREDS_IV / JWT_SECRET / JWT_REFRESH_SECRET
vi .env                     # 填 8 家 API Key（只填你有的）

docker compose build brain
docker compose up -d
docker compose logs -f brain litellm
```

第一个账号：LibreChat **没有** `create-user` 命令，只能临时开注册再关掉：

```bash
sed -i 's/^ALLOW_REGISTRATION=.*/ALLOW_REGISTRATION=true/' .env
docker compose -f docker-compose.lite.yml up -d api     # 去站点注册
sed -i 's/^ALLOW_REGISTRATION=.*/ALLOW_REGISTRATION=false/' .env
docker compose -f docker-compose.lite.yml up -d api
```

## 自检

```bash
# 1. 网关活着
curl -s https://$DOMAIN/api/umf/healthz

# 2. 模型列表通了（12 个别名）
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" | head -c 300

# 3. 模型能出字（直连 litellm，绕开前端）
curl -s localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"ds-chat","messages":[{"role":"user","content":"说一句你好"}],"stream":false}'
```

浏览器打开 → 选 `Brain` → `ds-chat` → 发一句话 → **逐字流式输出**即接通。
打开 DevTools → Network → 看聊天请求的响应头：`x-umf-recall: 3` 说明记忆注入了。

## 记忆链路验证（不装 Mem0 也能验）

```bash
# 写一条记忆（token 用浏览器 Application → Cookies/localStorage 里 LibreChat 的 accessToken）
TOKEN=...
curl -s -X POST https://$DOMAIN/api/umf/memories \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"用户在做个人博客，用 Next.js","kind":"fact"}'

# 看看这句话会召回什么
curl -s -X POST https://$DOMAIN/api/umf/recall \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"我的博客用什么框架"}'
```

第二条能返回上面那条记忆，说明"检索 → 注入"整条链是通的。

## 三个开关（都在 .env 里，不用改代码）

| 变量 | 作用 | 建议 |
|---|---|---|
| `COMPRESS_ENABLED=1` | 三层压缩总开关（结构裁剪 / 滚动摘要 / LLMLingua） | 保持开 |
| `WITH_COMPRESS=1` | 同时装 LLMLingua-2（+torch，镜像约 1.2GB） | 有"粘大段文档"的需求再开 |
| `WITH_MEM0=1` + `MEM0_ENABLED=1` | 换 Mem0 做抽取与检索 | 先用自带，跑顺了再换 |

不装 LLMLingua 时压缩会自动降级成"保留头尾"，**不会阻塞请求**；Mem0 初始化失败也会退回自带检索。

## 结构

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 全栈编排 |
| `docker-compose.tunnel.yml` | 可选：CF Tunnel 上线，不开放端口 |
| `litellm/config.yaml` | 12 个模型别名、fallback、`cheap-extract` |
| `librechat/librechat.yaml` | Brain 端点 + 4 个预设 + 三行用户态头（占位符，不改核心代码） |
| `brain/app/` | 网关、记忆、压缩、抽取 —— 自研的全部就这 900 行 |

## 常见故障

| 现象 | 原因 |
|---|---|
| 回复一次性吐出、没有流式 | 反代缓冲。确认 Caddy 的 `flush_interval -1` 生效 |
| `401` 但 Key 没问题 | LibreChat 里填的是 `BRAIN_API_KEY`，不是厂商 Key |
| 某家模型 404 | model id 改名了，改 `litellm/config.yaml` 那行，重启 litellm |
| 日志刷 "no X-UMF-User" | `librechat.yaml` 的 `headers` 没生效，确认 LibreChat 版本支持 `{{LIBRECHAT_USER_ID}}` |
| 记忆不召回 | 先看 `/api/umf/memories` 有没有数据；再看 embedding 模型在 litellm 里是否可用 |
