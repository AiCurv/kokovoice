"""
GitHub Actions Dispatch — trigger Kokoro TTS workflows via repository_dispatch.

This module sends a repository_dispatch event to GitHub Actions with
the TTS session details, including text chunks for long input.

CRITICAL: GitHub repository_dispatch client_payload is limited to 10 properties.
We combine text-related fields (input_text, chunks, total_chunks) into a single
`text_data` JSON-encoded string to stay within the limit.

Payload structure (9 properties — within the 10-property limit):
  - session_id: unique session identifier
  - text_data: JSON string containing {input_text, chunks, total_chunks}
  - language_id: language identifier (e.g., "us_en")
  - kokoro_lang_code: Kokoro pipeline lang code (e.g., "a")
  - voice_id: voice identifier (e.g., "af_heart")
  - speed: speech speed factor (e.g., "1.0")
  - request_id: unique request identifier
  - telegram_user_id: Telegram user ID (for reference)
  - chat_id: Telegram chat ID (for reference)

Dispatch failure → session transitions to FAILED (not stuck in GENERATING).
Dispatch success → session transitions from DISPATCHING to GENERATING.
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
from oracle.text_chunker import chunk_text

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


def dispatch_tts_job(session, chunks: list = None) -> dict:
    """
    Dispatch a Kokoro TTS job to GitHub Actions via repository_dispatch.

    Args:
        session: A Session object with language_id, voice_id, input_text, etc.
        chunks: List of text chunks (if None, auto-chunk from session.input_text)

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

    # Auto-chunk if chunks not provided
    if chunks is None:
        chunks = chunk_text(session.input_text)

    total_chunks = len(chunks)

    # Combine text-related fields into one JSON string to stay within
    # GitHub's 10-property client_payload limit
    text_data = json.dumps({
        "input_text": session.input_text,
        "chunks": chunks,
        "total_chunks": total_chunks,
    })

    # Payload: 9 properties (within the 10-property GitHub limit)
    payload = {
        "session_id": session.session_id,
        "text_data": text_data,
        "language_id": session.language_id,
        "kokoro_lang_code": lang_code,
        "voice_id": session.voice_id,
        "speed": str(session.speed),
        "request_id": request_id,
        "telegram_user_id": str(session.telegram_user_id),
        "chat_id": str(session.chat_id),
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

    logger.info(
        "Dispatching TTS job: session=%s voice=%s lang=%s request_id=%s chunks=%d",
        session.session_id, session.voice_id, session.language_id, request_id, total_chunks,
    )

    response = requests.post(url, headers=headers, json=body, timeout=30)

    if response.status_code != 204:
        logger.error(
            "GitHub dispatch failed: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()

    logger.info(
        "Dispatched TTS job successfully: request_id=%s",
        request_id,
    )

    return {"request_id": request_id, "status": "dispatched", "total_chunks": total_chunks}
