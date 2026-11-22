#!/usr/bin/env bash
# 从官方 LibreChat 镜像里把已经编译好的前端产物抽到宿主机，由 Caddy 直接托管。
#
# 为什么要这么做：librechat-dev 镜像里**没有静态服务器**（CMD 是 npm run backend），
# 它只提供 /app/client/dist 这份构建产物。所以要自己把它拿出来，交给 Caddy 发。
#
# 好处：
#   1. 不需要 npm install / pnpm build（2C4G 上那一步会 OOM）
#   2. 不需要额外跑一个 client 容器（省 128MB 内存和一个进程）
#   3. index.html 是普通文件，注入 PWA 标签就是改文件，删了重跑即可回滚
#
# 代价：产物和镜像版本绑定。升级镜像后重跑本脚本。
#
# 用法：
#   ./scripts/extract-static.sh                    # 默认 latest
#   ./scripts/extract-static.sh <image:tag>        # 锁定版本

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-ghcr.io/danny-avila/librechat-dev:latest}"
OUT="$ROOT/deploy/web/app"
TMP="umf_static_$$"

command -v docker >/dev/null || { echo "需要 docker"; exit 1; }

echo "[1/3] 从 $IMAGE 抽取前端产物"
docker create --name "$TMP" "$IMAGE" >/dev/null
trap 'docker rm -f "$TMP" >/dev/null 2>&1 || true' EXIT

rm -rf "$OUT"
mkdir -p "$(dirname "$OUT")"
if ! docker cp "$TMP:/app/client/dist" "$OUT" 2>/dev/null; then
  echo "      镜像里没有 /app/client/dist，列一下候选：" >&2
  docker run --rm --entrypoint sh "$IMAGE" -c \
    'find / -maxdepth 6 -name index.html -not -path "*/node_modules/*" 2>/dev/null | head' >&2 || true
  exit 3
fi
[ -f "$OUT/index.html" ] || { echo "      抽出来了但没有 index.html" >&2; exit 4; }
echo "      $(du -sh "$OUT" | cut -f1)，$(ls "$OUT/assets" 2>/dev/null | wc -l) 个静态资源"

echo "[2/3] 注入 PWA 标签（幂等）"
PY=python3
command -v python3 >/dev/null || PY=python
"$PY" - "$OUT/index.html" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
html = path.read_text(encoding="utf-8")

if "umf-pwa-injected" in html:
    print("      已注入过，跳过")
    sys.exit(0)

# 品牌与主题色：直接改原值，避免出现两个 theme-color 让浏览器挑错
html = re.sub(
    r'<meta name="theme-color" content="[^"]*"',
    '<meta name="theme-color" content="#0b0f17"',
    html,
)
html = re.sub(r"<title>[^<]*</title>", "<title>UMF</title>", html)
# LibreChat 自带一个 manifest 链接，先摘掉，避免两个 rel=manifest 抢所有权
html = re.sub(r'\s*<link[^>]+rel="manifest"[^>]*>', "", html)

BLOCK = """    <!-- umf-pwa-injected -->
    <link rel="manifest" href="/manifest.webmanifest" />
    <meta name="apple-mobile-web-app-title" content="UMF" />
    <link rel="apple-touch-icon" href="/icons/icon-192.png" />
    <script>
      if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
          navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
        });
      }
    </script>
"""

if "</head>" not in html:
    print("      警告：找不到 </head>，这个版本的 index.html 结构变了", file=sys.stderr)
    sys.exit(2)

path.write_text(html.replace("</head>", BLOCK + "  </head>", 1), encoding="utf-8")
print("      完成")
PY

echo "[3/3] 产物：$OUT"
grep -c "manifest.webmanifest" "$OUT/index.html" | sed 's/^/      manifest 引用数：/'
grep -o "<title>[^<]*</title>" "$OUT/index.html" | sed 's/^/      /'
echo
echo "下一步：docker compose -f docker-compose.lite.yml up -d"
