#!/usr/bin/env python3
"""把智能体头像搬进 Cloudflare R2，并把 Mongo 里的地址改成 R2 公开 URL。

为什么不用本地 /avatars/*.svg 了：
  本地目录跟着服务器走，换机器要重新生成；R2 是独立的一份资产，
  再配上 img.example.com 这个 CF 自定义域，全球访问都走边缘缓存。

键的约定：a/{原文件名}.svg —— 与原目录同名，方便对照；
头像更新时换文件名（加内容哈希）即可自然失效缓存，不做原地覆盖。

在服务器上跑：
    python3 /tmp/avatars-to-r2.py --dry-run
    python3 /tmp/avatars-to-r2.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

AVATAR_DIR = "/opt/umf/deploy/web/app/avatars/notionists"
ENV_FILE = "/opt/umf/deploy/.env"
MONGO = "umf-chat-lite-mongodb-1"


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
AID = E["R2_ACCOUNT_ID"]
TOKEN = E["R2_API_TOKEN"]
BUCKET = E.get("R2_BUCKET", "umf-avatars")
JUR = E.get("R2_JURISDICTION", "default")
BASE = E["R2_PUBLIC_BASE"].rstrip("/")


def mongosh(js: str) -> str:
    path = "/tmp/_r2js.js"
    open(path, "w", encoding="utf-8").write(js)
    subprocess.run(["docker", "cp", path, f"{MONGO}:/tmp/_r2js.js"], check=True,
                   stdout=subprocess.DEVNULL)
    r = subprocess.run(["docker", "exec", MONGO, "mongosh", "LibreChat", "--quiet", "-f", "/tmp/_r2js.js"],
                       capture_output=True, text=True, check=True)
    return r.stdout


def put(data: bytes, key: str) -> None:
    url = f"https://api.cloudflare.com/client/v4/accounts/{AID}/r2/buckets/{BUCKET}/objects/{key}"
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("cf-r2-jurisdiction", JUR)
    req.add_header("Content-Type", "image/svg+xml")
    with urllib.request.urlopen(req, timeout=40) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PUT {key} -> {resp.status}")


def upload_one(name: str) -> tuple[str, str, bool]:
    path = os.path.join(AVATAR_DIR, name)
    if not os.path.exists(path):
        return name, "", False
    data = open(path, "rb").read()
    if data[:4] != b"<svg":
        return name, "", False
    key = f"a/{name}"
    for attempt in range(3):
        try:
            put(data, key)
            return name, f"{BASE}/{key}", True
        except Exception as exc:  # 网络抖动重试两次
            if attempt == 2:
                print(f"  ! {name}: {exc}")
    return name, "", False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    raw = mongosh(
        'const out=[];db.agents.find({},{id:1,name:1,"versions":1}).forEach(a=>{'
        'const v=(a.versions||[])[0]||{};const fp=(v.avatar||{}).filepath||"";'
        'out.push({id:a.id,name:a.name,fp:fp});});print(JSON.stringify(out));'
    ).strip().splitlines()[-1]
    agents = json.loads(raw)
    todo = [a for a in agents if a["fp"].startswith("/avatars/notionists/")]
    names = sorted({a["fp"].rsplit("/", 1)[-1] for a in todo})
    print(f"agents={len(agents)} local={len(todo)} files={len(names)}")
    if not names:
        print("nothing to do")
        return 0
    if args.dry_run:
        print("sample:", names[:3], "->", f"{BASE}/a/...")
        return 0

    done: dict[str, str] = {}
    bad = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (name, url, ok) in enumerate(pool.map(upload_one, names), 1):
            if ok:
                done[name] = url
            else:
                bad += 1
            if i % 50 == 0:
                print(f"  uploaded {i}/{len(names)}")

    print(f"uploaded={len(done)} failed={bad}")
    if bad:
        print("有文件上传失败，先不改进 Mongo（否则会指向不存在的对象）")
        return 1

    # 一次性把 511 条记录的 filepath 换掉：只动 versions[].avatar.filepath
    pairs = {f"/avatars/notionists/{n}": u for n, u in done.items()}
    js_pairs = json.dumps(pairs, ensure_ascii=False)
    out = mongosh(
        f"const m={js_pairs};let n=0;"
        "db.agents.find({}).forEach(a=>{"
        "let ch=false;const vs=(a.versions||[]).map(v=>{"
        "if(v.avatar&&m[v.avatar.filepath]){v.avatar.filepath=m[v.avatar.filepath];ch=true;}return v;});"
        "if(ch){db.agents.updateOne({_id:a._id},{$set:{versions:vs}});n++;}});"
        'print("updated "+n);'
    )
    print(out.strip().splitlines()[-1])
    print(mongosh('print("left_local="+db.agents.countDocuments({"versions.0.avatar.filepath":/^\\/avatars\\//}))'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
