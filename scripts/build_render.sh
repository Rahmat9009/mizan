#!/usr/bin/env bash
set -euo pipefail

# 1. Install python package and dependencies
pip install --upgrade pip
pip install -e .

# 2. Ensure node / npm is available for building the Vite frontend
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found on PATH; installing standalone Node.js..."
  NODE_VERSION="20.18.0"
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *) NODE_ARCH="x64" ;;
  esac
  curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz
  tar -xf /tmp/node.tar.xz -C /tmp
  export PATH="/tmp/node-v${NODE_VERSION}-linux-${NODE_ARCH}/bin:$PATH"
fi

echo "Node version: $(node --version)"
echo "npm version: $(npm --version)"

# 3. Build frontend SPA
cd web
npm ci
VITE_API_MODE=http VITE_API_BASE_URL=/api/v1 npm run build
cd ..
