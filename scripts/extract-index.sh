#!/usr/bin/env bash
# 从官方 LibreChat 镜像里抽出 index.html，注入 PWA 需要的 6 行，再挂载回去。
#
# 这是"不构建前端也能装成 App"的关键一步：
#   正常做法  fork 源码 → pnpm install → pnpm build（2C4G 上要 2GB 峰值内存，30 分钟）
#   这个做法  docker create → docker cp → 插 6 行 → 挂载（10 秒，0 内存峰值）
#
# 代价：index.html 和镜像版本绑定。升级镜像后重跑一次这个脚本即可。
#
# 用法：
#   ./scripts/extract-index.sh                       # 用默认镜像
#   ./scripts/extract-index.sh <image:tag>           # 指定版本

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-ghcr.io/danny-avila/librechat-dev:latest}"
OUT="$ROOT/deploy/librechat/index.html"
TMP="umf_index_$$"

command -v docker >/dev/null || { echo "需要 docker"; exit 1; }
mkdir -p "$(dirname "$OUT")"

echo "[1/3] 从 $IMAGE 取 index.html"
docker create --name "$TMP" "$IMAGE" >/dev/null
trap 'docker rm -f "$TMP" >/dev/null 2>&1 || true' EXIT

# 不同版本/不同镜像（dev vs 正式）的静态目录不一定是同一个路径，逐个试
FOUND=""
for candidate in \
  /usr/share/nginx/html/index.html \
  /app/client/dist/index.html \
  /app/client/public/index.html; do
  if docker cp "$TMP:$candidate" "$OUT" 2>/dev/null; then
    FOUND="$candidate"
    break
  fi
done

if [ -z "$FOUND" ]; then
  echo "      没找到 index.html。列一下镜像里的候选路径：" >&2
  docker run --rm --entrypoint sh "$IMAGE" -c \
    'find / -maxdepth 6 -name index.html -not -path "*/node_modules/*" 2>/dev/null | head' >&2 || true
  exit 3
fi
echo "      来源：$FOUND"

# 挂载路径要和 compose 里一致，这里顺手核对一下
grep -q "$FOUND" "$ROOT/deploy/docker-compose.lite.yml" 2>/dev/null || \
  echo "      注意：compose 里挂载的容器路径可能和上面不一致，请核对 client.volumes"

echo "[2/3] 注入 PWA 标签（幂等，重复跑不会插两遍）"
python - "$OUT" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
html = path.read_text(encoding="utf-8")

if "manifest.webmanifest" in html:
    print("      已注入过，跳过")
    sys.exit(0)

BLOCK = """    <!-- UMF PWA：由 scripts/extract-index.sh 注入，升级镜像后重跑脚本 -->
    <link rel="manifest" href="/manifest.webmanifest" />
    <meta name="theme-color" content="#0b0f17" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
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
    print("      警告：找不到 </head>，这个版本的 index.html 结构变了，需要手工插入", file=sys.stderr)
    sys.exit(2)

path.write_text(html.replace("</head>", BLOCK + "  </head>", 1), encoding="utf-8")
print("      完成")
PY

echo "[3/3] 产物：$OUT"
grep -c "manifest.webmanifest" "$OUT" | sed 's/^/      manifest 引用数：/'
echo
echo "下一步：docker compose -f docker-compose.lite.yml up -d"
