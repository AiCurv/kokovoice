"""
Telegram UI Builder — stateful inline-keyboard navigation.

This module renders Telegram inline keyboards for the Kokovoicebot
flow: language selection → voice selection (paginated) → confirmation →
generating → completed / failed.

CRITICAL: Every callback_query MUST be acknowledged immediately via
answerCallbackQuery BEFORE any slow work (GitHub dispatch, etc.).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from oracle.config import VOICE_PAGE_SIZE
from oracle.voice_registry import (
    get_languages,
    get_voice_display_name,
    get_voice_page,
)
from oracle.session_store import Session

logger = logging.getLogger(__name__)


def welcome_keyboard() -> InlineKeyboardMarkup:
    """No buttons on the welcome screen — user just sends text."""
    return InlineKeyboardMarkup([])


def language_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Language selection keyboard: American English / British English."""
    languages = get_languages()
    buttons = []
    for lang in languages:
        buttons.append([
            InlineKeyboardButton(
                lang["display_name"],
                callback_data=f"tts:lang:{lang['id']}:{session_id}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


def voice_keyboard(session_id: str, lang_id: str, page: int) -> InlineKeyboardMarkup:
    """
    Paginated voice-selection keyboard.

    Each row shows one voice. Below the voices, pagination controls
    (Previous / Next / Back to language selection).
    """
    page_data = get_voice_page(lang_id, page, VOICE_PAGE_SIZE)
    buttons = []

    # Voice buttons — one per row for readability
    for voice_id, vdata in page_data["voices"]:
        display = get_voice_display_name(lang_id, voice_id)
        buttons.append([
            InlineKeyboardButton(
                display,
                callback_data=f"tts:voice:{voice_id}:{session_id}",
            )
        ])

    # Pagination controls
    nav_row = []
    if page_data["has_prev"]:
        nav_row.append(
            InlineKeyboardButton(
                "◀ Previous",
                callback_data=f"tts:page:voices:{page - 1}:{session_id}",
            )
        )
    if page_data["has_next"]:
        nav_row.append(
            InlineKeyboardButton(
                "Next ▶",
                callback_data=f"tts:page:voices:{page + 1}:{session_id}",
            )
        )
    if nav_row:
        buttons.append(nav_row)

    # Back to language selection
    buttons.append([
        InlineKeyboardButton(
            "🌐 Back to language selection",
            callback_data=f"tts:back:languages:{session_id}",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def confirmation_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Confirmation screen keyboard: Generate / Change Voice / Change Language / Cancel."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙️ Generate", callback_data=f"tts:generate:{session_id}")],
        [InlineKeyboardButton("🔄 Change Voice", callback_data=f"tts:back:voice_selection:{session_id}")],
        [InlineKeyboardButton("🌐 Change Language", callback_data=f"tts:back:languages:{session_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"tts:cancel:{session_id}")],
    ])


def generating_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Generating screen — only Cancel is available."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"tts:cancel:{session_id}")],
    ])


def failed_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Failed screen — Try Again."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again", callback_data=f"tts:retry:{session_id}")],
    ])
