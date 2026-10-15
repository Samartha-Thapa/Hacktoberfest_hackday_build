from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException, Request as FastAPIRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .config import Settings

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SESSION_COOKIE = "talkie_mail_session"


@dataclass
class OAuthAttempt:
    state: str
    code_verifier: str
    created_at: float


@dataclass
class SessionRecord:
    created_at: float
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    attempt: OAuthAttempt | None = None
    credentials: Credentials | None = None


class OAuthStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def session_id(self, request: FastAPIRequest) -> str | None:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id and session_id in self._sessions:
            return session_id
        return None

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = SessionRecord(created_at=time.time())
        return session_id

    def get_or_create_session(self, request: FastAPIRequest) -> tuple[str, bool]:
        session_id = self.session_id(request)
        if session_id:
            return session_id, False
        return self.create_session(), True

    def save_attempt(self, session_id: str, attempt: OAuthAttempt) -> None:
        self._sessions[session_id].attempt = attempt

    def consume_attempt(self, session_id: str, state: str, max_age: int) -> OAuthAttempt:
        record = self._sessions.get(session_id)
        attempt = record.attempt if record else None
        if not attempt or not secrets.compare_digest(attempt.state, state):
            raise HTTPException(status_code=400, detail="Invalid OAuth state.")
        record.attempt = None
        if time.time() - attempt.created_at > max_age:
            raise HTTPException(status_code=400, detail="OAuth state has expired.")
        return attempt

    def credentials(self, session_id: str) -> Credentials | None:
        record = self._sessions.get(session_id)
        return record.credentials if record else None

    def csrf_token(self, session_id: str) -> str | None:
        record = self._sessions.get(session_id)
        return record.csrf_token if record else None

    def set_credentials(self, session_id: str, credentials: Credentials) -> None:
        self._sessions[session_id].credentials = credentials

    def disconnect(self, session_id: str) -> Credentials | None:
        record = self._sessions.get(session_id)
        if not record:
            return None
        credentials = record.credentials
        record.credentials = None
        record.attempt = None
        return credentials

    def clear(self) -> None:
        self._sessions.clear()


store = OAuthStore()


def require_oauth_config(settings: Settings) -> None:
    if not settings.google_oauth_configured:
        raise HTTPException(
            status_code=503,
            detail="Gmail connection is not configured on this server.",
        )


def build_flow(settings: Settings, state: str | None = None) -> Flow:
    require_oauth_config(settings)
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=[GMAIL_READONLY_SCOPE],
        state=state,
        redirect_uri=settings.google_oauth_redirect_uri,
    )


def revoke_credentials(credentials: Credentials) -> None:
    token = credentials.refresh_token or credentials.token
    if not token:
        return
    request = Request(
        "https://oauth2.googleapis.com/revoke",
        data=urlencode({"token": token}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            return
    except (HTTPError, URLError, TimeoutError) as error:
        raise HTTPException(
            status_code=502,
            detail="Google credential revocation failed; try disconnecting again.",
        ) from error
