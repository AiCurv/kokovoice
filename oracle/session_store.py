"""
Lightweight session store for TTS sessions.

Sessions track the stateful flow: text received → language → voice →
confirmation → generating → upload_pending → delivery → completed.
State is persisted to a JSON file on disk so it survives process restarts.

Security: Only ALLOWED_TELEGRAM_USER_ID may create or interact with sessions.
Session ownership is validated on every callback.

Thread safety: A threading.Lock protects all reads/writes since Flask
runs with threaded=True by default and background threads from dispatch
also mutate sessions.
"""

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from oracle.config import ALLOWED_TELEGRAM_USER_ID, SESSION_EXPIRY_SECONDS

_STORE_PATH = Path(__file__).parent / "sessions.json"


class Session:
    """Represents a single TTS session."""

    def __init__(self, data: dict):
        self.session_id: str = data.get("session_id", "")
        self.telegram_user_id: int = data.get("telegram_user_id", 0)
        self.chat_id: int = data.get("chat_id", 0)
        self.source_message_id: int = data.get("source_message_id", 0)
        self.ui_message_id: int = data.get("ui_message_id", 0)
        self.input_text: str = data.get("input_text", "")
        self.language_id: str = data.get("language_id", "")
        self.kokoro_lang_code: str = data.get("kokoro_lang_code", "")
        self.voice_id: str = data.get("voice_id", "")
        self.speed: float = data.get("speed", 1.0)
        self.state: str = data.get("state", "IDLE")
        self.voice_page: int = data.get("voice_page", 0)
        self.github_run_id: str = data.get("github_run_id", "")
        self.request_id: str = data.get("request_id", "")
        self.created_at: float = data.get("created_at", 0.0)
        self.updated_at: float = data.get("updated_at", 0.0)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "telegram_user_id": self.telegram_user_id,
            "chat_id": self.chat_id,
            "source_message_id": self.source_message_id,
            "ui_message_id": self.ui_message_id,
            "input_text": self.input_text,
            "language_id": self.language_id,
            "kokoro_lang_code": self.kokoro_lang_code,
            "voice_id": self.voice_id,
            "speed": self.speed,
            "state": self.state,
            "voice_page": self.voice_page,
            "github_run_id": self.github_run_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def is_expired(self) -> bool:
        return time.time() - self.created_at > SESSION_EXPIRY_SECONDS

    def is_owned_by(self, user_id: int) -> bool:
        return self.telegram_user_id == user_id


class SessionStore:
    """Persistent JSON-based session store with thread safety."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        with self._lock:
            if _STORE_PATH.exists():
                with open(_STORE_PATH) as f:
                    data = json.load(f)
                for sid, sdata in data.items():
                    self._sessions[sid] = Session(sdata)
                self._purge_expired()

    def _save(self):
        """Save to disk — MUST be called while holding self._lock."""
        with open(_STORE_PATH, "w") as f:
            json.dump({sid: s.to_dict() for sid, s in self._sessions.items()}, f, indent=2)

    def _purge_expired(self):
        """Purge expired sessions — MUST be called while holding self._lock."""
        expired = [sid for sid, s in self._sessions.items() if s.is_expired()]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            self._save()

    def create(self, telegram_user_id: int, chat_id: int,
               source_message_id: int, input_text: str) -> Session:
        """Create a new session. Only allowed user may create sessions."""
        if telegram_user_id != ALLOWED_TELEGRAM_USER_ID:
            raise PermissionError(f"User {telegram_user_id} is not authorized")

        session_id = uuid.uuid4().hex[:12]
        now = time.time()
        session = Session({
            "session_id": session_id,
            "telegram_user_id": telegram_user_id,
            "chat_id": chat_id,
            "source_message_id": source_message_id,
            "ui_message_id": 0,
            "input_text": input_text,
            "language_id": "",
            "kokoro_lang_code": "",
            "voice_id": "",
            "speed": 1.0,
            "state": "TEXT_RECEIVED",
            "voice_page": 0,
            "github_run_id": "",
            "request_id": "",
            "created_at": now,
            "updated_at": now,
        })
        with self._lock:
            self._sessions[session_id] = session
            self._save()
        return session

    def get(self, session_id: str) -> Optional[Session]:
        """Retrieve a session by ID. Returns None if expired or missing."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.is_expired():
                del self._sessions[session_id]
                self._save()
                return None
            return session

    def get_by_user(self, telegram_user_id: int) -> Optional[Session]:
        """Get the most recent active session for a user."""
        with self._lock:
            self._purge_expired()
            user_sessions = [
                s for s in self._sessions.values()
                if s.telegram_user_id == telegram_user_id and not s.is_expired()
            ]
            if not user_sessions:
                return None
            return max(user_sessions, key=lambda s: s.updated_at)

    def update(self, session: Session):
        """Update a session and persist."""
        session.updated_at = time.time()
        with self._lock:
            self._sessions[session.session_id] = session
            self._save()

    def delete(self, session_id: str):
        """Delete a session."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                self._save()
