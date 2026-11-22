#!/usr/bin/env bash
# 一键拉取本项目的全部上游依赖源码到 vendor/
#
# 国内直连 GitHub 通常不通，默认走 gh-proxy 镜像。如果你有别的通道：
#   GH_PROXY= GIT_HOST=github.com ./scripts/fetch-vendor.sh
#
# 说明：vendor/ 只读参考 + fork 基底，不要在里面直接改业务代码。
#      业务代码全部在 deploy/ 下，升级上游时 vendor 随时可删可重拉。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor"
GH_PROXY="${GH_PROXY-https://gh-proxy.com/}"
mkdir -p "$VENDOR"

clone() {
  local name="$1" repo="$2" extra="${3:-}"
  if [ -d "$VENDOR/$name/.git" ]; then
    echo "[skip] $name 已存在"
    return
  fi
  echo "[clone] $name"
  # shellcheck disable=SC2086
  git clone --depth 1 $extra "${GH_PROXY}https://github.com/${repo}.git" "$VENDOR/$name"
}

# 聊天前端（MIT）—— 我们要 fork 它加 PWA / 邀请码 / 侧栏入口
clone librechat danny-avila/LibreChat

# 记忆引擎（Apache-2.0）—— 默认休眠，MEM0_ENABLED=1 时启用
clone mem0 mem0ai/mem0

# 对话压缩（MIT）—— LLMLingua-2，token 级压缩
clone LLMLingua microsoft/LLMLingua

# 模型网关（MIT）—— 只用它的 Docker 镜像 + config，源码稀疏拉取即可
clone litellm BerriAI/litellm "--filter=blob:none --sparse"
if [ -d "$VENDOR/litellm/.git" ]; then
  git -C "$VENDOR/litellm" sparse-checkout set litellm proxy_server_config.yaml || true
fi

# LobeHub v2（Community License，非商用放行）—— 不 fork，只当"记忆分层 / 压缩可见性"的设计参考
# 见 docs/05-选型对照与服务器配置.md
clone lobehub lobehub/lobehub "--filter=blob:none --sparse"
if [ -d "$VENDOR/lobehub/.git" ]; then
  git -C "$VENDOR/lobehub" sparse-checkout set \
    src/services/userMemory \
    src/store/chat \
    src/features/Conversation \
    packages/types \
    apps/server/src/services/memory \
    apps/server/src/router-hono/workflows \
    docs/usage/getting-started || true
fi

echo
echo "完成。各仓库版本："
for d in "$VENDOR"/*/; do
  [ -d "$d/.git" ] && printf "  %-12s %s\n" "$(basename "$d")" "$(git -C "$d" log -1 --format='%h %cs')"
done
