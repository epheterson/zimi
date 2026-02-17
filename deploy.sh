#!/bin/bash
# Deploy Zimi — NAS + local app build
set -e

echo "=== Deploying to NAS ==="
cat zimi.py | ssh nas "cat > /volume1/docker/kiwix/zimi.py"
cat templates/index.html | ssh nas "cat > /volume1/docker/kiwix/templates/index.html"
cat Dockerfile | ssh nas "cat > /volume1/docker/kiwix/Dockerfile"
ssh nas "mkdir -p /volume1/docker/kiwix/assets"
cat assets/icon.png | ssh nas "cat > /volume1/docker/kiwix/assets/icon.png"
cat assets/apple-touch-icon.png | ssh nas "cat > /volume1/docker/kiwix/assets/apple-touch-icon.png"
echo "  Files copied"

ssh nas "cd /volume1/docker/kiwix && /usr/local/bin/docker compose build --no-cache" 2>&1 | tail -3
ssh nas "cd /volume1/docker/kiwix && /usr/local/bin/docker compose down && /usr/local/bin/docker compose up -d" 2>&1 | tail -3
echo "  NAS deployed"

echo ""
echo "=== Syncing vault ==="
cp zimi.py ~/vault/infra/zim-reader/zimi.py
cp templates/index.html ~/vault/infra/zim-reader/templates/index.html
echo "  Vault synced"

echo ""
echo "=== Building desktop app ==="
pkill -9 -f "dist/Zimi" 2>/dev/null || true
sleep 1
pyinstaller zimi_desktop.spec --noconfirm 2>&1 | tail -5
echo "  App built"

echo ""
echo "=== Launching app ==="
open dist/Zimi.app
echo "  Done!"
