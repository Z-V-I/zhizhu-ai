#!/usr/bin/env python3
"""智能体头像：DiceBear notionists（概念主义者）→ Cloudflare R2。

踩过的坑（别再犯）：
  1. Mongo 里智能体有 **两个** 头像字段：顶层 `avatar` 和 `versions[].avatar`。
     UI 读的是顶层那个，只改 versions 等于没改。
  2. 换风格必须换 URL。同名覆盖会被浏览器/CDN 的缓存吃掉，
     所以这次换成 R2 前缀 a/v2/，等于一次干净的缓存失效。
  3. source 要写 "url"，否则客户端会把它当本地相对路径再拼一次前缀。

在服务器上跑：
    python3 /tmp/avatars-r2-sync.py --dry-run
    python3 /tmp/avatars-r2-sync.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

STYLE = "notionists"
DICEBEAR = f"https://api.dicebear.com/10.x/{STYLE}/svg"
CACHE_DIR = "/opt/umf/deploy/web/app/avatars/notionists"   # 本地缓存源文件
KEY_PREFIX = "a/v2"                                        # 换前缀 = 缓存失效
ENV_FILE = "/opt/umf/deploy/.env"
MONGO = "umf-chat-lite-mongodb-1"

BG = ["ffd5dc", "d1d4f9", "c0aede", "b6e3f4", "ffdfbf", "d9f2e6", "f4e2c8", "e3e3f0"]


def env() -> dict[str, str]:
    out = {}
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


E = env()
AID, TOKEN = E["R2_ACCOUNT_ID"], E["R2_API_TOKEN"]
BUCKET, JUR = E.get("R2_BUCKET", "umf-avatars"), E.get("R2_JURISDICTION", "default")
BASE = E["R2_PUBLIC_BASE"].rstrip("/")


def mongosh(js: str) -> str:
    path = "/tmp/_r2sync.js"
    open(path, "w", encoding="utf-8").write(js)
    subprocess.run(["docker", "cp", path, f"{MONGO}:/tmp/_r2sync.js"], check=True, stdout=subprocess.DEVNULL)
    r = subprocess.run(["docker", "exec", MONGO, "mongosh", "LibreChat", "--quiet", "-f", "/tmp/_r2sync.js"],
                       capture_output=True, text=True, check=True)
    return r.stdout


def local_svg(name: str, refresh: bool) -> bytes | None:
    path = os.path.join(CACHE_DIR, name)
    if os.path.exists(path) and not refresh and os.path.getsize(path) > 200:
        data = open(path, "rb").read()
        # 9.x 画的和 10.x 不完全一样，用元数据判断一下更稳：这里只认本地已有的
        if data[:4] == b"<svg":
            return data
    seed = name[:-4] if name.endswith(".svg") else name
    color = BG[sum(ord(c) for c in seed) % len(BG)]
    url = f"{DICEBEAR}?{urllib.parse.urlencode({'seed': seed, 'size': 128, 'backgroundColor': color})}"
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                svg = resp.read()
            if svg[:4] == b"<svg":
                os.makedirs(CACHE_DIR, exist_ok=True)
                open(path, "wb").write(svg)
                return svg
        except Exception:
            pass
    return None


def put(data: bytes, key: str) -> None:
    url = f"https://api.cloudflare.com/client/v4/accounts/{AID}/r2/buckets/{BUCKET}/objects/{key}"
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("cf-r2-jurisdiction", JUR)
    req.add_header("Content-Type", "image/svg+xml")
    with urllib.request.urlopen(req, timeout=40) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PUT {key} -> {resp.status}")


def one(name: str) -> tuple[str, str]:
    svg = local_svg(name, REFRESH)
    if not svg:
        return name, ""
    key = f"{KEY_PREFIX}/{name}"
    for attempt in range(3):
        try:
            put(svg, key)
            return name, f"{BASE}/{key}"
        except Exception as exc:
            if attempt == 2:
                print(f"  ! {name}: {exc}")
    return name, ""


REFRESH = False


def main() -> int:
    global REFRESH
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="强制重新下载 SVG")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    REFRESH = args.refresh

    raw = mongosh(
        'const out=[];db.agents.find({},{id:1,avatar:1,versions:1}).forEach(a=>{'
        'const fps=[];if(a.avatar&&a.avatar.filepath)fps.push(a.avatar.filepath);'
        '(a.versions||[]).forEach(v=>{if(v.avatar&&v.avatar.filepath)fps.push(v.avatar.filepath)});'
        'out.push({id:a.id,fps:fps});});print(JSON.stringify(out));'
    ).strip().splitlines()[-1]
    agents = json.loads(raw)

    names = set()
    for a in agents:
        for fp in a["fps"]:
            base = fp.rsplit("/", 1)[-1]
            if base.endswith(".svg") and ("/avatars/" in fp):
                names.add(base)
    names = sorted(names)
    print(f"agents={len(agents)} files={len(names)}")
    if not names:
        print("nothing to do")
        return 0
    if args.dry_run:
        print("sample:", names[:3], "->", f"{BASE}/{KEY_PREFIX}/...")
        return 0

    mapping: dict[str, str] = {}
    bad = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (name, url) in enumerate(pool.map(one, names), 1):
            if url:
                mapping[name] = url
            else:
                bad += 1
            if i % 100 == 0:
                print(f"  {i}/{len(names)}")
    print(f"uploaded={len(mapping)} failed={bad}")
    if bad:
        print("有失败项，先不改 Mongo")
        return 1

    js = f"""
const m = {json.dumps(mapping, ensure_ascii=False)};
const pick = fp => m[(fp || '').split('/').pop()] || null;
let changed = 0;
db.agents.find({{}}).forEach(a => {{
  let touched = false;
  if (a.avatar && a.avatar.filepath) {{
    const u = pick(a.avatar.filepath);
    if (u && a.avatar.filepath !== u) {{ a.avatar.filepath = u; a.avatar.source = 'url'; touched = true; }}
  }}
  (a.versions || []).forEach(v => {{
    if (v.avatar && v.avatar.filepath) {{
      const u = pick(v.avatar.filepath);
      if (u && v.avatar.filepath !== u) {{ v.avatar.filepath = u; v.avatar.source = 'url'; touched = true; }}
    }}
  }});
  if (touched) {{ db.agents.updateOne({{_id: a._id}}, {{$set: {{avatar: a.avatar, versions: a.versions}}}}); changed++; }}
}});
print('agents updated ' + changed);
print('r2 top-level ' + db.agents.countDocuments({{'avatar.filepath': /img\\.thinking\\.you/}}));
print('r2 versions  ' + db.agents.countDocuments({{'versions.0.avatar.filepath': /img\\.thinking\\.you/}}));
"""
    print(mongosh(js).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
