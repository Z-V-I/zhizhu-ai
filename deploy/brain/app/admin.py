"""管理员面板：决定「每个厂商实际用哪个模型」。

为什么单独一个口令，而不是复用用户 JWT：
  用户是朋友，管理员是你。这两件事不该共用一个权限体系。
  个人站只有一个人知道 ADMIN_TOKEN，写进 .env 就行，不需要角色表。

面板本身是一个静态 HTML（app/admin.html），不带构建、不带前端框架。
一个人用的后台，78KB 的 React 是负债不是资产。
"""

from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import providers
from .config import settings

log = logging.getLogger("brain.admin")

router = APIRouter()
HTML = Path(__file__).with_name("admin.html")


def _check(token: Optional[str]) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "没有配置 ADMIN_TOKEN，管理面板已禁用")
    if not token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(401, "口令不对")


# 旧的 /admin 页面已并入 /console（见 ops.py 里的 legacy_admin 重定向）。
# 这里只保留 providers 的 API —— 后台的「模型切换」就是调这几个接口。


@router.get("/api/admin/providers")
async def list_providers(x_admin_token: Optional[str] = Header(default=None)):
    _check(x_admin_token)
    rows = await providers.all_rows(force=True)
    out = []
    for r in rows:
        models = await providers.cached_models(r["code"])
        out.append(
            {
                "code": r["code"],
                "label": r["label"],
                "kind": r["kind"],
                "base_url": r["base_url"],
                "list_url": r["list_url"],
                "enabled": r["enabled"],
                "model_id": r["model_id"],
                "has_key": r["has_key"],
                "last_fetch_at": r["last_fetch_at"].isoformat() if r["last_fetch_at"] else None,
                "last_fetch_error": r["last_fetch_error"],
                "models": models,
                "ready": bool(r["enabled"] and r["model_id"] and r["has_key"]),
            }
        )
    return {"providers": out}


@router.post("/api/admin/providers/{code}/key")
async def set_key(
    code: str,
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
):
    _check(x_admin_token)
    body: dict[str, Any] = await request.json()
    key = str(body.get("key") or "").strip()
    if not await providers.row(code):
        raise HTTPException(404, "未知厂商")
    if key:
        await providers.save_key(code, key)
    log.info("provider %s: key updated (%s)", code, "set" if key else "cleared")
    return {"ok": True, "masked": providers.mask(key)}


@router.post("/api/admin/providers/{code}/fetch")
async def fetch(code: str, x_admin_token: Optional[str] = Header(default=None)):
    _check(x_admin_token)
    if not await providers.row(code):
        raise HTTPException(404, "未知厂商")
    ids, err = await providers.fetch_models(code)
    return {"ok": not err, "count": len(ids), "models": ids, "error": err}


@router.patch("/api/admin/providers/{code}")
async def update(
    code: str,
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
):
    _check(x_admin_token)
    body: dict[str, Any] = await request.json()
    if not await providers.row(code):
        raise HTTPException(404, "未知厂商")

    fields: dict[str, Any] = {}
    for k in ("label", "base_url", "list_url", "model_id", "enabled", "sort"):
        if k in body:
            fields[k] = body[k]
    if "params" in body:
        fields["params"] = json.dumps(body["params"] or {}, ensure_ascii=False)

    # 允许只填模型名不点"拉取"——MiniMax/混元的 /models 经常 404
    model = fields.get("model_id")
    if isinstance(model, str) and model.strip():
        fields["model_id"] = model.strip()

    await providers.save(code, **fields)
    log.info("provider %s updated: %s", code, {k: v for k, v in fields.items() if k != "params"})
    return {"ok": True}
