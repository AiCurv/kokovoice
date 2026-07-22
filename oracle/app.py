"""
Kokovoicebot — Oracle Control Plane Server.

Persistent Telegram webhook server that handles the TTS flow:
  1. Receive text → show language selection
  2. Language selected → show voice selection (paginated)
  3. Voice selected → show confirmation
  4. Generate tapped → acknowledge callback, update UI, dispatch GitHub Actions
  5. GitHub Actions completes → small JSON callback with status
  6. GitHub Actions uploads binary audio → Oracle sends audio to Telegram

Result-transfer architecture:
  GitHub Actions cannot carry large payloads in repository_dispatch
  (client_payload has a ~65 KB hard limit). Even the shortest TTS audio
  (3 seconds, 208 KB base64) exceeds this limit. Therefore, audio delivery
  uses a two-step approach:
    Step 1: Small JSON callback to /completion (session_id, request_id,
            status, upload_token — all well under 65 KB)
    Step 2: Binary multipart POST to /upload/audio with the WAV file
            authenticated by the HMAC upload_token from Step 1

CRITICAL RULE: answerCallbackQuery is called IMMEDIATELY on every callback
before any slow operation. This prevents the permanent-spinner bug.

Security: Only ALLOWED_TELEGRAM_USER_ID may use the bot. All other users
are rejected on every message and callback. This is single-user only.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackQueryHandler

from oracle.config import (
    TELEGRAM_BOT_TOKEN,
    ALLOWED_TELEGRAM_USER_ID,
    WEBHOOK_PORT,
    WEBHOOK_URL_BASE,
    ORACLE_COMPLETION_SECRET,
)
from oracle.session_store import SessionStore
from oracle.telegram_ui import (
    language_keyboard,
    voice_keyboard,
    confirmation_keyboard,
    generating_keyboard,
    failed_keyboard,
)
from oracle.voice_registry import get_language, get_kokoro_lang_code, validate_voice, get_voice_display_name
from oracle.github_dispatch import dispatch_tts_job

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize components
app = Flask(__name__)
# Allow up to 50 MB for audio upload (Telegram sendAudio limit)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

store = SessionStore()

# Set up Telegram Bot dispatcher (no Updater — we use webhook mode)
from telegram import Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=0)  # synchronous dispatcher


def _generate_upload_token(session_id: str, request_id: str) -> str:
    """
    Generate a one-time HMAC upload token for authenticating audio uploads.

    The token is HMAC(session_id + request_id, ORACLE_COMPLETION_SECRET).
    This ensures only GitHub Actions (which received the token in the
    small callback payload) can upload audio for a specific session.
    """
    message = f"{session_id}:{request_id}"
    token = hmac.new(
        ORACLE_COMPLETION_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return token


def _validate_upload_token(session_id: str, request_id: str, token: str) -> bool:
    """Validate that the upload token matches the expected HMAC."""
    expected = _generate_upload_token(session_id, request_id)
    return hmac.compare_digest(expected, token)


def _reject_unauthorized_user(user_id: int) -> bool:
    """Check if user is the single allowed user. Returns True if REJECTED."""
    return user_id != ALLOWED_TELEGRAM_USER_ID


def _send_unauthorized_reply(chat_id: int):
    """Send a polite rejection to unauthorized users."""
    bot.send_message(
        chat_id=chat_id,
        text="⛔ This bot is private and reserved for its owner. You are not authorized to use it.",
    )


def _deliver_audio_to_telegram(session, audio_path: Path):
    """Send the audio file to Telegram and update the UI."""
    lang_display = get_language(session.language_id)["display_name"]
    voice_display = get_voice_display_name(session.language_id, session.voice_id)

    with open(audio_path, "rb") as audio_file:
        bot.send_audio(
            chat_id=session.chat_id,
            audio=audio_file,
            title="Kokoro TTS",
            performer="Kokoro",
            caption=f"Language: {lang_display} | Voice: {voice_display}",
        )

    session.state = "COMPLETED"
    store.update(session)

    # Update the UI message
    try:
        bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.ui_message_id,
            text=f"✅ Audio delivered!\n\nLanguage: {lang_display}\nVoice: {voice_display}\n\nSend me more text anytime.",
            reply_markup=None,
        )
    except Exception:
        pass


def _mark_session_failed(session, error_msg: str):
    """Mark session as failed and update Telegram UI."""
    session.state = "FAILED"
    store.update(session)
    try:
        keyboard = failed_keyboard(session.session_id)
        bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.ui_message_id,
            text=f"❌ {error_msg}\n\nPlease try again.",
            reply_markup=keyboard,
        )
    except Exception:
        pass


# ── /start command ──
def start_handler(update: Update, context):
    """Handle /start — show welcome message."""
    user_id = update.effective_user.id
    if _reject_unauthorized_user(user_id):
        _send_unauthorized_reply(update.effective_chat.id)
        return

    bot.send_message(
        chat_id=update.effective_chat.id,
        text="Welcome to Kokoro Voice! 🎙️\n\nSend me text and I will convert it to speech.",
    )


# ── Text message handler ──
def text_handler(update: Update, context):
    """Handle incoming text — create a session and show language selection."""
    user_id = update.effective_user.id
    if _reject_unauthorized_user(user_id):
        _send_unauthorized_reply(update.effective_chat.id)
        return

    input_text = update.message.text
    if not input_text or len(input_text.strip()) == 0:
        bot.send_message(
            chat_id=update.effective_chat.id,
            text="Please send some text to convert to speech.",
        )
        return

    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    # Delete any previous active session for this user
    old_session = store.get_by_user(user_id)
    if old_session:
        store.delete(old_session.session_id)

    # Create new session
    session = store.create(user_id, chat_id, message_id, input_text.strip())
    session.state = "LANGUAGE_SELECTION"
    store.update(session)

    # Send language selection UI
    keyboard = language_keyboard(session.session_id)
    sent = bot.send_message(
        chat_id=chat_id,
        text="Choose the language/accent for your voice:",
        reply_markup=keyboard,
    )
    session.ui_message_id = sent.message_id
    store.update(session)


# ── Callback query handler ──
def callback_handler(update: Update, context):
    """
    Handle all inline keyboard callbacks.

    CRITICAL: answerCallbackQuery is called IMMEDIATELY before any slow work.
    This is the fix for the permanent-spinner bug.
    """
    query = update.callback_query
    user_id = query.from_user.id

    # ── IMMEDIATE callback acknowledgement ──
    # This MUST happen before any slow operation.
    query.answer()

    if _reject_unauthorized_user(user_id):
        # Already acknowledged the callback; just don't do anything else.
        logger.warning("Unauthorized callback from user %s", user_id)
        return

    # Parse callback data: "tts:action:detail:session_id"
    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != "tts":
        logger.warning("Malformed callback data: %s", query.data)
        return

    action = parts[1]
    session_id = parts[-1] if len(parts) >= 4 else parts[2]

    # Retrieve session
    session = store.get(session_id)
    if session is None:
        query.edit_message_text("⚠️ This session has expired. Please send your text again.")
        return

    # Validate session ownership
    if not session.is_owned_by(user_id):
        logger.warning("Session ownership mismatch: user=%s session_owner=%s", user_id, session.telegram_user_id)
        return

    # ── Handle actions ──
    # AFTER answerCallbackQuery, we can safely do slow work.

    if action == "lang":
        # Language selected
        lang_id = parts[2] if len(parts) >= 4 else None
        if lang_id is None:
            return
        lang = get_language(lang_id)
        if lang is None:
            return

        session.language_id = lang_id
        session.kokoro_lang_code = lang["lang_code"]
        session.voice_id = ""
        session.voice_page = 0
        session.state = "VOICE_SELECTION"
        store.update(session)

        keyboard = voice_keyboard(session_id, lang_id, 0)
        query.edit_message_text(
            text=f"Choose a voice for {lang['display_name']}:",
            reply_markup=keyboard,
        )

    elif action == "voice":
        # Voice selected
        voice_id = parts[2] if len(parts) >= 4 else None
        if voice_id is None:
            return
        if not validate_voice(session.language_id, voice_id):
            logger.warning("Invalid voice %s for language %s", voice_id, session.language_id)
            return

        session.voice_id = voice_id
        session.state = "CONFIRMATION"
        store.update(session)

        lang_display = get_language(session.language_id)["display_name"]
        voice_display = get_voice_display_name(session.language_id, voice_id)

        keyboard = confirmation_keyboard(session_id)
        query.edit_message_text(
            text=f"Ready to generate your audio.\n\nLanguage: {lang_display}\nVoice: {voice_display}\n\nTap Generate to continue.",
            reply_markup=keyboard,
        )

    elif action == "page":
        # Voice pagination
        page_num = int(parts[2]) if len(parts) >= 4 else 0
        session.voice_page = page_num
        store.update(session)

        keyboard = voice_keyboard(session_id, session.language_id, page_num)
        lang_display = get_language(session.language_id)["display_name"]
        query.edit_message_text(
            text=f"Choose a voice for {lang_display} (page {page_num + 1}):",
            reply_markup=keyboard,
        )

    elif action == "back":
        # Navigation: back to languages or voice selection
        target = parts[2]
        if target == "languages":
            session.state = "LANGUAGE_SELECTION"
            session.language_id = ""
            session.voice_id = ""
            session.voice_page = 0
            store.update(session)
            keyboard = language_keyboard(session_id)
            query.edit_message_text(
                text="Choose the language/accent for your voice:",
                reply_markup=keyboard,
            )
        elif target == "voice_selection":
            session.state = "VOICE_SELECTION"
            session.voice_id = ""
            store.update(session)
            keyboard = voice_keyboard(session_id, session.language_id, 0)
            lang_display = get_language(session.language_id)["display_name"]
            query.edit_message_text(
                text=f"Choose a voice for {lang_display}:",
                reply_markup=keyboard,
            )

    elif action == "generate":
        # ── GENERATE ──
        # Callback already acknowledged above. Now update UI and dispatch.
        session.state = "GENERATING"
        store.update(session)

        lang_display = get_language(session.language_id)["display_name"]
        voice_display = get_voice_display_name(session.language_id, session.voice_id)

        keyboard = generating_keyboard(session_id)
        query.edit_message_text(
            text=f"⏳ Generating your audio...\n\nLanguage: {lang_display}\nVoice: {voice_display}",
            reply_markup=keyboard,
        )

        # Dispatch GitHub Actions asynchronously in a background thread
        def _dispatch_and_handle():
            try:
                result = dispatch_tts_job(session)
                session.request_id = result["request_id"]
                store.update(session)
                logger.info("GitHub dispatch succeeded: request_id=%s", result["request_id"])
            except Exception as e:
                logger.error("GitHub dispatch failed: %s", e)
                _mark_session_failed(session, "Could not dispatch to GitHub Actions.")

        thread = threading.Thread(target=_dispatch_and_handle, daemon=True)
        thread.start()

    elif action == "cancel":
        # Cancel the session
        session.state = "CANCELLED"
        store.update(session)
        store.delete(session_id)
        query.edit_message_text(text="❌ Cancelled. Send me text again to start a new conversion.")

    elif action == "retry":
        # Retry — go back to language selection with existing text
        session.state = "LANGUAGE_SELECTION"
        session.language_id = ""
        session.voice_id = ""
        session.voice_page = 0
        store.update(session)
        keyboard = language_keyboard(session_id)
        query.edit_message_text(
            text=f"Choose the language/accent for your voice:\n\nOriginal text: \"{session.input_text[:100]}\"",
            reply_markup=keyboard,
        )


# ── Register handlers ──
dispatcher.add_handler(CommandHandler("start", start_handler))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, text_handler))
dispatcher.add_handler(CallbackQueryHandler(callback_handler))


# ── Completion callback endpoint (Step 1: small JSON metadata) ──
@app.route("/completion", methods=["POST"])
def completion_endpoint():
    """
    Receive completion status callback from GitHub Actions.

    This endpoint receives a SMALL JSON payload (well under the 65 KB
    GitHub Actions client_payload limit) containing:
      - session_id, request_id
      - status: "success" or "failure"
      - upload_token: HMAC token for authenticating the subsequent
        binary audio upload (only present on success)

    For success, GitHub Actions will then POST the actual audio binary
    to /upload/audio using the upload_token.

    For failure, the error_message is included and no audio upload follows.

    Authentication: X-Completion-Secret header must match ORACLE_COMPLETION_SECRET.
    """
    # Authenticate
    auth_header = request.headers.get("X-Completion-Secret", "")
    if auth_header != ORACLE_COMPLETION_SECRET:
        logger.warning("Completion endpoint: invalid secret")
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    session_id = data.get("session_id")
    request_id = data.get("request_id")
    status = data.get("status")

    if not session_id:
        return jsonify({"error": "missing session_id"}), 400

    session = store.get(session_id)
    if session is None:
        return jsonify({"error": "session not found or expired"}), 404

    # Idempotency check
    if session.state in ("COMPLETED", "DELIVERING"):
        return jsonify({"status": "already_completed"}), 200

    if status == "success":
        # Generate upload token for the subsequent binary upload
        upload_token = _generate_upload_token(session_id, request_id)
        session.state = "GENERATING"  # Still generating until audio arrives
        session.request_id = request_id
        store.update(session)

        logger.info("Completion callback received: success, upload_token generated for session %s", session_id)

        # Return the upload token — GitHub Actions uses this for the binary upload
        return jsonify({
            "status": "upload_ready",
            "upload_url": f"{WEBHOOK_URL_BASE}/upload/audio",
            "upload_token": upload_token,
        }), 200

    elif status == "failure":
        error_msg = data.get("error_message", "Unknown error")
        _mark_session_failed(session, f"Generation failed: {error_msg}")
        return jsonify({"status": "failure_acknowledged"}), 200

    return jsonify({"error": "unknown status"}), 400


# ── Audio upload endpoint (Step 2: binary file upload) ──
@app.route("/upload/audio", methods=["POST"])
def upload_audio_endpoint():
    """
    Receive binary audio file upload from GitHub Actions.

    GitHub Actions sends the WAV file as multipart/form-data after
    receiving the upload_token from the completion callback.

    Authentication: upload_token must match the HMAC generated for the session.
    This prevents unauthorized uploads even if the endpoint URL is known.

    Payload (multipart/form-data):
      - request_id: string (form field)
      - session_id: string (form field)
      - upload_token: string (form field, HMAC-authenticated)
      - audio_file: WAV file (file upload)
    """
    # Get form fields
    session_id = request.form.get("session_id")
    request_id = request.form.get("request_id")
    upload_token = request.form.get("upload_token")

    if not session_id or not request_id or not upload_token:
        return jsonify({"error": "missing session_id, request_id, or upload_token"}), 400

    # Validate upload token
    if not _validate_upload_token(session_id, request_id, upload_token):
        logger.warning("Upload endpoint: invalid upload_token for session %s", session_id)
        return jsonify({"error": "invalid upload_token"}), 403

    # Retrieve session
    session = store.get(session_id)
    if session is None:
        return jsonify({"error": "session not found or expired"}), 404

    # Idempotency check
    if session.state in ("COMPLETED", "DELIVERING"):
        return jsonify({"status": "already_completed"}), 200

    # Get the audio file
    audio_file = request.files.get("audio_file")
    if audio_file is None:
        return jsonify({"error": "missing audio_file"}), 400

    # Save temporarily
    audio_path = Path(f"/tmp/kokoro_{session_id}.wav")
    audio_file.save(audio_path)

    # Validate file is non-empty
    if audio_path.stat().st_size == 0:
        audio_path.unlink()
        _mark_session_failed(session, "Generated audio file was empty.")
        return jsonify({"error": "empty audio file"}), 400

    # Deliver audio to Telegram
    session.state = "DELIVERING"
    store.update(session)

    try:
        _deliver_audio_to_telegram(session, audio_path)
        logger.info("Audio delivered to Telegram for session %s", session_id)
        return jsonify({"status": "delivered"}), 200
    except Exception as e:
        logger.error("Failed to deliver audio to Telegram: %s", e)
        _mark_session_failed(session, "Generation succeeded but delivery failed.")
        return jsonify({"error": "delivery failed"}), 500
    finally:
        # Clean up temp file
        if audio_path.exists():
            audio_path.unlink()


# ── Webhook endpoint ──
@app.route("/webhook", methods=["POST"])
def webhook_endpoint():
    """Receive Telegram webhook updates."""
    update = Update.de_json(request.get_json(force=True), bot)
    # Process update synchronously in the dispatcher
    dispatcher.process_update(update)
    return jsonify({"status": "ok"}), 200


# ── Health check ──
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "bot": "kokovoicebot"}), 200


# ── Main ──
def main():
    """Start the webhook server and set the Telegram webhook."""
    logger.info("Starting Kokovoicebot Oracle Control Plane...")
    logger.info("Allowed Telegram user ID: %s", ALLOWED_TELEGRAM_USER_ID)

    # Set webhook
    webhook_url = f"{WEBHOOK_URL_BASE}/webhook"
    result = bot.set_webhook(url=webhook_url)
    if not result:
        logger.error("Failed to set Telegram webhook to %s", webhook_url)
    else:
        logger.info("Telegram webhook set to %s", webhook_url)

    # Start Flask server
    app.run(
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        ssl_context=None,  # Use nginx reverse proxy for TLS
        debug=False,
    )


if __name__ == "__main__":
    main()
