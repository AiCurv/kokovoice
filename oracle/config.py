"""
Oracle Control Plane Configuration.

All secrets are loaded from environment variables or a protected .env file.
NEVER commit actual values to source code.
"""

import os
import secrets
from pathlib import Path

# Load .env file if present (for development; production uses systemd Environment=)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

# Telegram
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_TELEGRAM_USER_ID = int(os.environ["ALLOWED_TELEGRAM_USER_ID"])

# Telegram webhook secret — validated on every webhook POST from Telegram
# If not set in env, generate a random one at startup
TELEGRAM_WEBHOOK_SECRET = os.environ.get(
    "TELEGRAM_WEBHOOK_SECRET",
    secrets.token_urlsafe(32),
)

# GitHub Actions
GITHUB_REPO_OWNER = os.environ["GITHUB_REPOSITORY_OWNER"]
GITHUB_REPO_NAME = os.environ["GITHUB_REPOSITORY_NAME"]
GITHUB_DISPATCH_TOKEN = os.environ["GITHUB_DISPATCH_TOKEN"]

# Completion callback security
ORACLE_COMPLETION_SECRET = os.environ["ORACLE_COMPLETION_SECRET"]

# Server
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8443"))
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "https://localhost:8443")

# Session settings
SESSION_EXPIRY_SECONDS = 3600  # 1 hour
VOICE_PAGE_SIZE = 5  # voices per page in Telegram keyboard

# Input text limits
MAX_INPUT_TEXT_LENGTH = 500  # Maximum characters for TTS input

# Kokoro model
KOKORO_MODEL_ID = "hexgrad/Kokoro-82M"
