"""
Voice Registry — single source of truth for supported voices.

Loads from the pinned voices.json and validates all voice IDs
against Kokoro's official registry. This module is used by both
the Telegram UI and the GitHub Actions workflow to ensure
consistent voice identifiers.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from oracle.config import KOKORO_MODEL_ID

_VOICES_PATH = Path(__file__).parent.parent / "kokoro" / "voices.json"
_registry: Optional[dict] = None


def _load_registry() -> dict:
    """Load and cache the voice registry from voices.json."""
    global _registry
    if _registry is None:
        with open(_VOICES_PATH) as f:
            _registry = json.load(f)
    return _registry


def get_languages() -> List[dict]:
    """Return supported languages for Telegram UI."""
    reg = _load_registry()
    result = []
    for lang_id, lang_data in reg["supported_languages"].items():
        result.append({
            "id": lang_id,
            "display_name": lang_data["display_name"],
            "kokoro_lang_code": lang_data["lang_code"],
        })
    return result


def get_language(lang_id: str) -> Optional[dict]:
    """Get a specific language's metadata."""
    reg = _load_registry()
    return reg["supported_languages"].get(lang_id)


def get_voices(lang_id: str) -> Dict[str, dict]:
    """Return all voices for a given language."""
    lang = get_language(lang_id)
    if lang is None:
        return {}
    return lang["voices"]


def validate_voice(lang_id: str, voice_id: str) -> bool:
    """Validate that a voice ID belongs to the specified language."""
    voices = get_voices(lang_id)
    return voice_id in voices


def get_voice_display_name(lang_id: str, voice_id: str) -> str:
    """Get a human-readable display name for a voice."""
    voices = get_voices(lang_id)
    if voice_id in voices:
        v = voices[voice_id]
        gender_icon = "🚺" if v["gender"] == "female" else "🚹"
        traits_str = " ".join(v.get("traits", []))
        grade_str = v.get("grade", "")
        return f"{v['name']} {gender_icon} {traits_str} [{grade_str}]"
    return voice_id


def get_voice_page(lang_id: str, page: int, page_size: int) -> dict:
    """
    Return a paginated subset of voices for Telegram inline keyboard.

    Returns:
        dict with 'voices' (ordered list of voice entries),
        'page' (current page number),
        'total_pages' (total page count),
        'has_next', 'has_prev' booleans.
    """
    voices = get_voices(lang_id)
    # Sort by grade (higher grade first) then name
    grade_order = {"A": 0, "A-": 1, "B": 2, "B-": 3, "C+": 4, "C": 5, "C-": 6, "D+": 7, "D": 8, "D-": 9, "F+": 10, "F": 11}
    sorted_voices = sorted(
        voices.items(),
        key=lambda x: (grade_order.get(x[1].get("grade", ""), 99), x[1]["name"])
    )

    total = len(sorted_voices)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start = page * page_size
    end = start + page_size
    page_voices = sorted_voices[start:end]

    return {
        "voices": [(vid, vdata) for vid, vdata in page_voices],
        "page": page,
        "total_pages": total_pages,
        "has_next": page < total_pages - 1,
        "has_prev": page > 0,
    }


def get_kokoro_lang_code(lang_id: str) -> str:
    """Return the Kokoro pipeline lang_code for a given language."""
    lang = get_language(lang_id)
    if lang is None:
        raise ValueError(f"Unknown language: {lang_id}")
    return lang["lang_code"]
