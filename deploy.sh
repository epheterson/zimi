#!/bin/bash
# Deploy Zimi — NAS + local app build
set -e

echo "=== Deploying to NAS ==="
ssh nas "mkdir -p /volume1/docker/kiwix/zimi/templates /volume1/docker/kiwix/zimi/assets"
cat zimi/server.py | ssh nas "cat > /volume1/docker/kiwix/zimi/server.py"
cat zimi/__init__.py | ssh nas "cat > /volume1/docker/kiwix/zimi/__init__.py"
cat zimi/__main__.py | ssh nas "cat > /volume1/docker/kiwix/zimi/__main__.py"
cat zimi/mcp_server.py | ssh nas "cat > /volume1/docker/kiwix/zimi/mcp_server.py"
cat zimi/templates/index.html | ssh nas "cat > /volume1/docker/kiwix/zimi/templates/index.html"
cat zimi/assets/icon.png | ssh nas "cat > /volume1/docker/kiwix/zimi/assets/icon.png"
cat zimi/assets/apple-touch-icon.png | ssh nas "cat > /volume1/docker/kiwix/zimi/assets/apple-touch-icon.png"
cat requirements.txt | ssh nas "cat > /volume1/docker/kiwix/requirements.txt"
cat Dockerfile | ssh nas "cat > /volume1/docker/kiwix/Dockerfile"
echo "  Files copied"

ssh nas "cd /volume1/docker/kiwix && /usr/local/bin/docker compose build --no-cache" 2>&1 | tail -3
ssh nas "cd /volume1/docker/kiwix && /usr/local/bin/docker compose down && /usr/local/bin/docker compose up -d" 2>&1 | tail -3
echo "  NAS deployed"

echo ""
echo "=== Syncing vault ==="
mkdir -p ~/vault/infra/zim-reader/zimi/templates
cp zimi/server.py ~/vault/infra/zim-reader/zimi/server.py
cp zimi/__init__.py ~/vault/infra/zim-reader/zimi/__init__.py
cp zimi/templates/index.html ~/vault/infra/zim-reader/zimi/templates/index.html
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
