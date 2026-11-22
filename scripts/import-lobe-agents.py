#!/usr/bin/env python3
"""把 lobehub/lobe-chat-agents 的智能体灌进 LibreChat 的智能体市场。

做四件事：
1. 读仓库里的 JSON（优先 zh-CN，回退默认语言）
2. 用 DiceBear 生成头像，落到本地静态目录（不依赖 dicebear 在线服务，国内也能秒开）
3. 调 POST /api/agents 建卡，分类映射到 LibreChat 固定的 7 个 tab
4. 调 PUT /api/permissions/agent/<_id> 把卡片授权给 public —— 不做这步，市场里谁都看不见

用法：
    python3 import-lobe-agents.py --dry-run     # 只打印统计，不写数据
    python3 import-lobe-agents.py               # 真跑
    python3 import-lobe-agents.py --limit 5     # 只导前 5 个，用来试水

幂等：把已导入的 identifier 记在 STATE_FILE 里，重跑只补新增的。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("UMF_BASE_URL", "http://localhost").rstrip("/")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REPO_DIR = os.getenv("LOBE_AGENTS_DIR", "/tmp/lobe-chat-agents-main/src")
AVATAR_DIR = os.getenv("LOBE_AVATAR_DIR", "/opt/umf/deploy/web/app/avatars/lobe")
AVATAR_URL = BASE + "/avatars/lobe"
STATE_FILE = "/opt/umf/deploy/.agent-import.json"
ENV_FILE = os.getenv("UMF_ENV_FILE", "/opt/umf/deploy/.env")

# 登录用的账号密码从环境变量读。
# ★ 这里以前写死了明文邮箱和密码 —— 那个脚本一度差点被推到公开仓库。
#   现在缺变量就直接退出，不再有"默认值"可以蒙混过关。
LOGIN_EMAIL = os.getenv("UMF_EMAIL", "").strip()
LOGIN_PASSWORD = os.getenv("UMF_PASSWORD", "").strip()

# DiceBear 的 bottts-neutral：机器人脸，比人像更"像个智能体"，也不冒犯真人
DICEBEAR = "https://api.dicebear.com/9.x/bottts-neutral/svg"
# 深色主题下太暗的背景会糊成一团，只用中低明度的柔和色
BG = ["ffd5dc", "d1d4f9", "c0aede", "b6e3f4", "ffdfbf", "d9f2e6", "f4e2c8", "e3e3f0"]

# LobeChat 的 14 个分类 → LibreChat 市场固定的 7 个 tab
MACRO = {
    "programming": "rd",
    "design": "rd",
    "career": "hr",
    "marketing": "sales",
    "education": "general",
    "academic": "general",
    "copywriting": "general",
    "general": "general",
    "life": "general",
    "entertainment": "general",
    "translation": "general",
    "emotions": "general",
    "games": "general",
    "office": "general",
}

# LobeChat 没有财务 / IT / 售后 这三个分类，靠关键词捞。
# 顺序有意义：先捞细分职能，再让大类兜底。
OVERRIDE = [
    (("财务", "会计", "报税", "发票", "预算", "理财", "投资", "审计", "成本"), "finance"),
    (("售后", "客服", "投诉", "退货", "工单", "回访"), "aftersales"),
    (("运维", "服务器", "网络", "linux", "docker", "kubernetes", "k8s", "数据库", "dba", "防火墙", "监控"), "it"),
    (("营销", "推广", "广告", "品牌", "seo", "获客", "销售", "商务"), "sales"),
    (("招聘", "简历", "面试", "绩效", "人事", "hr"), "hr"),
]


def api(path: str, token: str | None = None, method: str = "GET", body: dict | None = None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        return exc.code, {"error": raw[:200]}


def admin_token() -> str:
    for line in open(ENV_FILE):
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("ADMIN_TOKEN not found")


def login() -> str:
    if not (LOGIN_EMAIL and LOGIN_PASSWORD):
        raise SystemExit(
            "缺 UMF_EMAIL / UMF_PASSWORD。\n"
            "用法：UMF_EMAIL=you@example.com UMF_PASSWORD='...' python3 import-lobe-agents.py"
        )
    st, d = api(
        "/api/auth/login",
        method="POST",
        body={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
    )
    if st != 200 or "token" not in d:
        raise SystemExit(f"login failed: {st} {d}")
    return d["token"]


def load_agents() -> list[dict]:
    """同 identifier 有多语言版本，zh-CN 优先，其次默认文件。"""
    best: dict[str, tuple[int, dict]] = {}
    rank = {"zh-CN": 100, "zh-TW": 90, "zh": 95}
    for name in os.listdir(REPO_DIR):
        if not name.endswith(".json"):
            continue
        parts = name[:-5].split(".")
        ident, locale = parts[0], (parts[1] if len(parts) > 1 else "")
        score = rank.get(locale, 10 if locale else 50)
        try:
            data = json.load(open(os.path.join(REPO_DIR, name), encoding="utf-8"))
        except Exception:
            continue
        if not (data.get("config") or {}).get("systemRole"):
            continue
        if ident not in best or score > best[ident][0]:
            best[ident] = (score, data)
    return [d for _, d in best.values()]


def pick_category(meta: dict) -> str:
    text = " ".join(
        [meta.get("title") or "", meta.get("description") or ""] + list(meta.get("tags") or [])
    ).lower()
    for words, cat in OVERRIDE:
        if any(w in text for w in words):
            return cat
    return MACRO.get((meta.get("category") or "general").lower(), "general")


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fallback_svg(ident: str) -> bytes:
    """DiceBear 挂掉时的兜底：同一个人永远同一个色块 + 首字母，不至于开天窗。"""
    color = BG[sum(ord(c) for c in ident) % len(BG)]
    ch = (ident.strip()[:1] or "A").upper()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="128" height="128">'
        f'<rect width="120" height="120" rx="28" fill="#{color}"/>'
        f'<text x="60" y="76" font-family="system-ui,-apple-system,sans-serif" font-size="50" '
        f'font-weight="600" fill="#3a3a46" text-anchor="middle">{ch}</text></svg>'
    ).encode()


def fetch_avatar(ident: str) -> str:
    path = os.path.join(AVATAR_DIR, f"{ident}.svg")
    if os.path.exists(path) and os.path.getsize(path) > 200:
        return f"{AVATAR_URL}/{ident}.svg"
    color = BG[sum(ord(c) for c in ident) % len(BG)]
    url = f"{DICEBEAR}?{urllib.parse.urlencode({'seed': ident, 'size': 128, 'backgroundColor': color})}"
    svg = b""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                svg = resp.read()
            if svg[:4] == b"<svg":
                break
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    if svg[:4] != b"<svg":
        svg = _fallback_svg(ident)
    os.makedirs(AVATAR_DIR, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(svg)
    return f"{AVATAR_URL}/{ident}.svg"


def prefetch_avatars(idents: list[str], workers: int = 8) -> None:
    """头像下载是纯 IO，串行跑 500 张要几分钟，开线程池压到几十秒。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch_avatar, idents))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    agents = load_agents()
    agents.sort(key=lambda d: (d.get("meta") or {}).get("category") or "")
    if args.limit:
        agents = agents[: args.limit]

    if args.dry_run:
        from collections import Counter

        cats = Counter(pick_category(d.get("meta") or {}) for d in agents)
        avatars = Counter(
            "emoji" if len((d.get("meta") or {}).get("avatar") or "") <= 4 else "url"
            for d in agents
        )
        print(f"agents={len(agents)} categories={dict(cats)} origin_avatars={dict(avatars)}")
        for d in agents[:3]:
            m = d["meta"]
            print("-", m.get("title"), "|", pick_category(m), "|", clip(m.get("description"), 60))
        return

    os.makedirs(AVATAR_DIR, exist_ok=True)
    state = {"done": {}}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    pending = [d for d in agents if (d.get("identifier") or "") not in state["done"]]
    print(f"{len(agents)} agents, already imported {len(state['done'])}, pending {len(pending)}")
    if pending:
        print("downloading avatars…")
        prefetch_avatars([d.get("identifier") or "" for d in pending])
    tok = login()
    print("login ok")

    created = skipped = failed = 0
    for i, d in enumerate(agents, 1):
        meta = d.get("meta") or {}
        cfg = d.get("config") or {}
        ident = d.get("identifier") or f"agent{i}"
        if ident in state["done"]:
            skipped += 1
            continue
        title = clip(meta.get("title") or ident, 40)
        try:
            avatar = fetch_avatar(ident)
        except Exception as exc:
            print(f"  ! avatar fail {ident}: {exc}")
            avatar = f"{BASE}/icons/icon-192.png"

        payload = {
            "name": title,
            "description": clip(meta.get("description") or "", 180),
            "instructions": (cfg.get("systemRole") or "").strip(),
            "provider": "Brain",
            "model": "deepseek",
            "avatar": {"source": "url", "filepath": avatar},
            "conversation_starters": [clip(q, 60) for q in (cfg.get("openingQuestions") or [])[:4]],
            "category": pick_category(meta),
            "tools": [],
        }
        st, res = api("/api/agents", tok, "POST", payload)
        if st not in (200, 201):
            failed += 1
            print(f"  x create {ident}: {st} {str(res)[:120]}")
            continue
        aid, oid = res.get("id"), res.get("_id")
        if not oid:
            st2, detail = api(f"/api/agents/{aid}", tok)
            oid = detail.get("_id")
        st, res = api(
            f"/api/permissions/agent/{oid}",
            tok,
            "PUT",
            {"public": True, "publicAccessRoleId": "agent_viewer", "updated": [], "removed": []},
        )
        if st != 200:
            print(f"  ! public {ident}: {st} {str(res)[:120]}")
        state["done"][ident] = {"agent_id": aid, "name": title}
        created += 1
        if created % 20 == 0:
            json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)
            print(f"  ... {created} created")
        time.sleep(0.12)

    json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)
    print(f"done: created={created} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
