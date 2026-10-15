from pathlib import Path
import logging
import time
import secrets

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.auth.exceptions import GoogleAuthError
from requests import RequestException

from .config import get_settings
from .gmail import (
    GmailAuthenticationError,
    GmailMessageNotFound,
    GmailRateLimitError,
    GmailServiceError,
    retrieve_inbox_message,
    retrieve_inbox_messages,
)
from .oauth import (
    GMAIL_READONLY_SCOPE,
    SESSION_COOKIE,
    OAuthAttempt,
    build_flow,
    revoke_credentials,
    store,
)
from .summarizer import (
    SummarizationInputError,
    SummarizationUnavailable,
    summarize_email,
)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"

app = FastAPI(
    title="Talkie Mail API",
    description="Backend for the Talkie Mail email companion",
    version="0.1.0",
)
logger = logging.getLogger(__name__)


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health_check():
    return {"status": "ok", "app": "Talkie Mail"}


def set_session_cookie(response: RedirectResponse, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=settings.session_max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


@app.get("/api/gmail/status")
def gmail_status(request: Request):
    configured = get_settings().google_oauth_configured
    session_id = store.session_id(request)
    credentials = store.credentials(session_id) if session_id else None
    return {
        "configured": configured,
        "connected": bool(configured and credentials and credentials.valid),
        "scope": GMAIL_READONLY_SCOPE if credentials else None,
        "csrf_token": store.csrf_token(session_id) if session_id else None,
    }


@app.get("/api/gmail/messages")
def gmail_messages(
    request: Request,
    max_results: int = Query(20, ge=1, le=50),
    page_token: str | None = Query(None, alias="pageToken"),
):
    session_id = store.session_id(request)
    credentials = store.credentials(session_id) if session_id else None
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Connect Gmail before loading messages.",
        )
    try:
        return retrieve_inbox_messages(
            credentials,
            max_results=max_results,
            page_token=page_token,
        )
    except GmailAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except GmailRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GmailServiceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/gmail/messages/{message_id}")
def gmail_message_detail(message_id: str, request: Request):
    if not message_id:
        raise HTTPException(status_code=404, detail="Inbox message not found.")
    session_id = store.session_id(request)
    credentials = store.credentials(session_id) if session_id else None
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Connect Gmail before loading messages.",
        )
    try:
        return retrieve_inbox_message(credentials, message_id=message_id)
    except GmailMessageNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GmailAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except GmailRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GmailServiceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/gmail/messages/{message_id}/summarize")
def gmail_message_summary(message_id: str, request: Request):
    session_id = store.session_id(request)
    credentials = store.credentials(session_id) if session_id else None
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Connect Gmail before summarizing messages.",
        )
    started_at = time.monotonic()
    try:
        message = retrieve_inbox_message(credentials, message_id=message_id)
        summary = summarize_email(str(message.get("body") or ""), get_settings())
        return {"message_id": message_id, **summary}
    except GmailMessageNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GmailAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except SummarizationInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SummarizationUnavailable as error:
        cause = error.__cause__ or error
        logger.warning(
            "Email summarization failed: exception_type=%s elapsed_seconds=%.3f "
            "http_status=%s response_length=%s json_parsed=%s "
            "parse_stage=%s finish_reason=%s generated_content_length=%s content_type=%s",
            type(cause).__name__,
            time.monotonic() - started_at,
            error.status_code,
            error.response_length,
            error.json_parsed,
            error.parse_stage,
            error.finish_reason,
            error.generated_content_length,
            error.content_type,
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GmailRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GmailServiceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/gmail/authorize")
def gmail_authorize(request: Request):
    settings = get_settings()
    session_id, created = store.get_or_create_session(request)
    flow = build_flow(settings)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge_method="S256",
    )
    code_verifier = flow.code_verifier
    if not code_verifier:
        raise HTTPException(status_code=500, detail="OAuth PKCE setup failed.")
    store.save_attempt(
        session_id,
        OAuthAttempt(state=state, code_verifier=code_verifier, created_at=time.time()),
    )
    response = RedirectResponse(authorization_url, status_code=307)
    if created:
        set_session_cookie(response, session_id)
    return response


@app.get("/api/gmail/callback")
def gmail_callback(request: Request):
    settings = get_settings()
    error = request.query_params.get("error")
    session_id = store.session_id(request)
    if not session_id:
        return RedirectResponse("/?oauth=error#settings", status_code=303)
    state = request.query_params.get("state")
    if not state:
        return RedirectResponse("/?oauth=error#settings", status_code=303)
    try:
        attempt = store.consume_attempt(session_id, state, settings.oauth_state_max_age)
    except HTTPException:
        return RedirectResponse("/?oauth=error#settings", status_code=303)
    if error:
        store.disconnect(session_id)
        return RedirectResponse("/?oauth=denied#settings", status_code=303)
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse("/?oauth=error#settings", status_code=303)
    try:
        flow = build_flow(settings, state=state)
        flow.code_verifier = attempt.code_verifier
        flow.fetch_token(code=code)
        if not flow.credentials or not flow.credentials.valid:
            raise GoogleAuthError("Google returned invalid credentials.")
        store.set_credentials(session_id, flow.credentials)
    except HTTPException:
        return RedirectResponse("/?oauth=error#settings", status_code=303)
    except (GoogleAuthError, RequestException, ValueError):
        return RedirectResponse("/?oauth=error#settings", status_code=303)
    return RedirectResponse("/?oauth=connected#settings", status_code=303)


@app.post("/api/gmail/disconnect")
def gmail_disconnect(request: Request):
    session_id = store.session_id(request)
    if not session_id:
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")
    csrf_token = request.headers.get("X-CSRF-Token")
    expected_token = store.csrf_token(session_id)
    if (
        not csrf_token
        or not expected_token
        or not secrets.compare_digest(csrf_token, expected_token)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")
    credentials = store.disconnect(session_id)
    if credentials:
        revoke_credentials(credentials)
    return {"connected": False}
