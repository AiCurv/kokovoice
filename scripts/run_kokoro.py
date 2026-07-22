"""
Run Kokoro TTS inference and deliver the result to Oracle.

Two-step delivery architecture:
  Step 1: POST a small JSON callback to Oracle /completion endpoint
          containing session_id, request_id, status, and error_message.
          On success, Oracle returns an upload_token (HMAC-authenticated)
          and an upload_url.

  Step 2: POST the actual WAV audio as binary multipart/form-data to
          Oracle /upload/audio endpoint, authenticated by the upload_token
          from Step 1.

Why two steps? GitHub Actions repository_dispatch client_payload has a
~65 KB hard limit. Even the shortest TTS audio (3 seconds, 208 KB base64)
exceeds this limit by 3x. Splitting metadata and binary data avoids this
constraint entirely.

Security:
  - Text is passed via environment variables, never interpolated into shell commands
  - The upload_token is HMAC(session_id:request_id, ORACLE_COMPLETION_SECRET)
  - The completion endpoint authenticates via X-Completion-Secret header
  - The upload endpoint authenticates via the upload_token form field
"""

import json
import os
import sys
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kokoro import KPipeline
import soundfile as sf


def validate_wav(path: str) -> bool:
    """Validate that the WAV file exists, is non-empty, and is 24 kHz."""
    if not Path(path).exists():
        return False
    data, sr = sf.read(path)
    if len(data) == 0:
        return False
    if sr != 24000:
        print(f"WARNING: sample rate is {sr}, expected 24000")
    return True


def send_completion_callback(session_id, request_id, status, error_message=None):
    """
    Step 1: Send a small JSON callback to Oracle /completion endpoint.

    On success, Oracle returns an upload_token and upload_url for Step 2.
    On failure, Oracle marks the session as failed.

    Returns: dict with 'upload_url' and 'upload_token' on success,
             or None on failure.
    """
    completion_url = os.environ.get("ORACLE_COMPLETION_URL", "")
    completion_secret = os.environ.get("ORACLE_COMPLETION_SECRET", "")

    if not completion_url:
        print("WARNING: ORACLE_COMPLETION_URL not set; cannot deliver audio")
        return None

    payload = {
        "session_id": session_id,
        "request_id": request_id,
        "status": status,
    }
    if error_message:
        payload["error_message"] = error_message[:500]

    headers = {
        "X-Completion-Secret": completion_secret,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(completion_url, json=payload, headers=headers, timeout=30)
        print(f"Completion callback: status_code={resp.status_code}")

        if status == "success" and resp.status_code == 200:
            result = resp.json()
            return {
                "upload_url": result.get("upload_url", ""),
                "upload_token": result.get("upload_token", ""),
            }
        return None

    except Exception as e:
        print(f"Completion callback failed: {e}")
        return None


def upload_audio_binary(upload_url, upload_token, session_id, request_id, audio_path):
    """
    Step 2: Upload the WAV audio file as binary multipart/form-data.

    The upload is authenticated by the upload_token from Step 1
    (HMAC-signed, one-time use).
    """
    try:
        with open(audio_path, "rb") as f:
            files = {"audio_file": ("kokoro_output.wav", f, "audio/wav")}
            data = {
                "session_id": session_id,
                "request_id": request_id,
                "upload_token": upload_token,
            }
            resp = requests.post(
                upload_url,
                files=files,
                data=data,
                timeout=120,  # Allow up to 2 minutes for large file upload
            )
        print(f"Audio upload: status_code={resp.status_code}")
        return resp.status_code in (200, 201)
    except Exception as e:
        print(f"Audio upload failed: {e}")
        return False


def main():
    text = os.environ["DISPATCH_TEXT"]
    lang_code = os.environ["DISPATCH_LANG_CODE"]
    voice_id = os.environ["DISPATCH_VOICE"]
    speed = float(os.environ.get("DISPATCH_SPEED", "1.0"))
    session_id = os.environ["DISPATCH_SESSION_ID"]
    request_id = os.environ["DISPATCH_REQUEST_ID"]
    output_path = "/tmp/kokoro_output.wav"

    try:
        # Initialize Kokoro pipeline
        pipeline = KPipeline(lang_code=lang_code)

        # Run inference
        generator = pipeline(text, voice=voice_id, speed=speed)

        # Collect all audio segments
        audio_segments = []
        for _, _, audio in generator:
            if audio is not None:
                audio_segments.append(audio)

        if not audio_segments:
            raise ValueError("KPipeline produced no audio segments")

        # Concatenate segments
        import numpy as np
        full_audio = np.concatenate(audio_segments)

        # Write WAV file
        sf.write(output_path, full_audio, 24000)

        # Validate output
        if not validate_wav(output_path):
            raise ValueError("Output WAV validation failed")

        print(f"Kokoro inference succeeded: {len(full_audio)} samples, 24 kHz")
        print(f"Output file: {output_path} ({Path(output_path).stat().st_size} bytes)")

        # ── Step 1: Send completion callback (small JSON) ──
        upload_info = send_completion_callback(session_id, request_id, "success")

        if upload_info and upload_info.get("upload_url"):
            # ── Step 2: Upload audio binary ──
            success = upload_audio_binary(
                upload_info["upload_url"],
                upload_info["upload_token"],
                session_id,
                request_id,
                output_path,
            )
            if not success:
                print("Audio upload failed — reporting failure")
                send_completion_callback(
                    session_id, request_id, "failure",
                    "Audio upload to Oracle failed"
                )
        else:
            print("WARNING: No upload_url received from Oracle; audio not delivered")

    except Exception as e:
        error_msg = str(e)
        print(f"Kokoro inference FAILED: {error_msg}")

        # Report failure to Oracle
        send_completion_callback(session_id, request_id, "failure", error_msg)

        sys.exit(1)


if __name__ == "__main__":
    main()
