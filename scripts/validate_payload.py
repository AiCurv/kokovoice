"""
Validate the incoming repository_dispatch payload.

Checks that language_id, voice_id, kokoro_lang_code, and speed
are all valid against the pinned voice registry. Rejects any
arbitrary or malformed identifiers.

This is a security gate: invalid payloads abort the workflow
before any inference runs.
"""

import json
import os
import sys
from pathlib import Path

VOICES_PATH = Path(__file__).parent.parent / "kokoro" / "voices.json"


def main():
    with open(VOICES_PATH) as f:
        registry = json.load(f)

    lang_id = os.environ.get("DISPATCH_LANG", "")
    voice_id = os.environ.get("DISPATCH_VOICE", "")
    kokoro_lang_code = os.environ.get("DISPATCH_KOKORO_LANG", "")
    speed_str = os.environ.get("DISPATCH_SPEED", "1.0")

    # Validate language
    supported = registry["supported_languages"]
    if lang_id not in supported:
        print(f"FAIL: Unknown language_id '{lang_id}'")
        sys.exit(1)

    lang_data = supported[lang_id]

    # Validate kokoro_lang_code matches language
    if kokoro_lang_code != lang_data["lang_code"]:
        print(f"FAIL: lang_code '{kokoro_lang_code}' does not match language '{lang_id}' (expected '{lang_data['lang_code']}')")
        sys.exit(1)

    # Validate voice belongs to language
    if voice_id not in lang_data["voices"]:
        print(f"FAIL: voice_id '{voice_id}' not found in language '{lang_id}'")
        sys.exit(1)

    # Validate speed is a reasonable number
    try:
        speed = float(speed_str)
        if speed < 0.5 or speed > 2.0:
            print(f"FAIL: speed {speed} out of range [0.5, 2.0]")
            sys.exit(1)
    except ValueError:
        print(f"FAIL: speed '{speed_str}' is not a valid number")
        sys.exit(1)

    print(f"OK: lang={lang_id} lang_code={kokoro_lang_code} voice={voice_id} speed={speed}")
    sys.exit(0)


if __name__ == "__main__":
    main()
