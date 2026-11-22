#!/usr/bin/env python3
"""把智能体头像统一换成 DiceBear 的 notionists（中文站叫「概念主义者」）。

不重新建智能体、不动提示词，只做两件事：
  1. 用同一个 seed（原来的文件名）重新下一套 SVG，落到 /avatars/notionists/
  2. 把 Mongo 里 versions[].avatar.filepath 的 /avatars/lobe/ 换成 /avatars/notionists/

为什么要换目录而不是原地覆盖：文件名一样，浏览器和 Caddy 都会按 immutable 缓存，
原地覆盖等于所有人继续看到旧头像。换目录是一次干净的缓存失效。

在服务器上跑：
    python3 /tmp/restyle-avatars.py --dry-run
    python3 /tmp/restyle-avatars.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OLD_DIR = "/opt/umf/deploy/web/app/avatars/lobe"
NEW_DIR = "/opt/umf/deploy/web/app/avatars/notionists"
NEW_URL = "/avatars/notionists"
STYLE = "notionists"
DICEBEAR = f"https://api.dicebear.com/9.x/{STYLE}/svg"

# notionists 是手绘人像，背景太深会吃掉线条，只用很浅的色
BG = ["ffd5dc", "d1d4f9", "c0aede", "b6e3f4", "ffdfbf", "d9f2e6", "f4e2c8", "e3e3f0"]


def fetch(name: str) -> tuple[str, bool]:
    path = os.path.join(NEW_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 200:
        return name, True
    seed = name.replace(".svg", "")
    color = BG[sum(ord(c) for c in seed) % len(BG)]
    url = f"{DICEBEAR}?{urllib.parse.urlencode({'seed': seed, 'size': 128, 'backgroundColor': color})}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                svg = resp.read()
            if svg[:4] == b"<svg":
                with open(path, "wb") as fh:
                    fh.write(svg)
                return name, True
        except Exception as exc:  # 网络抖一下不致命，重试
            print(f"  ! {name}: {exc}", file=sys.stderr)
            time.sleep(0.8 * (attempt + 1))
    return name, False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    names = sorted(n for n in os.listdir(OLD_DIR) if n.endswith(".svg"))
    print(f"旧头像 {len(names)} 个 → 新风格 {STYLE}")
    if args.dry_run:
        print("dry-run：不下载、不改库")
        return

    os.makedirs(NEW_DIR, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(fetch, names))
    ok = sum(1 for _, good in results if good)
    print(f"下载完成 {ok}/{len(results)}")

    js = """
const r = db.agents.updateMany({}, [{$set: {versions: {$map: {input: "$versions", as: "v", in: {
  $mergeObjects: ["$$v", {avatar: {$mergeObjects: ["$$v.avatar", {filepath: {
    $replaceOne: {input: "$$v.avatar.filepath", find: "/avatars/lobe/", replacement: "/avatars/notionists/"}
  }}]}}]
}}}}]}]);
print("agents updated: " + r.modifiedCount);
"""
    subprocess.run(["docker", "exec", "-i", "umf-chat-lite-mongodb-1", "mongosh", "LibreChat", "--quiet"],
                   input=js, text=True, check=False)


if __name__ == "__main__":
    main()
