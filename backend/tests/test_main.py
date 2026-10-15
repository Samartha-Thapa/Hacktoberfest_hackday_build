import base64
import time
import requests

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.config import Settings, get_settings
from backend.app import main as main_module
from backend.app import gmail as gmail_module
from backend.app.gmail import (
    GmailAuthenticationError,
    GmailMessageNotFound,
    GmailServiceError,
)
from backend.app.summarizer import (
    SummarizationInputError,
    SummarizationUnavailable,
    summarize_email,
)
from backend.app.main import app
from backend.app.oauth import OAuthAttempt, store

client = TestClient(app)


def teardown_function():
    store.clear()
    client.cookies.clear()
    get_settings.cache_clear()


def test_health_returns_existing_contract():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "Talkie Mail"}


def test_homepage_contains_accessible_disconnected_shell():
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="en">' in response.text
    assert '<main class="main-content">' in response.text
    assert 'aria-label="Main navigation"' in response.text
    assert "Hey, I'm Buddy. Let's make your inbox feel a little calmer." in response.text
    assert "Yay!! Gmail connected!" in response.text
    assert "read-only Gmail access" in response.text
    assert 'src="/static/assets/buddy/buddy.svg"' in response.text
    assert 'data-connection-state="disconnected"' in response.text
    assert "Your inbox is ready when you are" in response.text
    assert "Summarize this email" in response.text
    assert "data-summary-invitation" in response.text
    assert "summary-buddy-float" in client.get("/static/styles.css").text


def test_onboarding_script_contains_stateful_steps_and_persistence_controls():
    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "state.connection" in response.text
    assert "/api/gmail/messages/" in response.text
    assert "textContent" in response.text
    assert "body_content" in response.text
    assert "safeHttpUrl" in response.text
    assert "innerHTML" not in response.text
    assert 'state.connection === "connected"' in response.text
    assert "talkie-mail-onboarding-complete" in response.text
    assert "localStorage" in response.text
    assert "showCelebration" in response.text
    assert "prefers-reduced-motion" in client.get("/static/styles.css").text
    assert "onboarding-scene-in" in client.get("/static/styles.css").text


def test_static_assets_are_served(monkeypatch):
    stylesheet = client.get("/static/styles.css")
    script = client.get("/static/app.js")
    buddy_notes = client.get("/static/assets/buddy/README.md")
    buddy_image = client.get("/assets/buddy_single_sparkling_eyes.png")
    buddy_svg = client.get("/static/assets/buddy/buddy.svg")

    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]
    assert "--coral: #ff5a50" in stylesheet.text
    assert script.status_code == 200
    assert "loadMessages" in script.text
    assert buddy_notes.status_code == 200
    assert "canonical Buddy artwork" in buddy_notes.text
    assert buddy_image.status_code == 200
    assert buddy_image.headers["content-type"] == "image/png"
    assert buddy_svg.status_code == 200
    assert "<path" in buddy_svg.text
    assert "<ellipse" in buddy_svg.text
    assert "buddy-smile" in buddy_svg.text
    assert "buddy-left-blush" in buddy_svg.text
    assert "buddy-blink" in buddy_svg.text
    assert "@keyframes buddy-blink" in buddy_svg.text
    assert "prefers-reduced-motion" in buddy_svg.text
    assert "<image" not in buddy_svg.text


def test_gmail_status_reports_unconfigured_without_credentials(monkeypatch):
    unconfigured = Settings(
        app_env="test",
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_oauth_redirect_uri=None,
        session_secret=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: unconfigured)
    response = client.get("/api/gmail/status")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "connected": False,
        "scope": None,
        "csrf_token": None,
    }


def test_gmail_messages_returns_metadata_and_pagination(monkeypatch):
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    expected = {
        "messages": [
            {
                "id": "message-1",
                "thread_id": "thread-1",
                "from": "sender@example.com",
                "subject": "Hello",
                "date": "Wed, 14 Oct 2026 12:00:00 +0000",
                "snippet": "A real Gmail snippet",
            }
        ],
        "next_page_token": "next-page",
        "result_size_estimate": 1,
    }

    def fake_retrieve(credentials, *, max_results, page_token):
        assert max_results == 10
        assert page_token == "previous-page"
        assert credentials is store.credentials(session_id)
        return expected

    monkeypatch.setattr(main_module, "retrieve_inbox_messages", fake_retrieve)

    response = client.get(
        "/api/gmail/messages?max_results=10&pageToken=previous-page"
    )

    assert response.status_code == 200
    assert response.json() == expected


def test_gmail_messages_rejects_disconnected_session():
    response = client.get("/api/gmail/messages")

    assert response.status_code == 401
    assert response.json() == {"detail": "Connect Gmail before loading messages."}


def test_gmail_messages_maps_provider_failure_to_safe_error(monkeypatch):
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    monkeypatch.setattr(
        main_module,
        "retrieve_inbox_messages",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GmailServiceError("Gmail could not complete the request.")
        ),
    )

    response = client.get("/api/gmail/messages")

    assert response.status_code == 502
    assert response.json() == {"detail": "Gmail could not complete the request."}


def test_gmail_messages_maps_expired_credentials_to_authentication_error(monkeypatch):
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    monkeypatch.setattr(
        main_module,
        "retrieve_inbox_messages",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GmailAuthenticationError("Gmail authorization has expired.")
        ),
    )

    response = client.get("/api/gmail/messages")

    assert response.status_code == 401
    assert response.json() == {"detail": "Gmail authorization has expired."}


def test_gmail_messages_enforces_bounded_result_limit():
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)

    too_many = client.get("/api/gmail/messages?max_results=51")
    zero = client.get("/api/gmail/messages?max_results=0")

    assert too_many.status_code == 422
    assert zero.status_code == 422


def test_gmail_client_requests_inbox_metadata_only(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.payload = payload

        def json(self):
            return self.payload

    responses = iter(
        [
            FakeResponse(
                {
                    "messages": [{"id": "message-1"}],
                    "nextPageToken": "next-page",
                    "resultSizeEstimate": 1,
                }
            ),
            FakeResponse(
                {
                    "id": "message-1",
                    "threadId": "thread-1",
                    "snippet": "Preview",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "sender@example.com"},
                            {"name": "Subject", "value": "Subject"},
                            {"name": "Date", "value": "Date"},
                        ]
                    },
                }
            ),
        ]
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(gmail_module.requests, "get", fake_get)

    result = gmail_module.retrieve_inbox_messages(
        FakeCredentials(),
        max_results=20,
        page_token=None,
    )

    assert result["messages"][0]["id"] == "message-1"
    assert result["messages"][0]["snippet"] == "Preview"
    assert calls[0][1]["params"] == {
        "userId": "me",
        "labelIds": "INBOX",
        "maxResults": 20,
    }
    assert calls[1][1]["params"] == {
        "format": "metadata",
        "metadataHeaders": ["From", "Subject", "Date"],
    }
    assert calls[0][1]["headers"] == {"Authorization": "Bearer access-token"}


def test_gmail_client_maps_metadata_headers_and_preserves_missing_values(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    responses = iter(
        [
            FakeResponse({"messages": [{"id": "message-1"}, {"id": "message-2"}]}),
            FakeResponse(
                {
                    "id": "message-1",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Ada <ada@example.com>"},
                            {"name": "Subject", "value": "A subject"},
                            {"name": "Date", "value": "Wed, 14 Oct 2026 12:00:00 +0000"},
                        ]
                    },
                    "snippet": "First snippet",
                }
            ),
            FakeResponse(
                {
                    "id": "message-2",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "sender@example.com"},
                        ]
                    },
                    "snippet": "Second snippet",
                }
            ),
        ]
    )
    monkeypatch.setattr(
        gmail_module.requests,
        "get",
        lambda *args, **kwargs: next(responses),
    )

    messages = gmail_module.retrieve_inbox_messages(
        FakeCredentials(),
        max_results=20,
        page_token=None,
    )["messages"]

    assert messages[0]["from"] == "Ada <ada@example.com>"
    assert messages[0]["subject"] == "A subject"
    assert messages[0]["date"] == "Wed, 14 Oct 2026 12:00:00 +0000"
    assert messages[0]["snippet"] == "First snippet"
    assert messages[1]["from"] == "sender@example.com"
    assert messages[1]["subject"] == ""
    assert messages[1]["date"] == ""
    assert messages[1]["snippet"] == "Second snippet"


def test_gmail_client_returns_only_inbox_message_with_plain_text_body(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "id": "message-1",
                "threadId": "thread-1",
                "labelIds": ["INBOX", "IMPORTANT"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "Ada <ada@example.com>"},
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Date", "value": "Date"},
                    ],
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Plain body").decode(),
                    },
                },
            }

    calls = []
    monkeypatch.setattr(
        gmail_module.requests,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or FakeResponse()),
    )

    result = gmail_module.retrieve_inbox_message(
        FakeCredentials(),
        message_id="message-1",
    )

    assert result["from"] == "Ada <ada@example.com>"
    assert result["subject"] == "Hello"
    assert result["body"] == "Plain body"
    assert calls[0][1]["params"] == {
        "format": "full",
        "fields": "id,threadId,labelIds,payload",
    }


def test_gmail_client_rejects_message_without_inbox_label(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "id": "message-1",
                "labelIds": ["SENT"],
                "payload": {
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Secret body").decode(),
                    }
                },
            }

    monkeypatch.setattr(gmail_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(GmailMessageNotFound):
        gmail_module.retrieve_inbox_message(FakeCredentials(), message_id="message-1")


def test_gmail_client_decodes_multipart_plain_text_and_html_fallback(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    plain = base64.urlsafe_b64encode(b"Plain part").decode()
    html = base64.urlsafe_b64encode(
        b"<html><body><p>HTML <strong>part</strong></p><script>bad()</script></body></html>"
    ).decode()
    responses = iter(
        [
            FakeResponse(
                {
                    "id": "message-1",
                    "labelIds": ["INBOX"],
                    "payload": {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/html", "body": {"data": html}},
                            {"mimeType": "text/plain", "body": {"data": plain}},
                        ],
                    },
                }
            ),
            FakeResponse(
                {
                    "id": "message-2",
                    "labelIds": ["INBOX"],
                    "payload": {
                        "mimeType": "text/html",
                        "body": {"data": html},
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr(gmail_module.requests, "get", lambda *args, **kwargs: next(responses))

    plain_result = gmail_module.retrieve_inbox_message(
        FakeCredentials(), message_id="message-1"
    )
    html_result = gmail_module.retrieve_inbox_message(
        FakeCredentials(), message_id="message-2"
    )

    assert plain_result["body"] == "Plain part"
    assert html_result["body"] == "HTML part"
    assert "bad()" not in html_result["body"]


def test_gmail_html_cleanup_preserves_content_and_hides_tracking_noise(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def json(self):
            html = """
                <html><head><style>.hidden { display: none }</style></head>
                <body>
                  <h1>Weekly update</h1>
                  <p>Welcome to the community.</p>
                  <p><a href="https://tracking.example.test/click?utm_campaign=secret">
                    View the schedule
                  </a></p>
                  <p><a href="https://example.test/docs">https://example.test/docs</a></p>
                  <ul><li>Challenge one</li><li>Use code ABC-123</li></ul>
                  <img alt="GHW Logo"><img alt="Logo">
                  <script>trackingSecret()</script>
                </body></html>
            """
            return {
                "id": "message-html",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/html",
                    "body": {
                        "data": base64.urlsafe_b64encode(html.encode()).decode(),
                    },
                },
            }

    monkeypatch.setattr(gmail_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    result = gmail_module.retrieve_inbox_message(
        FakeCredentials(),
        message_id="message-html",
    )
    body = result["body"]

    assert "Weekly update" in body
    assert "Welcome to the community." in body
    assert "View the schedule" in body
    assert "tracking.example.test" not in body
    assert body.count("https://example.test/docs") == 1
    assert "Challenge one" in body
    assert "Use code ABC-123" in body
    assert "[Image: GHW Logo]" in body
    assert "[Image: Logo]" not in body
    assert "trackingSecret" not in body
    assert "\n\n\n" not in body


def test_gmail_html_image_links_use_labels_without_tracking_destinations(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def json(self):
            html = """
                <p>Before the announcement.</p>
                <a href="https://tracking.example.test/click?token=synthetic">
                  <img alt="GHW Logo">
                </a>
                <p><a href="https://example.test/schedule">View the schedule</a></p>
                <p>After the announcement.</p>
            """
            return {
                "id": "image-link-message",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
                },
            }

    monkeypatch.setattr(gmail_module.requests, "get", lambda *args, **kwargs: FakeResponse())
    result = gmail_module.retrieve_inbox_message(
        FakeCredentials(), message_id="image-link-message"
    )

    assert result["body"] == (
        "Before the announcement.\n\n[Image: GHW Logo]\n\n"
        "View the schedule\n\nAfter the announcement."
    )
    assert result["body_content"][1]["children"] == [{
        "type": "text",
        "text": "[Image: GHW Logo]",
    }]
    assert result["body_content"][2]["children"] == [{
        "type": "link",
        "text": "View the schedule",
        "href": "https://example.test/schedule",
    }]


def test_gmail_plain_text_cleanup_preserves_urls_codes_and_paragraphs(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def json(self):
            plain = (
                "Hello   team.\n\n\n"
                "Visit https://example.test/docs for details.\n"
                "Use code ABC-123  to continue.\n\n"
                "Thanks."
            )
            return {
                "id": "message-plain",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(plain.encode()).decode(),
                    },
                },
            }

    monkeypatch.setattr(gmail_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    body = gmail_module.retrieve_inbox_message(
        FakeCredentials(),
        message_id="message-plain",
    )["body"]

    assert "Hello team." in body
    assert "https://example.test/docs" in body
    assert "ABC-123" in body
    assert "\n\n\n" not in body


def test_gmail_html_returns_safe_structured_links_and_formatting(monkeypatch):
    class FakeCredentials:
        valid = True
        token = "access-token"
        refresh_token = None

    class FakeResponse:
        status_code = 200

        def json(self):
            html = """
                <html><head><style>.hidden { display:none }</style></head>
                <body>
                  <h2>Schedule</h2>
                  <p><a href="https://tracking.example.test/click?utm_source=synthetic">
                    View today's schedule
                  </a></p>
                  <p><a href="javascript:alert(1)">Unsafe label</a></p>
                  <p><a href="https://example.test/image"><img alt="Logo"></a></p>
                  <ul><li>First item</li><li><strong>Second</strong> item</li></ul>
                  <script>secret()</script>
                </body></html>
            """
            return {
                "id": "structured-message",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
                },
            }

    monkeypatch.setattr(gmail_module.requests, "get", lambda *args, **kwargs: FakeResponse())
    result = gmail_module.retrieve_inbox_message(
        FakeCredentials(), message_id="structured-message"
    )

    assert result["body"] == (
        "Schedule\n\nView today's schedule\n\nUnsafe label\n\n"
        "First item\n\nSecond item"
    )
    blocks = result["body_content"]
    assert blocks[0] == {
        "type": "heading",
        "level": 2,
        "children": [{"type": "text", "text": "Schedule"}],
    }
    assert blocks[1]["children"] == [{
        "type": "link",
        "text": "View today's schedule",
        "href": "https://tracking.example.test/click?utm_source=synthetic",
    }]
    assert blocks[2]["children"] == [{"type": "text", "text": "Unsafe label"}]
    assert [block["type"] for block in blocks[3:]] == ["list_item", "list_item"]
    assert "secret" not in result["body"]


def test_gmail_message_detail_route_requires_connection_and_maps_failures(monkeypatch):
    disconnected = client.get("/api/gmail/messages/message-1")
    assert disconnected.status_code == 401

    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    monkeypatch.setattr(
        main_module,
        "retrieve_inbox_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GmailMessageNotFound("Inbox message not found.")
        ),
    )

    response = client.get("/api/gmail/messages/message-1")
    assert response.status_code == 404
    assert response.json() == {"detail": "Inbox message not found."}


def test_gmail_summary_returns_local_model_result_for_selected_message(monkeypatch):
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    monkeypatch.setattr(
        main_module,
        "retrieve_inbox_message",
        lambda *args, **kwargs: {"id": "message-1", "body": "Synthetic email body."},
    )
    monkeypatch.setattr(
        main_module,
        "summarize_email",
        lambda body, settings: {
            "gist": "The email shares a schedule.",
            "key_points": ["The schedule is available."],
            "action_items": ["Review the schedule."],
        },
    )

    response = client.post("/api/gmail/messages/message-1/summarize")

    assert response.status_code == 200
    assert response.json() == {
        "message_id": "message-1",
        "gist": "The email shares a schedule.",
        "key_points": ["The schedule is available."],
        "action_items": ["Review the schedule."],
    }


def test_gmail_summary_requires_connection_and_inbox_message(monkeypatch):
    assert client.post("/api/gmail/messages/message-1/summarize").status_code == 401
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    monkeypatch.setattr(
        main_module,
        "retrieve_inbox_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GmailMessageNotFound("Inbox message not found.")
        ),
    )

    response = client.post("/api/gmail/messages/sent-message/summarize")

    assert response.status_code == 404
    assert response.json() == {"detail": "Inbox message not found."}


def test_gmail_summary_maps_empty_model_input_and_local_failures(monkeypatch):
    session_id = store.create_session()
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)
    monkeypatch.setattr(
        main_module,
        "retrieve_inbox_message",
        lambda *args, **kwargs: {"id": "message-1", "body": ""},
    )
    monkeypatch.setattr(
        main_module,
        "summarize_email",
        lambda body, settings: (_ for _ in ()).throw(
            SummarizationInputError("This message has no readable content to summarize.")
        ),
    )
    empty_response = client.post("/api/gmail/messages/message-1/summarize")
    assert empty_response.status_code == 422

    monkeypatch.setattr(
        main_module,
        "summarize_email",
        lambda body, settings: (_ for _ in ()).throw(
            SummarizationUnavailable("The local summarizer is unavailable.")
        ),
    )
    unavailable_response = client.post("/api/gmail/messages/message-1/summarize")
    assert unavailable_response.status_code == 503
    assert unavailable_response.json() == {
        "detail": "The local summarizer is unavailable."
    }


def test_local_summarizer_posts_only_bounded_email_text_and_parses_response(monkeypatch):
    settings = Settings(app_env="test", llama_cpp_base_url="http://127.0.0.1:8080")
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"gist":"A synthetic update.","key_points":["One point"],'
                            '"action_items":["Review it"]}'
                        )
                    }
                }]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr("backend.app.summarizer.requests.post", fake_post)
    result = summarize_email("Synthetic email body.", settings)

    assert result == {
        "gist": "A synthetic update.",
        "key_points": ["One point"],
        "action_items": ["Review it"],
    }
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert "Synthetic email body." in captured["payload"]["messages"][1]["content"]
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["payload"]["reasoning_format"] == "none"


def test_local_summarizer_keeps_long_email_within_context_budget(monkeypatch):
    settings = Settings(app_env="test")
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"gist":"A long update.","key_points":[],"action_items":[]}'
                        )
                    }
                }]
            }

    def fake_post(url, **kwargs):
        if url.endswith("/tokenize"):
            content = kwargs["json"]["content"]
            return type(
                "TokenResponse",
                (),
                {"status_code": 200, "json": lambda self: {"tokens": [0] * (len(content) // 4)}},
            )()
        captured["prompt"] = kwargs["json"]["messages"][1]["content"]
        email = captured["prompt"].split("EMAIL:\n", 1)[1]
        assert len(email) < 20000
        assert "[...content shortened...]" in email
        return FakeResponse()

    monkeypatch.setattr("backend.app.summarizer.requests.post", fake_post)

    result = summarize_email("word " * 4000, settings)

    assert result == {
        "gist": "A long update.",
        "key_points": [],
        "action_items": [],
    }


def test_local_summarizer_parses_fenced_json(monkeypatch):
    settings = Settings(app_env="test")

    class FakeResponse:
        status_code = 200
        content = b'{"choices":[{"message":{"content":"fenced"}}]}'

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": (
                            "```json\n"
                            '{"gist":"A fenced update.","key_points":["One point"],'
                            '"action_items":[]}\n'
                            "```"
                        )
                    }
                }]
            }

    monkeypatch.setattr(
        "backend.app.summarizer.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    assert summarize_email("Synthetic email body.", settings) == {
        "gist": "A fenced update.",
        "key_points": ["One point"],
        "action_items": [],
    }


def test_local_summarizer_rejects_more_than_three_items(monkeypatch):
    settings = Settings(app_env="test")

    class FakeResponse:
        status_code = 200
        content = b'{"choices":[{"message":{"content":"invalid"}}]}'

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"gist":"Too many points.","key_points":["1","2","3","4"],'
                            '"action_items":[]}'
                        )
                    }
                }]
            }

    monkeypatch.setattr(
        "backend.app.summarizer.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    with pytest.raises(SummarizationUnavailable, match="incomplete"):
        summarize_email("Synthetic email body.", settings)


@pytest.mark.parametrize("content", ["", '{"gist":"truncated"', "not json"])
def test_local_summarizer_rejects_malformed_or_truncated_json(monkeypatch, content):
    settings = Settings(app_env="test")

    class FakeResponse:
        status_code = 200

        def __init__(self):
            self.content = content.encode()

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(
        "backend.app.summarizer.requests.post",
        lambda url, **kwargs: (
            type(
                "TokenResponse",
                (),
                {"status_code": 200, "json": lambda self: {"tokens": [0] * 10}},
            )()
            if url.endswith("/tokenize")
            else FakeResponse()
        ),
    )

    with pytest.raises(SummarizationUnavailable, match="invalid response"):
        summarize_email("Synthetic email body.", settings)


def test_local_summarizer_maps_http_error(monkeypatch):
    settings = Settings(app_env="test")

    class FakeResponse:
        status_code = 400
        content = b'{"error":"context"}'

        def json(self):
            return {"tokens": [0] * 10}

    def fake_post(url, **kwargs):
        if url.endswith("/tokenize"):
            return FakeResponse()
        return FakeResponse()

    monkeypatch.setattr("backend.app.summarizer.requests.post", fake_post)

    with pytest.raises(SummarizationUnavailable) as raised:
        summarize_email("Synthetic email body.", settings)

    assert raised.value.status_code == 400
    assert raised.value.response_length == len(FakeResponse.content)


def test_local_summarizer_distinguishes_invalid_message_content_json(monkeypatch):
    settings = Settings(app_env="test")

    class FakeResponse:
        status_code = 200
        content = b'{"choices":[{"message":{"content":"{\\"gist\\":"}}]}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": '{"gist":"truncated'},
                }]
            }

    monkeypatch.setattr(
        "backend.app.summarizer.requests.post",
        lambda url, **kwargs: (
            type(
                "TokenResponse",
                (),
                {"status_code": 200, "json": lambda self: {"tokens": [0] * 10}},
            )()
            if url.endswith("/tokenize")
            else FakeResponse()
        ),
    )

    with pytest.raises(SummarizationUnavailable) as raised:
        summarize_email("Synthetic Arcade notice.", settings)

    assert raised.value.json_parsed is True
    assert raised.value.parse_stage == "message_content"
    assert raised.value.finish_reason == "length"
    assert raised.value.generated_content_length == len('{"gist":"truncated')


def test_local_summarizer_retries_truncated_output_with_compact_prompt(monkeypatch):
    settings = Settings(app_env="test")
    responses = iter([
        {
            "finish_reason": "length",
            "content": '{"gist":"truncated',
        },
        {
            "finish_reason": "stop",
            "content": (
                '{"gist":"A concise update.","key_points":["One point"],'
                '"action_items":[]}'
            ),
        },
    ])
    prompts = []

    def fake_post(url, **kwargs):
        if url.endswith("/tokenize"):
            return type(
                "TokenResponse",
                (),
                {"status_code": 200, "json": lambda self: {"tokens": [0] * 10}},
            )()
        response = next(responses)
        prompts.append(kwargs["json"]["messages"][1]["content"])
        return type(
            "ModelResponse",
            (),
            {
                "status_code": 200,
                "content": response["content"].encode(),
                "headers": {"content-type": "application/json"},
                "json": lambda self: {
                    "choices": [{
                        "finish_reason": response["finish_reason"],
                        "message": {"content": response["content"]},
                    }]
                },
            },
        )()

    monkeypatch.setattr("backend.app.summarizer.requests.post", fake_post)

    result = summarize_email("Synthetic newsletter body.", settings)

    assert result["gist"] == "A concise update."
    assert len(prompts) == 2
    assert "Return only compact valid JSON" in prompts[1]


def test_local_summarizer_rejects_exhausted_truncation_retry(monkeypatch):
    settings = Settings(app_env="test")
    calls = 0

    def fake_post(url, **kwargs):
        nonlocal calls
        if url.endswith("/tokenize"):
            return type(
                "TokenResponse",
                (),
                {"status_code": 200, "json": lambda self: {"tokens": [0] * 10}},
            )()
        calls += 1
        content = '{"gist":"still truncated'
        return type(
            "ModelResponse",
            (),
            {
                "status_code": 200,
                "content": content.encode(),
                "headers": {"content-type": "application/json"},
                "json": lambda self: {
                    "choices": [{
                        "finish_reason": "length",
                        "message": {"content": content},
                    }]
                },
            },
        )()

    monkeypatch.setattr("backend.app.summarizer.requests.post", fake_post)

    with pytest.raises(SummarizationUnavailable) as raised:
        summarize_email("Synthetic newsletter body.", settings)

    assert calls == 2
    assert raised.value.finish_reason == "length"


def test_local_summarizer_maps_timeout_without_exposing_email(monkeypatch):
    settings = Settings(app_env="test")

    def fake_post(url, **kwargs):
        raise requests.Timeout("synthetic timeout")

    monkeypatch.setattr("backend.app.summarizer.requests.post", fake_post)

    with pytest.raises(SummarizationUnavailable, match="took too long"):
        summarize_email("Synthetic private body.", settings)


def test_gmail_authorize_refuses_missing_configuration(monkeypatch):
    unconfigured = Settings(
        app_env="test",
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_oauth_redirect_uri=None,
        session_secret=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: unconfigured)
    response = client.get("/api/gmail/authorize", follow_redirects=False)

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_gmail_authorize_sets_session_and_uses_pkce(monkeypatch):
    settings = Settings(
        google_oauth_client_id="client-id",
        google_oauth_client_secret="client-secret",
        google_oauth_redirect_uri="http://localhost/callback",
        session_secret="session-secret",
    )

    class FakeFlow:
        code_verifier = "verifier"

        def authorization_url(self, **kwargs):
            assert kwargs["code_challenge_method"] == "S256"
            return "https://accounts.google.com/o/oauth2/auth?state=state", "state"

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "build_flow", lambda settings: FakeFlow())

    response = client.get("/api/gmail/authorize", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://accounts.google.com/o/oauth2/auth?state=state"
    assert "talkie_mail_session=" in response.headers["set-cookie"]
    session_id = response.cookies["talkie_mail_session"]
    attempt = store._sessions[session_id].attempt
    assert attempt is not None
    assert attempt.state == "state"
    assert attempt.code_verifier == "verifier"


def test_gmail_callback_rejects_missing_or_invalid_state():
    response = client.get(
        "/api/gmail/callback?state=not-valid&code=not-used",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?oauth=error#settings"


def test_gmail_denial_with_valid_state_disconnects_current_session():
    session_id = store.create_session()
    store.save_attempt(
        session_id,
        OAuthAttempt(state="valid-state", code_verifier="verifier", created_at=time.time()),
    )
    store.set_credentials(session_id, object())
    client.cookies.set("talkie_mail_session", session_id)

    response = client.get(
        "/api/gmail/callback?state=valid-state&error=access_denied",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?oauth=denied#settings"
    assert store.credentials(session_id) is None


def test_gmail_denial_with_invalid_state_preserves_current_connection():
    session_id = store.create_session()
    store.save_attempt(
        session_id,
        OAuthAttempt(state="valid-state", code_verifier="verifier", created_at=time.time()),
    )
    credentials = object()
    store.set_credentials(session_id, credentials)
    client.cookies.set("talkie_mail_session", session_id)

    response = client.get(
        "/api/gmail/callback?state=wrong-state&error=access_denied",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?oauth=error#settings"
    assert store.credentials(session_id) is credentials


def test_disconnect_rejects_missing_or_invalid_csrf_token():
    session_id = store.create_session()
    client.cookies.set("talkie_mail_session", session_id)

    missing = client.post("/api/gmail/disconnect")
    invalid = client.post(
        "/api/gmail/disconnect",
        headers={"X-CSRF-Token": "wrong-token"},
    )

    assert missing.status_code == 403
    assert invalid.status_code == 403


def test_disconnect_accepts_session_csrf_token():
    session_id = store.create_session()
    csrf_token = store.csrf_token(session_id)
    client.cookies.set("talkie_mail_session", session_id)

    response = client.post(
        "/api/gmail/disconnect",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    assert response.json() == {"connected": False}


def test_production_rejects_insecure_session_cookie_configuration():
    with pytest.raises(ValidationError):
        Settings(app_env="production", session_cookie_secure=False)


def test_local_session_cookie_can_be_insecure_but_production_cookie_is_secure(
    monkeypatch,
):
    class FakeFlow:
        code_verifier = "verifier"

        def authorization_url(self, **kwargs):
            return "https://accounts.google.com/auth", "state"

    base = {
        "google_oauth_client_id": "client-id",
        "google_oauth_client_secret": "client-secret",
        "google_oauth_redirect_uri": "http://localhost/callback",
        "session_secret": "session-secret",
    }
    monkeypatch.setattr(main_module, "build_flow", lambda settings: FakeFlow())

    local_settings = Settings(**base, app_env="development", session_cookie_secure=False)
    monkeypatch.setattr(main_module, "get_settings", lambda: local_settings)
    local_response = client.get("/api/gmail/authorize", follow_redirects=False)
    assert " Secure" not in local_response.headers["set-cookie"]

    store.clear()
    client.cookies.clear()
    production_base = {
        **base,
        "google_oauth_redirect_uri": "https://example.test/api/gmail/callback",
    }
    production_settings = Settings(
        **production_base,
        app_env="production",
        session_cookie_secure=True,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: production_settings)
    production_response = client.get("/api/gmail/authorize", follow_redirects=False)
    assert " Secure" in production_response.headers["set-cookie"]


def test_oauth_configuration_requires_all_server_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost/callback")
    monkeypatch.setenv("SESSION_SECRET", "session-secret")
    get_settings.cache_clear()

    assert Settings().google_oauth_configured is True


def test_disconnect_does_not_expose_or_return_credentials():
    response = client.post("/api/gmail/disconnect")

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid CSRF token."}
    assert "client-secret" not in response.text
