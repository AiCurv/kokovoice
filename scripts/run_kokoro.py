"""
Run Kokoro TTS inference and deliver the result to Oracle.

Supports text chunking: if DISPATCH_CHUNKS_JSON is provided, each chunk
is processed sequentially in a single workflow run. All audio outputs are
delivered to Oracle in order.

Two-step delivery architecture per chunk:
  Step 1: POST a small JSON callback to Oracle /completion endpoint
  Step 2: POST the WAV audio as binary multipart/form-data to Oracle /upload/audio

Why two steps? GitHub Actions repository_dispatch client_payload has a
~65 KB hard limit. Even the shortest TTS audio exceeds this by 3x.

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
import numpy as np


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


def send_completion_callback(session_id, request_id, status, error_message=None,
                             chunk_num=1, total_chunks=1):
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
        "chunk_num": chunk_num,
        "total_chunks": total_chunks,
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


def upload_audio_binary(upload_url, upload_token, session_id, request_id,
                        audio_path, chunk_num=1, total_chunks=1):
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
                "chunk_num": str(chunk_num),
                "total_chunks": str(total_chunks),
            }
            resp = requests.post(
                upload_url,
                files=files,
                data=data,
                timeout=120,
            )
        print(f"Audio upload (chunk {chunk_num}/{total_chunks}): status_code={resp.status_code}")
        return resp.status_code in (200, 201)
    except Exception as e:
        print(f"Audio upload failed: {e}")
        return False


def run_single_chunk(pipeline, text, voice_id, speed, session_id, request_id,
                     chunk_num, total_chunks, completion_url, completion_secret):
    """
    Run Kokoro inference on a single text chunk and deliver the audio.

    Returns True on success, False on failure.
    """
    output_path = f"/tmp/kokoro_output_chunk{chunk_num}.wav"

    try:
        # Run inference
        generator = pipeline(text, voice=voice_id, speed=speed)

        # Collect all audio segments
        audio_segments = []
        for _, _, audio in generator:
            if audio is not None:
                audio_segments.append(audio)

        if not audio_segments:
            raise ValueError(f"KPipeline produced no audio segments for chunk {chunk_num}")

        # Concatenate segments
        full_audio = np.concatenate(audio_segments)

        # Write WAV file
        sf.write(output_path, full_audio, 24000)

        # Validate output
        if not validate_wav(output_path):
            raise ValueError(f"Output WAV validation failed for chunk {chunk_num}")

        print(f"Chunk {chunk_num}/{total_chunks}: {len(full_audio)} samples, 24 kHz")

        # ── Step 1: Send completion callback ──
        upload_info = send_completion_callback(
            session_id, request_id, "success",
            chunk_num=chunk_num, total_chunks=total_chunks,
        )

        if upload_info and upload_info.get("upload_url"):
            # ── Step 2: Upload audio binary ──
            success = upload_audio_binary(
                upload_info["upload_url"],
                upload_info["upload_token"],
                session_id, request_id,
                output_path,
                chunk_num=chunk_num, total_chunks=total_chunks,
            )
            if not success:
                print(f"Audio upload failed for chunk {chunk_num} — reporting failure")
                send_completion_callback(
                    session_id, request_id, "failure",
                    f"Audio upload to Oracle failed for chunk {chunk_num}",
                    chunk_num=chunk_num, total_chunks=total_chunks,
                )
                return False
        else:
            print("WARNING: No upload_url received from Oracle; audio not delivered")
            return False

        # Clean up chunk file
        Path(output_path).unlink(missing_ok=True)

        return True

    except Exception as e:
        error_msg = str(e)
        print(f"Chunk {chunk_num} FAILED: {error_msg}")

        # Clean up on failure too
        Path(output_path).unlink(missing_ok=True)

        # Report this specific chunk failure
        send_completion_callback(
            session_id, request_id, "failure",
            f"Chunk {chunk_num} generation failed: {error_msg}",
            chunk_num=chunk_num, total_chunks=total_chunks,
        )
        return False


def main():
    text = os.environ.get("DISPATCH_TEXT", "")
    lang_code = os.environ["DISPATCH_LANG_CODE"]
    voice_id = os.environ["DISPATCH_VOICE"]
    speed = float(os.environ.get("DISPATCH_SPEED", "1.0"))
    session_id = os.environ["DISPATCH_SESSION_ID"]
    request_id = os.environ["DISPATCH_REQUEST_ID"]
    chunks_json = os.environ.get("DISPATCH_CHUNKS_JSON", "")
    total_chunks_str = os.environ.get("DISPATCH_TOTAL_CHUNKS", "1")

    # Parse chunks from JSON string (GitHub Actions passes it as a stringified JSON array)
    if chunks_json:
        try:
            chunks = json.loads(chunks_json)
        except json.JSONDecodeError:
            print("WARNING: Failed to parse DISPATCH_CHUNKS_JSON; using full text as single chunk")
            chunks = [text]
    else:
        # No chunks provided — use full text as single chunk
        chunks = [text]

    total_chunks = int(total_chunks_str)

    # Validate that chunk count matches
    if len(chunks) != total_chunks:
        print(f"WARNING: chunks count ({len(chunks)}) != total_chunks ({total_chunks}); using actual count")
        total_chunks = len(chunks)

    if total_chunks == 0:
        print("ERROR: No text chunks to process")
        send_completion_callback(session_id, request_id, "failure", "No text chunks provided")
        sys.exit(1)

    print(f"Processing {total_chunks} chunks for session {session_id}")

    try:
        # Initialize Kokoro pipeline (one pipeline for all chunks)
        pipeline = KPipeline(lang_code=lang_code)

        # Process each chunk sequentially in order
        failed_chunks = []
        for i, chunk_text in enumerate(chunks, 1):
            print(f"--- Chunk {i}/{total_chunks}: {len(chunk_text)} chars ---")
            success = run_single_chunk(
                pipeline, chunk_text, voice_id, speed,
                session_id, request_id, i, total_chunks,
                os.environ.get("ORACLE_COMPLETION_URL", ""),
                os.environ.get("ORACLE_COMPLETION_SECRET", ""),
            )
            if not success:
                failed_chunks.append(i)

        if failed_chunks:
            print(f"FAILED chunks: {failed_chunks}")
            # If any chunks failed, the session is already marked FAILED via completion callback
            sys.exit(1)

        print(f"All {total_chunks} chunks delivered successfully!")

    except Exception as e:
        error_msg = str(e)
        print(f"Kokoro inference FAILED: {error_msg}")
        send_completion_callback(session_id, request_id, "failure", error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
