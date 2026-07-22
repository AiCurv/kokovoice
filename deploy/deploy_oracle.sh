#!/bin/bash
# ── Kokovoicebot Oracle VM Deployment Script ──
# Deploys to the EXISTING Oracle Cloud VM at 130.210.12.9
# Run this ON the Oracle VM itself (SSH in and execute)
#
# This script does NOT create or replace any infrastructure.
# It only installs software and configures services on the existing VM.

set -euo pipefail

ORACLE_IP="130.210.12.9"
APP_DIR="/opt/kokovoicebot"
APP_USER="kokovoicebot"
REPO_URL=""  # Set this to your GitHub repo URL after pushing

echo "=== Kokovoicebot Deployment on Existing Oracle VM ==="
echo "VM IP: $ORACLE_IP"
echo "App directory: $APP_DIR"
echo ""

# ── 1. Create service user ──
echo "[1/10] Creating service user '$APP_USER'..."
sudo useradd -r -m -d "$APP_DIR" "$APP_USER" 2>/dev/null || echo "User already exists"

# ── 2. Install system packages ──
echo "[2/10] Installing system packages on existing VM..."
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv nginx espeak-ng

# ── 3. Clone/push repository ──
echo "[3/10] Setting up application code..."
if [ -n "$REPO_URL" ]; then
    sudo git clone "$REPO_URL" "$APP_DIR" 2>/dev/null || sudo git -C "$APP_DIR" pull
else
    echo "⚠️  REPO_URL not set. Copy the kokovoicebot project files manually:"
    echo "   scp -r /path/to/kokovoicebot/* $APP_USER@$ORACLE_IP:$APP_DIR/"
fi
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 4. Create Python virtualenv ──
echo "[4/10] Setting up Python virtual environment..."
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip install -r "$APP_DIR/oracle/requirements.txt"

# ── 5. Generate self-signed SSL certificate ──
echo "[5/10] Generating self-signed SSL certificate for Telegram webhook..."
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/kokovoicebot.key \
    -out /etc/nginx/ssl/kokovoicebot.crt \
    -subj "/CN=$ORACLE_IP" 2>/dev/null || echo "Certificate already exists"

# ── 6. Configure nginx ──
echo "[6/10] Configuring nginx reverse proxy..."
sudo cp "$APP_DIR/deploy/nginx_kokovoicebot.conf" /etc/nginx/sites-available/kokovoicebot.conf
sudo ln -sf /etc/nginx/sites-available/kokovoicebot.conf /etc/nginx/sites-enabled/kokovoicebot.conf
sudo nginx -t && sudo systemctl reload nginx

# ── 7. Configure secrets in systemd service ──
echo "[7/10] Setting up systemd service..."
# IMPORTANT: You MUST edit this file with your actual secret values
# before starting the service.
sudo cp "$APP_DIR/deploy/kokovoicebot.service" /etc/systemd/system/kokovoicebot.service
echo ""
echo "⚠️  CRITICAL: Edit the systemd service file with your actual secret values:"
echo "   sudo nano /etc/systemd/system/kokovoicebot.service"
echo ""
echo "   Replace these placeholders:"
echo "   %TELEGRAM_BOT_TOKEN%     → Your Telegram bot token"
echo "   %ALLOWED_TELEGRAM_USER_ID% → 6404893345"
echo "   %GITHUB_REPO_OWNER%      → Your GitHub username"
echo "   %GITHUB_REPO_NAME%       → kokovoicebot (or your repo name)"
echo "   %GITHUB_DISPATCH_TOKEN%  → Your fine-grained GitHub PAT"
echo "   %ORACLE_COMPLETION_SECRET% → A random secret you generate"
echo "   %ORACLE_PUBLIC_DOMAIN%   → $ORACLE_IP"
echo ""

# ── 8. Set Telegram webhook ──
echo "[8/10] Setting Telegram webhook..."
echo "The webhook will be set automatically when the service starts."
echo "Webhook URL: https://$ORACLE_IP/webhook"
echo ""

# ── 9. Enable and start service ──
echo "[9/10] Enabling kokovoicebot service..."
sudo systemctl daemon-reload
sudo systemctl enable kokovoicebot
# Don't start yet — user needs to fill in secrets first

# ── 10. Verify ──
echo "[10/10] Verification checklist..."
echo ""
echo "=== Deployment files installed ==="
echo ""
echo "NEXT STEPS (do these manually):"
echo "1. Edit secrets in the systemd service file:"
echo "   sudo nano /etc/systemd/system/kokovoicebot.service"
echo ""
echo "2. Start the service:"
echo "   sudo systemctl start kokovoicebot"
echo ""
echo "3. Verify health:"
echo "   curl -k https://localhost/health"
echo "   curl -k https://$ORACLE_IP/health"
echo ""
echo "4. Set GitHub repository secrets:"
echo "   ORACLE_COMPLETION_URL = https://$ORACLE_IP/completion"
echo "   ORACLE_COMPLETION_SECRET = <same value as in systemd service>"
echo ""
echo "5. Test by sending text to your Telegram bot"
echo ""
