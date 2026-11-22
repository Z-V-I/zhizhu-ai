"""集中配置。所有密钥只从环境变量来，代码里不出现任何明文。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(key: str, default: bool = False) -> bool:
    return os.getenv(key, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # 上游。留空表示"直连各厂商"（精简版默认），
    # 填了才走 LiteLLM（完整版）。这个默认值决定了一套代码能跑两种部署。
    litellm_base: str = os.getenv("LITELLM_BASE", "").rstrip("/")
    master_key: str = os.getenv("LITELLM_MASTER_KEY", "")

    # 存储
    database_url: str = os.getenv("DATABASE_URL", "")

    # 鉴权：与 LibreChat 共用 JWT 密钥，这样不需要在两边同步用户表
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    debug: bool = _flag("BRAIN_DEBUG")

    # 管理面板：一个口令，不是用户体系。个人站只有一个人有它。
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    # 加密厂商密钥用；没配就从 JWT_SECRET 派生（同一台机器，风险等价）
    secret_key: str = os.getenv("SECRET_KEY", "")

    # 模型
    cheap_model: str = os.getenv("CHEAP_MODEL", "cheap-extract")
    embed_model: str = os.getenv("EMBED_MODEL", "text-embedding-3-small")

    # 上下文预算（token）。注意：这是省钱的总闸门，不是体验参数
    history_budget: int = int(os.getenv("HISTORY_BUDGET_TOKENS", "8000"))
    keep_recent_turns: int = int(os.getenv("KEEP_RECENT_TURNS", "8"))
    memory_top_k: int = int(os.getenv("MEMORY_TOP_K", "8"))
    memory_min_score: float = float(os.getenv("MEMORY_MIN_SCORE", "0.35"))

    # 压缩
    compress_enabled: bool = _flag("COMPRESS_ENABLED", True)
    compress_ratio: float = float(os.getenv("COMPRESS_RATIO", "0.6"))
    compress_min_tokens: int = int(os.getenv("COMPRESS_MIN_TOKENS", "1500"))
    llmlingua_model: str = os.getenv(
        "LLMLINGUA_MODEL",
        "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
    )

    # 记忆引擎
    mem0_enabled: bool = _flag("MEM0_ENABLED")

    # 头像对象存储。填了 R2_* 就走 Cloudflare R2（零出口费）；
    # 没填就落在容器卷 /app/data/avatars 上，由 brain 自己发 —— 功能一致，只是没有全球缓存。
    r2_account_id: str = os.getenv("R2_ACCOUNT_ID", "")
    # 走 R2 的 REST 对象接口只需要这个 Token（限定到单个桶），
    # 不需要 S3 Access Key —— 那个只能在 CF 后台手工点出来。
    r2_api_token: str = os.getenv("R2_API_TOKEN", "")
    r2_bucket: str = os.getenv("R2_BUCKET", "umf-avatars")
    # 账号落在 EU 区域，对象接口必须带这个头，否则一律 401
    r2_jurisdiction: str = os.getenv("R2_JURISDICTION", "default")
    # R2 公开域名（自定义域或 r2.dev），结尾不要带 /
    r2_public_base: str = os.getenv("R2_PUBLIC_BASE", "").rstrip("/")
    avatar_dir: str = os.getenv("AVATAR_DIR", "/app/data/avatars")

    @property
    def r2_ready(self) -> bool:
        return all([self.r2_account_id, self.r2_api_token, self.r2_public_base])


settings = Settings()
