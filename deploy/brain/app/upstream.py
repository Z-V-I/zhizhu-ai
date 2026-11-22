"""上游路由：把「别名」翻译成「真实模型 + 密钥 + 参数」。

为什么自己写这 150 行，而不是继续用 LiteLLM：
  LiteLLM Proxy 是个常驻 Python 进程，空载就要 350-500MB。它换来的是
  「多租户计费、额度、日志、管理后台」——个人自用一样都用不上。
  我们要的只有四件事：别名映射、密钥注入、参数修正、失败兜底。
  这四件事在一个已经有 FastAPI 的进程里做，是零额外内存。

配置在 models.json，密钥永远只从环境变量读（`key_env` 指向变量名），
所以这个文件可以进 git，不会泄漏任何东西。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("brain.upstream")

# 这些参数只对 OpenAI 官方的部分模型有意义，国内几家收到会 400
# stream_options 不在剔除列表里：它是 inclusion of usage 的唯一来源，
# LibreChat 的 agents 运行时拿不到 usage 就无法收尾（报 tool_calls in undefined）。
_OPENAI_ONLY = ("logit_bias", "seed")


@dataclass(frozen=True)
class Target:
    alias: str
    base: str
    model: str
    key: str
    provider: str = ""
    drop: frozenset = field(default_factory=frozenset)
    max_tokens: Optional[int] = None
    dimensions: Optional[int] = None
    label: str = ""
    # 固定附加到请求体里的字段（比如 reasoning: {enabled: false}）。
    # 只补客户端没传的键，绝不覆盖用户/前端已经指定的值。
    extra: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{self.base.rstrip('/')}/chat/completions"

    @property
    def embed_url(self) -> str:
        return f"{self.base.rstrip('/')}/embeddings"

    def headers(self, incoming: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if incoming:
            for k, v in incoming.items():
                if k.lower().startswith("x-") and k.lower() not in ("x-umf-user", "x-umf-agent", "x-umf-conv"):
                    h[k] = v
        return h


class Router:
    def __init__(self, path: Optional[str] = None) -> None:
        self._targets: dict[str, Target] = {}
        self._fallback: dict[str, list[str]] = {}
        self._defaults: dict[str, str] = {}
        self._embed: Optional[str] = None
        self._cheap: Optional[str] = None
        self._load(path or os.getenv("UMF_MODELS_FILE", "/app/models.json"))

    def _load(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            log.warning("models file not found: %s (路由将不可用，只能直连)", path)
            return
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("models file unreadable: %s", exc)
            return

        providers = cfg.get("providers") or {}
        for name, item in (cfg.get("aliases") or {}).items():
            prov = providers.get(item.get("provider") or "", {})
            base = item.get("base") or prov.get("base") or ""
            key_env = item.get("key_env") or prov.get("key_env") or ""
            key = os.getenv(key_env, "") if key_env else ""
            if not base:
                log.warning("alias %s skipped: no base url", name)
                continue
            if not key:
                # 缺密钥不算错：你只买了 8 家里的 5 家也很正常，
                # 这个别名就是不可用，兜底链会跳过它。
                log.info("alias %s disabled: %s not set", name, key_env)
                continue
            self._targets[name] = Target(
                alias=name,
                base=base,
                model=item.get("model") or name,
                key=key,
                provider=item.get("provider") or prov.get("provider") or "",
                drop=frozenset(item.get("drop") or prov.get("drop") or ()),
                max_tokens=item.get("max_tokens"),
                label=item.get("label") or name,
            )

        self._fallback = {k: list(v) for k, v in (cfg.get("fallback") or {}).items()}
        self._defaults = dict(cfg.get("default") or {})
        emb = cfg.get("embed") or {}
        self._embed = emb.get("alias") if isinstance(emb, dict) else emb
        cheap = cfg.get("cheap") or {}
        self._cheap = cheap.get("alias") if isinstance(cheap, dict) else cheap
        log.info("router ready: %d aliases %s", len(self._targets), sorted(self._targets))

    # ---------------- 查询 ----------------

    def get(self, name: str) -> Optional[Target]:
        return self._targets.get(name)

    def chain(self, name: str) -> list[Target]:
        """主别名 + 兜底链。缺密钥/未定义的会被自动跳过。"""
        out: list[Target] = []
        for n in [name, *self._fallback.get(name, [])]:
            t = self._targets.get(n)
            if t and t not in out:
                out.append(t)
        return out

    def default(self, kind: str) -> Optional[Target]:
        name = self._defaults.get(kind) or self._cheap or next(iter(self._targets), None)
        return self._targets.get(name) if name else None

    @property
    def cheap(self) -> Optional[Target]:
        return self._targets.get(self._cheap) if self._cheap else self.default("cheap")

    @property
    def embed(self) -> Optional[Target]:
        return self._targets.get(self._embed) if self._embed else None

    def names(self) -> list[str]:
        return sorted(self._targets)

    def catalog(self) -> dict[str, Any]:
        """OpenAI 兼容的 /v1/models 响应，只暴露别名，不暴露真实模型和上游。"""
        return {
            "object": "list",
            "data": [
                {"id": n, "object": "model", "owned_by": "umf", "created": 0}
                for n in self.names()
            ],
        }

    # ---------------- 请求改写 ----------------

    @staticmethod
    def prepare(body: dict[str, Any], target: Target) -> dict[str, Any]:
        """把一个发往 Brain 的 body，改写成发往上游的 body。

        只做必要的修正，不重写语义：
          · model 换成上游真实名
          · 剔除上游不认识的参数（否则国内几家直接 400）
          · max_tokens 不超过上游上限（LibreChat 默认会填很大一个数）
        """
        out = dict(body)
        out["model"] = target.model

        for k in target.drop:
            out.pop(k, None)
        if target.provider and target.provider not in ("openai", "azure", "openrouter"):
            for k in _OPENAI_ONLY:
                out.pop(k, None)

        if target.max_tokens:
            cur = out.get("max_tokens") or out.get("max_completion_tokens")
            out["max_tokens"] = min(int(cur), target.max_tokens) if cur else target.max_tokens
            out.pop("max_completion_tokens", None)

        for k, v in (target.extra or {}).items():
            out.setdefault(k, v)

        return out


router = Router()
