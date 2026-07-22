"""
Kokovoicebot — Oracle Control Plane Server.

Persistent Telegram webhook server that handles the TTS flow using
python-telegram-bot v21+ Application-based API with Flask webhook receiver.

ARCHITECTURE (corrected):
  Flask receives the webhook POST → puts the Update into the Application's
  update_queue → the Application's own persistent event loop processes it
  via its registered async handlers. This is the ONLY safe way to bridge
  Flask (sync) with python-telegram-bot v21+ (async Application).

  We NEVER create temporary event loops or call asyncio.run() for Bot
  methods from Flask endpoints. Instead, we use the Application's
  update_queue.put() to schedule work on the Application's loop, and
  asyncio.run_coroutine_threadsafe() for async work from sync threads.

Result-transfer architecture (two-step binary upload):
  Step 1: Small JSON callback to /completion (< 65 KB)
  Step 2: Binary multipart upload to /upload/audio with HMAC one-time-use token

CRITICAL RULE: answer_callback_query is called IMMEDIATELY on every callback
before any slow operation. This prevents the permanent-spinner bug.

Security:
  - Only ALLOWED_TELEGRAM_USER_ID may use the bot (single-user lock)
  - Telegram webhook secret_token validated on every /webhook POST
  - HMAC upload tokens are one-time-use (consumed after first valid upload)
  - Completion endpoint authenticated via X-Completion-Secret header
  - Duplicate completion callbacks rejected by state machine
"""

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from oracle.config import (
    TELEGRAM_BOT_TOKEN,
    ALLOWED_TELEGRAM_USER_ID,
    WEBHOOK_PORT,
    WEBHOOK_URL_BASE,
    ORACLE_COMPLETION_SECRET,
    TELEGRAM_WEBHOOK_SECRET,
    MAX_INPUT_TEXT_LENGTH,
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

# Initialize Flask
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB for audio uploads

# ── Create the telegram-bot Application (v21+ API) ──
# This Application has its OWN persistent event loop that runs in a
# background thread. All async Bot methods run on this loop.
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
store = SessionStore()

# Track consumed upload tokens for one-time-use enforcement
_consumed_tokens = set()  # type: set

# The Application's event loop reference — set during startup
_app_loop = None  # type: asyncio.AbstractEventLoop


# ── HMAC Token Generation and Validation ──

def _generate_upload_token(session_id: str, request_id: str) -> str:
    """HMAC-based upload token for authenticating audio uploads."""
    message = f"{session_id}:{request_id}"
    token = hmac.new(
        ORACLE_COMPLETION_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return token


def _validate_upload_token(session_id: str, request_id: str, token: str) -> bool:
    """
    Validate upload token: HMAC must match AND token must not have been
    consumed already (one-time-use enforcement).
    """
    expected = _generate_upload_token(session_id, request_id)
    if not hmac.compare_digest(expected, token):
        return False
    # One-time-use: reject if already consumed
    if token in _consumed_tokens:
        logger.warning("Rejected replayed upload token for session %s", session_id)
        return False
    return True


def _consume_upload_token(token: str):
    """Mark an upload token as consumed (one-time-use)."""
    _consumed_tokens.add(token)


def _reject_unauthorized_user(user_id: int) -> bool:
    """Returns True if the user is NOT the single allowed user."""
    return user_id != ALLOWED_TELEGRAM_USER_ID


# ── Async helpers (run on Application's event loop) ──

async def _send_unauthorized_reply(chat_id: int):
    """Send rejection to unauthorized users."""
    bot = telegram_app.bot
    await bot.send_message(
        chat_id=chat_id,
        text="⛔ This bot is private and reserved for its owner.",
    )


async def _mark_session_failed(session, error_msg: str):
    """Mark session as failed and update Telegram UI."""
    session.state = "FAILED"
    store.update(session)
    try:
        keyboard = failed_keyboard(session.session_id)
        bot = telegram_app.bot
        await bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.ui_message_id,
            text=f"❌ {error_msg}\n\nPlease try again.",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning("Failed to update UI for failed session: %s", e)


async def _deliver_audio_to_telegram(session, audio_path: Path):
    """Deliver the generated audio file to the user via Telegram."""
    bot = telegram_app.bot
    lang_display = get_language(session.language_id)["display_name"]
    voice_display = get_voice_display_name(session.language_id, session.voice_id)

    with open(audio_path, "rb") as f:
        await bot.send_audio(
            chat_id=session.chat_id,
            audio=f,
            title="Kokoro TTS",
            performer="Kokoro",
            caption=f"Language: {lang_display} | Voice: {voice_display}",
        )

    session.state = "COMPLETED"
    store.update(session)

    try:
        await bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.ui_message_id,
            text=f"✅ Audio delivered!\n\nLanguage: {lang_display}\nVoice: {voice_display}\n\nSend me more text anytime.",
        )
    except Exception:
        pass


# ── Schedule async work on Application's loop ──

def _schedule_async(coro):
    """
    Schedule an async coroutine to run on the Application's event loop.
    
    Uses asyncio.run_coroutine_threadsafe() which is the correct way to
    submit work to a running event loop from a different (sync) thread.
    """
    if _app_loop is None or _app_loop.is_closed():
        logger.error("Application event loop is not available — cannot schedule async work")
        return
    asyncio.run_coroutine_threadsafe(coro, _app_loop)


# ── Telegram handlers (v21+ async — called via Application's event loop) ──

async def start_handler(update: Update, context):
    """Handle /start — show welcome message."""
    user_id = update.effective_user.id
    if _reject_unauthorized_user(user_id):
        await _send_unauthorized_reply(update.effective_chat.id)
        return

    await update.message.reply_text(
        "Welcome to Kokoro Voice! 🎙️\n\nSend me text and I will convert it to speech.",
    )


async def text_handler(update: Update, context):
    """Handle incoming text — create a session and show language selection."""
    user_id = update.effective_user.id
    if _reject_unauthorized_user(user_id):
        await _send_unauthorized_reply(update.effective_chat.id)
        return

    input_text = update.message.text
    if not input_text or len(input_text.strip()) == 0:
        await update.message.reply_text("Please send some text to convert to speech.")
        return

    if len(input_text) > MAX_INPUT_TEXT_LENGTH:
        await update.message.reply_text(
            f"⚠️ Text too long ({len(input_text)} chars). Maximum is {MAX_INPUT_TEXT_LENGTH} characters. "
            f"Please send a shorter message."
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
    bot = context.bot
    keyboard = language_keyboard(session.session_id)
    sent = await bot.send_message(
        chat_id=chat_id,
        text="Choose the language/accent for your voice:",
        reply_markup=keyboard,
    )
    session.ui_message_id = sent.message_id
    store.update(session)


async def callback_handler(update: Update, context):
    """
    Handle all inline keyboard callbacks.
    
    CRITICAL: answer_callback_query is called IMMEDIATELY before any slow work.
    """
    query = update.callback_query
    user_id = query.from_user.id

    # ── IMMEDIATE callback acknowledgement ──
    # This MUST happen before ANY other work to prevent the spinner bug.
    await query.answer()

    if _reject_unauthorized_user(user_id):
        logger.warning("Unauthorized callback from user %s", user_id)
        return

    # Parse callback data: "tts:action:detail:session_id"
    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != "tts":
        logger.warning("Malformed callback data: %s", query.data)
        return

    action = parts[1]
    session_id = parts[-1] if len(parts) >= 4 else parts[2]

    session = store.get(session_id)
    if session is None:
        await query.edit_message_text("⚠️ This session has expired. Please send your text again.")
        return

    if not session.is_owned_by(user_id):
        logger.warning("Session ownership mismatch: user=%s owner=%s", user_id, session.telegram_user_id)
        return

    # ── Handle actions AFTER answer_callback_query ──

    if action == "lang":
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
        await query.edit_message_text(
            text=f"Choose a voice for {lang['display_name']}:",
            reply_markup=keyboard,
        )

    elif action == "voice":
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
        await query.edit_message_text(
            text=f"Ready to generate your audio.\n\nLanguage: {lang_display}\nVoice: {voice_display}\n\nTap Generate to continue.",
            reply_markup=keyboard,
        )

    elif action == "page":
        page_num = int(parts[2]) if len(parts) >= 4 else 0
        session.voice_page = page_num
        store.update(session)

        keyboard = voice_keyboard(session_id, session.language_id, page_num)
        lang_display = get_language(session.language_id)["display_name"]
        await query.edit_message_text(
            text=f"Choose a voice for {lang_display} (page {page_num + 1}):",
            reply_markup=keyboard,
        )

    elif action == "back":
        target = parts[2]
        if target == "languages":
            session.state = "LANGUAGE_SELECTION"
            session.language_id = ""
            session.voice_id = ""
            session.voice_page = 0
            store.update(session)
            keyboard = language_keyboard(session_id)
            await query.edit_message_text(
                text="Choose the language/accent for your voice:",
                reply_markup=keyboard,
            )
        elif target == "voice_selection":
            session.state = "VOICE_SELECTION"
            session.voice_id = ""
            store.update(session)
            keyboard = voice_keyboard(session_id, session.language_id, 0)
            lang_display = get_language(session.language_id)["display_name"]
            await query.edit_message_text(
                text=f"Choose a voice for {lang_display}:",
                reply_markup=keyboard,
            )

    elif action == "generate":
        session.state = "GENERATING"
        store.update(session)

        lang_display = get_language(session.language_id)["display_name"]
        voice_display = get_voice_display_name(session.language_id, session.voice_id)

        keyboard = generating_keyboard(session_id)
        await query.edit_message_text(
            text=f"⏳ Generating your audio...\n\nLanguage: {lang_display}\nVoice: {voice_display}",
            reply_markup=keyboard,
        )

        # Dispatch GitHub Actions in a background thread.
        # The thread is sync; when it needs to call async Bot methods
        # (e.g., _mark_session_failed), it uses _schedule_async() to
        # submit the coroutine to the Application's running event loop.
        def _dispatch_and_handle():
            try:
                result = dispatch_tts_job(session)
                session.request_id = result["request_id"]
                store.update(session)
                logger.info("GitHub dispatch succeeded: request_id=%s", result["request_id"])
            except Exception as e:
                logger.error("GitHub dispatch failed: %s", e)
                _schedule_async(_mark_session_failed(session, "Could not dispatch to GitHub Actions."))

        thread = threading.Thread(target=_dispatch_and_handle, daemon=True)
        thread.start()

    elif action == "cancel":
        session.state = "CANCELLED"
        store.update(session)
        store.delete(session_id)
        await query.edit_message_text(text="❌ Cancelled. Send me text again to start a new conversion.")

    elif action == "retry":
        session.state = "LANGUAGE_SELECTION"
        session.language_id = ""
        session.voice_id = ""
        session.voice_page = 0
        store.update(session)
        keyboard = language_keyboard(session_id)
        await query.edit_message_text(
            text=f"Choose the language/accent for your voice:\n\nOriginal text: \"{session.input_text[:100]}\"",
            reply_markup=keyboard,
        )


# ── Register handlers ──
telegram_app.add_handler(CommandHandler("start", start_handler))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))


# ── Flask endpoints (sync — bridge to Application via update_queue or _schedule_async) ──

@app.route("/webhook", methods=["POST"])
def webhook_endpoint():
    """Receive Telegram webhook updates and process them via Application's queue."""
    # Validate Telegram webhook secret token
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret_header != TELEGRAM_WEBHOOK_SECRET:
        logger.warning("Webhook: invalid secret_token header")
        return jsonify({"status": "rejected"}), 403

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        if update is None:
            return jsonify({"status": "invalid_update"}), 400

        # Put the update into the Application's update_queue.
        # The Application's own event loop will process it via the
        # registered handlers. This is the CORRECT bridge pattern for
        # python-telegram-bot v21+ with external webhook servers.
        telegram_app.update_queue.put_nowait(update)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error("Webhook processing error: %s", e)
        return jsonify({"status": "error", "message": str(e)[:200]}), 200


@app.route("/completion", methods=["POST"])
def completion_endpoint():
    """Step 1: Small JSON callback from GitHub Actions."""
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

    # Reject if session has already moved past GENERATING state
    # (prevents duplicate completion callbacks)
    if session.state in ("COMPLETED", "DELIVERING", "UPLOAD_PENDING"):
        logger.info("Duplicate completion for session %s in state %s — ignoring", session_id, session.state)
        return jsonify({"status": "already_completed"}), 200

    # Session must be in GENERATING state to accept completion
    if session.state != "GENERATING":
        logger.warning("Completion for session %s in unexpected state %s", session_id, session.state)
        return jsonify({"error": "unexpected_session_state"}), 400

    if status == "success":
        upload_token = _generate_upload_token(session_id, request_id)
        session.request_id = request_id
        # Transition to UPLOAD_PENDING — prevents duplicate completions
        session.state = "UPLOAD_PENDING"
        store.update(session)
        logger.info("Completion callback: success, upload_token for session %s", session_id)
        return jsonify({
            "status": "upload_ready",
            "upload_url": f"{WEBHOOK_URL_BASE}/upload/audio",
            "upload_token": upload_token,
        }), 200

    elif status == "failure":
        error_msg = data.get("error_message", "Unknown error")
        _schedule_async(_mark_session_failed(session, f"Generation failed: {error_msg}"))
        return jsonify({"status": "failure_acknowledged"}), 200

    return jsonify({"error": "unknown status"}), 400


@app.route("/upload/audio", methods=["POST"])
def upload_audio_endpoint():
    """Step 2: Binary audio upload from GitHub Actions."""
    session_id = request.form.get("session_id")
    request_id = request.form.get("request_id")
    upload_token = request.form.get("upload_token")

    if not session_id or not request_id or not upload_token:
        return jsonify({"error": "missing session_id, request_id, or upload_token"}), 400

    if not _validate_upload_token(session_id, request_id, upload_token):
        logger.warning("Upload endpoint: invalid or replayed upload_token")
        return jsonify({"error": "invalid or replayed upload_token"}), 403

    # Mark token as consumed IMMEDIATELY (one-time-use)
    _consume_upload_token(upload_token)

    session = store.get(session_id)
    if session is None:
        return jsonify({"error": "session not found or expired"}), 404

    if session.state not in ("UPLOAD_PENDING", "GENERATING"):
        logger.info("Upload for session %s in state %s — already handled", session_id, session.state)
        return jsonify({"status": "already_completed"}), 200

    audio_file = request.files.get("audio_file")
    if audio_file is None:
        return jsonify({"error": "missing audio_file"}), 400

    audio_path = Path(f"/tmp/kokoro_{session_id}.wav")
    audio_file.save(audio_path)

    if audio_path.stat().st_size == 0:
        audio_path.unlink()
        _schedule_async(_mark_session_failed(session, "Generated audio file was empty."))
        return jsonify({"error": "empty audio file"}), 400

    session.state = "DELIVERING"
    store.update(session)

    try:
        # Schedule audio delivery on the Application's event loop and wait for result
        future = asyncio.run_coroutine_threadsafe(
            _deliver_audio_to_telegram(session, audio_path),
            _app_loop,
        )
        # Wait for delivery to complete (with timeout)
        future.result(timeout=60)
        logger.info("Audio delivered to Telegram for session %s", session_id)
        return jsonify({"status": "delivered"}), 200

    except Exception as e:
        logger.error("Failed to deliver audio: %s", e)
        _schedule_async(_mark_session_failed(session, "Generation succeeded but delivery failed."))
        return jsonify({"error": "delivery failed"}), 500

    finally:
        if audio_path.exists():
            audio_path.unlink()


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "bot": "kokovoicebot"}), 200


def main():
    """Start the webhook server."""
    logger.info("Starting Kokovoicebot Oracle Control Plane...")
    logger.info("Allowed Telegram user ID: %s", ALLOWED_TELEGRAM_USER_ID)

    # ── Start Application's event loop in a background thread ──
    # The Application needs a persistent running event loop for all async work.
    # We initialize and start it in a dedicated thread, then capture the loop
    # reference for use by _schedule_async() and run_coroutine_threadsafe().
    def _run_app():
        global _app_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _app_loop = loop

        # Initialize and start the Application on this loop
        loop.run_until_complete(telegram_app.initialize())
        loop.run_until_complete(telegram_app.start())
        logger.info("Telegram Application initialized and started on background loop")

        # Set the Telegram webhook
        webhook_url = f"{WEBHOOK_URL_BASE}/webhook"
        loop.run_until_complete(
            telegram_app.bot.set_webhook(
                url=webhook_url,
                secret_token=TELEGRAM_WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
        )
        logger.info("Webhook set to %s", webhook_url)

        # Keep the loop running forever — all async Bot methods run here
        loop.run_forever()

    app_thread = threading.Thread(target=_run_app, daemon=True)
    app_thread.start()

    # Wait for Application to be ready
    time.sleep(3)

    if _app_loop is None:
        logger.error("Application event loop failed to start — aborting")
        return

    # Start Flask server (sync — receives webhooks, puts updates into queue)
    app.run(
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        ssl_context=None,  # nginx handles TLS
        debug=False,
    )


if __name__ == "__main__":
    main()
