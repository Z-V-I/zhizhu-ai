# 知猪 · Zhizhu

一个**自己部署、只有邀请码才能进**的 AI 聊天站：多厂商模型可切换，带五层长期记忆与用户画像，站长有一套能看数据、能问 AI 的后台。

不是又一个 ChatGPT 套壳。它解决的是「把 AI 聊天当成长期在用的东西」之后冒出来的四个具体问题：

| 问题 | 这里的做法 |
|---|---|
| 换一家模型就丢上下文 | 模型只是「谁来答」，记忆存在自己的库里，换厂商不影响 |
| 聊了几百轮，模型不记得你是谁 | 五层记忆 + 每轮按预算注入，超预算自动滚动摘要 |
| 数据在别人服务器上 | 全部自托管：对话在 MongoDB，记忆在 PostgreSQL，图片在本地卷 |
| 站长想看「站上在发生什么」 | 后台能查用户/登录/导入，还能把全站数据推进知识库直接问 |

---

## 它长什么样

```
                ┌──────────────┐
   浏览器 ──────▶│    Caddy     │  TLS、静态资源、路由分流
                └──┬────────┬──┘
                   │        │
        ┌──────────▼──┐  ┌──▼─────────────┐
        │ LibreChat   │  │ brain (FastAPI)│
        │ （聊天前端  │  │ 记忆注入 / 路由 │
        │  + 后端 API）│  │ 运营后台 / IMA  │
        └──┬───────┬──┘  └──┬──────────┬──┘
           │       │        │          │
      ┌────▼──┐ ┌──▼─────┐ ┌▼────────┐ │
      │MongoDB│ │ 图片卷 │ │PostgreSQL│ │
      │对话消息│ │ 附件   │ │+pgvector │ │
      └───────┘ └────────┘ └─────────┘ │
                                        │
                          9 家模型 API ─┘（Key 只在服务端）
```

一次对话的实际路径：

1. 前端把请求发给 **brain** 而不是厂商（`baseURL: http://brain:8000/v1`）
2. brain 认出「这句话是谁说的」，从 PostgreSQL 取回相关记忆，压进 system 消息
3. 换成真实模型名 + 对应厂商的 Key，转发上游
4. 边收边转（SSE），顺手把 `delta.content: null` 修成 `""`
5. 回复结束后异步抽记忆、必要时重算画像

LibreChat 全程不知道记忆这回事，所以升级它不会踩到我们的逻辑。

---

## 三个自己写的模块

**`deploy/brain/`（约 5k 行 Python）** —— 整个项目的核心，也是它和「装个 LibreChat」的区别所在。

- `context.py` 记忆注入：五层记忆（身份/偏好/经验/进行中/上下文）双轨检索，
  必带层直出、其余按向量补齐，去重阈值 0.92，否决阈值 0.60
- `memory_build.py` 画像与话题：从对话里抽条目、归纳话题、生成用户画像
- `upstream.py` 模型路由：别名 → 真实模型 + Key + 参数修正 + 兜底链。
  150 行替掉了 LiteLLM 常驻的 350–500 MB
- `importer_mongo.py` 历史导入：ChatGPT / Claude / Gemini / DeepSeek / 豆包五种备份格式
- `ima_sync.py` 腾讯 IMA 知识库：把站内数据整理成文档推进去，提问时走它检索
- `ops.py` 运营后台：用户、登录审计、邀请码、模型配置、AI 问答

**`deploy/web/umf-ui.js`** —— 一个跑在 LibreChat 构建产物之上的「裁剪层」。
用 CSS 属性选择器 + MutationObserver 改界面，不改一行 React 源码，
所以升级 LibreChat 时这份补丁不需要跟着改。

**`deploy/Caddyfile`** —— 反向代理同时承担「注册闸门」：`/api/auth/register`
先转到 brain 校验邀请码和人机验证，通过了才转发给 LibreChat。

---

## 快速开始

> 前提：一台 2C4G 的 Linux（Debian/Ubuntu，装好 Docker 和 Compose v2），一个域名。
> 实测常驻内存 1.3 GB，2C2G 需要调参（见 `docs/11-最低配置与免费部署.md`）。

```bash
git clone https://github.com/Z-V-I/zhizhu-ai.git
cd zhizhu-ai/deploy

# 1. 配置
cp .env.example .env
#   必填：DOMAIN / ACME_EMAIL / POSTGRES_PASSWORD
#   生成密钥：openssl rand -hex 32（CREDS_KEY / JWT_SECRET / JWT_REFRESH_SECRET）
#             openssl rand -hex 16（CREDS_IV）
#   再填 ADMIN_PASSWORD（进 /console 用）
vi .env

# 2. 前端：从官方镜像抽出已编译产物，再打补丁（不需要 npm build）
cd .. && ./scripts/extract-static.sh && python3 scripts/patch-index.py deploy/web/app/index.html
cd deploy

# 3. 起
docker compose -f docker-compose.lite.yml up -d
```

打开 `https://你的域名/console`，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录：

1. **模型切换** → 逐家填 Key、选型号（也可以在 `.env` 里填环境变量）
2. **邀请码** → 生成一个，这才是唯一的注册方式
3. 把邀请码发给朋友，他打开 `/login` 输入即可（首次自动建号）

> ⚠️ `CREDS_KEY` / `CREDS_IV` 上线后**不要再改**。改了等于所有人掉登录，
> 而且数据库里加密的厂商 Key 全部解不开，表现为「所有模型都 401」。

### 上哪儿买、买多大

`docs/05-选型对照与服务器配置.md` 有逐项算账，`docs/11-最低配置与免费部署.md` 有实测数据和免费方案（Oracle Always Free ARM 完全够用）。

---

## 主要特性

- **邀请码即门票**：密码登录不开放，注册口只有邀请码一条路，可加 Cloudflare Turnstile
- **9 家模型随时切**：DeepSeek / 通义千问 / 智谱 GLM / 腾讯混元 / MiniMax / OpenAI / Gemini / Grok / OpenRouter
- **五层长期记忆**：记忆可编辑、可删除；用户的每次更正都会被记录下来，后续生成不会再复活错误信息
- **全局画像 + 话题 + 时间线**：站长和用户看到的是同一份数据
- **历史导入**：ChatGPT、Claude、Gemini、DeepSeek、豆包导出的备份都能灌进来，连同 AI 生成的图和附件
- **运营后台**：用户管理、登录审计、邀请码、模型配置、AI 管理（把全站数据推进 IMA 知识库后直接提问）
- **PWA**：可装到手机主屏
- **省内存**：没有 Redis / Meilisearch / LiteLLM / torch，2C4G 跑得住

---

## 目录

```
deploy/
  brain/              自研后端（FastAPI + PostgreSQL/pgvector）
    app/*.py          记忆、路由、导入、后台、IMA
    models.json       模型别名表（不含密钥）
  librechat/          LibreChat 配置（只做「把 brain 当模型端点」）
  patches/            对 LibreChat 产物的三处小补丁
  web/umf-ui.js       前端裁剪层（运行时改界面）
  web/icons/          站点图标
  Caddyfile           反代 + TLS + 注册闸门
  docker-compose.lite.yml   精简版（推荐）：2C4G
  docker-compose.yml        完整版：多一套 LiteLLM/Redis/Meilisearch
scripts/              抽静态文件、打补丁、生成图标、导入智能体
docs/                 设计与运维笔记（含踩过的坑）
```

---

## 数据存在哪

| 数据 | 位置 | 备份方式 |
|---|---|---|
| 对话、消息、附件元数据 | MongoDB 卷 `mongo_data` | `mongodump` |
| 记忆、画像、话题、登录审计 | PostgreSQL 卷 `pg_data` | `pg_dump` |
| 上传的图片与文件 | 卷 `librechat_images` | tar 打包卷 |
| 厂商 Key | 加密后存在 PostgreSQL，密钥在 `.env` | **导出 `.env`** |

`deploy/RESTORE.md` 有完整的迁移/还原步骤。

---

## 已知限制

- 只有中文界面，没有做 i18n
- 单人/小团体定位：没有租户隔离，不适合对外营业
- 会话搜索走 Mongo 正则，不是全文索引（20 人量级够用）
- IMA 的知识库上传接口只能新增、不能改删（见 `docs/10-AI管理方案.md`）

---

## 致谢与署名

完整的第三方清单见 [CREDITS.md](CREDITS.md)。

Built with **DeepSeek** & **CodeBuddy**。

## License

MIT —— 见 [LICENSE](LICENSE)。
LibreChat 本身也是 MIT，但它编译后的前端产物不随本仓库分发（用 `scripts/extract-static.sh` 自行抽取）。
