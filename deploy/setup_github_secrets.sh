#!/bin/bash
# ── Kokovoicebot GitHub Secrets Setup ──
# Run this from your local machine to configure GitHub repository secrets.

set -euo pipefail

REPO="YOUR_GITHUB_USERNAME/kokovoicebot"

echo "=== Setting GitHub Repository Secrets ==="
echo ""
echo "Repository: $REPO"
echo ""

if ! command -v gh &> /dev/null; then
    echo "Installing GitHub CLI..."
    sudo apt-get install -y gh || brew install gh
fi

gh auth login

echo ""
echo "Setting ORACLE_COMPLETION_URL..."
read -p "Enter Oracle completion URL (e.g. https://your-vm:8443/completion): " ORACLE_URL
gh secret set ORACLE_COMPLETION_URL --repo "$REPO" --body "$ORACLE_URL"

echo "Setting ORACLE_COMPLETION_SECRET..."
read -p "Enter the shared completion secret: " ORACLE_SECRET
gh secret set ORACLE_COMPLETION_SECRET --repo "$REPO" --body "$ORACLE_SECRET"

echo ""
echo "=== GitHub secrets configured ==="
