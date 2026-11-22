# 致谢 / Credits

这个站点不是从零长出来的。它站着的东西如下，逐项说明**用来干什么**、**改了没有**。

---

## 直接构建其上的开源项目

| 项目 | 许可 | 在这里的角色 | 是否修改 |
|---|---|---|---|
| [LibreChat](https://github.com/danny-avila/LibreChat) | MIT | 聊天前端与后端 API：会话、消息、用户、智能体 | **不改源码**。用 `deploy/patches/` 覆盖 3 个文件，用 `deploy/web/umf-ui.js` 在运行时裁剪界面 |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | 自研网关 `brain` 的 Web 框架 | — |
| [PostgreSQL](https://www.postgresql.org/) + [pgvector](https://github.com/pgvector/pgvector) | PostgreSQL / PostgreSQL | 记忆、画像、话题、审计的存储与向量检索 | — |
| [MongoDB](https://www.mongodb.com/) | SSPL | LibreChat 的对话与消息存储 | — |
| [Caddy](https://caddyserver.com/) | Apache-2.0 | 反向代理、自动 HTTPS、注册闸门 | 只写配置 |
| [Docker Compose](https://github.com/docker/compose) | Apache-2.0 | 编排 | — |
| [Lucide](https://lucide.dev/) | ISC | 后台与界面的图标（直接内联 SVG 路径，不引运行时） | — |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3 | ASGI 服务器 | — |
| [httpx](https://github.com/encode/httpx) | BSD-3 | brain 调上游模型 / 调 IMA | — |
| [asyncpg](https://github.com/MagicStack/asyncpg) | Apache-2.0 | PostgreSQL 异步驱动 | — |
| [PyMongo](https://github.com/mongodb/mongo-python-driver) | Apache-2.0 | 只读访问 LibreChat 的库（后台追踪用） | — |
| [lobe-chat-agents](https://github.com/lobehub/lobe-chat-agents) | MIT | 智能体市场的 513 个人格（内容，不是代码） | 用 `scripts/import-lobe-agents.py` 导入；头像用 DiceBear 离线生成，不依赖在线服务 |

### 可选组件（默认关闭，开了镜像会大一圈）

| 项目 | 许可 | 用途 |
|---|---|---|
| [LLMLingua-2](https://github.com/microsoft/LLMLingua) | MIT | token 级压缩（要装 torch，约 +1.2 GB 镜像） |
| [Mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | 另一套记忆引擎，可选并行 |
| [LiteLLM](https://github.com/BerriAI/litellm) | MIT | 完整版才用；精简版用 150 行自研路由替掉了它 |
| [LobeHub](https://github.com/lobehub/lobe-chat) | Apache-2.0 | 界面设计参考（未部署） |

### 思路上的借鉴

- **LibreChat 的自定义端点机制**（`headers` 里的 `{{LIBRECHAT_USER_ID}}` 模板）是整个记忆注入能成立的前提。
- **"别名而不是真实模型名"** 这个设计借鉴了 LiteLLM 的 `model_name` 映射，但我们只需要这一个能力，所以自己实现了。
- **`umf-ui.js` 的做法**（不碰 React，只用 CSS 属性选择器 + MutationObserver 改界面）参考了社区里各种「不改源码定制 LibreChat」的讨论；踩了几次 `insertBefore` 崩溃之后才定下这条规矩。
- **智能体市场**的产品形态来自 LobeChat。

---

## 使用的外部服务

| 服务 | 用途 | 是否必需 |
|---|---|---|
| DeepSeek / 通义千问（DashScope） / 智谱 GLM / 腾讯混元 / MiniMax / OpenAI / Google Gemini / xAI Grok / OpenRouter | 模型推理。九家里填了哪几家就启用哪几家 | 至少一家 |
| [腾讯 IMA 知识库](https://ima.qq.com/) | 后台「AI 管理」把站内数据整理成文档推进去，提问时走它的检索 | 可选 |
| [Cloudflare Turnstile](https://www.cloudflare.com/products/turnstile/) | 注册人机验证（无感，不用点红绿灯） | 可选 |
| [Cloudflare R2](https://www.cloudflare.com/products/r2/) | 头像对象存储（零出口费 + 边缘缓存） | 可选，不填就落本地卷 |
| [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) | 不开放 80/443 也能上线（家用宽带场景） | 可选 |
| [Umami](https://umami.is/) | 站点统计，自建、无 cookie、不采集个人身份 | 可选 |
| [DiceBear](https://www.dicebear.com/) | 智能体头像风格参考（离线渲染，不调在线接口） | — |

---

## 写在最后

站点是 **Zvi** 一个人做的，从选型、写 brain、调记忆阈值，到"为什么这个按钮会闪一下"这种像素级问题。

© 2026 Zvi. All rights reserved.
