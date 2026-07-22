"""
GitHub Actions Dispatch — asynchronously trigger Kokoro TTS workflows.

This module sends a repository_dispatch event to GitHub Actions with
the TTS session details. It is called AFTER the Telegram callback is
acknowledged, so it never blocks the Telegram response.
"""

import json
import logging
import uuid

import requests

from oracle.config import (
    GITHUB_REPO_OWNER,
    GITHUB_REPO_NAME,
    GITHUB_DISPATCH_TOKEN,
)
from oracle.voice_registry import validate_voice, get_kokoro_lang_code

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


def dispatch_tts_job(session) -> dict:
    """
    Dispatch a Kokoro TTS job to GitHub Actions via repository_dispatch.

    Args:
        session: A Session object with language_id, voice_id, input_text, etc.

    Returns:
        dict with 'request_id' and dispatch status info.

    Raises:
        ValueError: if voice/language validation fails.
        requests.HTTPError: if GitHub API rejects the dispatch.
    """
    # Validate voice before dispatching
    if not validate_voice(session.language_id, session.voice_id):
        raise ValueError(
            f"Voice '{session.voice_id}' is not valid for language '{session.language_id}'"
        )

    lang_code = get_kokoro_lang_code(session.language_id)
    request_id = uuid.uuid4().hex

    payload = {
        "session_id": session.session_id,
        "telegram_user_id": str(session.telegram_user_id),
        "chat_id": str(session.chat_id),
        "input_text": session.input_text,
        "language_id": session.language_id,
        "kokoro_lang_code": lang_code,
        "voice_id": session.voice_id,
        "speed": str(session.speed),
        "request_id": request_id,
    }

    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/dispatches"
    headers = {
        "Authorization": f"token {GITHUB_DISPATCH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    body = {
        "event_type": "kokoro_tts_request",
        "client_payload": payload,
    }

    response = requests.post(url, headers=headers, json=body, timeout=30)

    if response.status_code != 204:
        logger.error(
            "GitHub dispatch failed: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()

    logger.info(
        "Dispatched TTS job: session=%s voice=%s lang=%s request_id=%s",
        session.session_id,
        session.voice_id,
        session.language_id,
        request_id,
    )

    return {"request_id": request_id, "status": "dispatched"}
