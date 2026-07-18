#!/usr/bin/env bash
# One-time setup for a fresh Oracle Cloud "Always Free" Ampere A1 (Ubuntu) VM.
# Run this once via SSH on the new VM as the ubuntu user (has sudo).
#
#   curl -fsSL https://raw.githubusercontent.com/omixz/clipai/main/deploy/oracle-bootstrap.sh -o bootstrap.sh
#   bash bootstrap.sh
#
# It installs Docker, clones the repo, and brings the app + Caddy reverse
# proxy up. You still need to: (1) create a free DuckDNS hostname pointing
# at this VM's public IP and put it in Caddyfile, and (2) fill in .env with
# real secrets (copy them from the Render dashboard's Environment tab, or
# from wherever you've been storing them).
set -euo pipefail

REPO_URL="https://github.com/omixz/clipai.git"
APP_DIR="/opt/peakcut"

echo "==> Installing Docker..."
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"

echo "==> Opening firewall for HTTP/HTTPS..."
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT || true
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT || true
sudo netfilter-persistent save 2>/dev/null || true

echo "==> Cloning the repo..."
sudo mkdir -p "$APP_DIR"
sudo chown "$USER":"$USER" "$APP_DIR"
git clone "$REPO_URL" "$APP_DIR" 2>/dev/null || (cd "$APP_DIR" && git pull)
cd "$APP_DIR"

if [ ! -f .env ]; then
    cp .env.example .env
    echo ">>> Edit $APP_DIR/.env with your real secrets before continuing, then re-run:"
    echo ">>>   cd $APP_DIR && sudo docker compose up -d --build"
    exit 0
fi

if grep -q "your-hostname-here" Caddyfile; then
    echo ">>> Edit $APP_DIR/Caddyfile with your real DuckDNS hostname before continuing, then re-run:"
    echo ">>>   cd $APP_DIR && sudo docker compose up -d --build"
    exit 0
fi

echo "==> Building and starting..."
sudo docker compose up -d --build

echo "==> Done. Check status with: sudo docker compose ps"
echo "==> Logs: sudo docker compose logs -f app"
