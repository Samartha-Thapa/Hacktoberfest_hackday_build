from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import requests
from google.auth.credentials import Credentials
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest

GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_REQUEST_TIMEOUT = 10
MESSAGE_HEADERS = ("From", "Subject", "Date")


class GmailAuthenticationError(Exception):
    """The stored Gmail credentials cannot authenticate a request."""


class GmailRateLimitError(Exception):
    """Gmail rejected a request because a rate limit was exceeded."""


class GmailServiceError(Exception):
    """Gmail returned an unexpected service or request error."""


class GmailMessageNotFound(Exception):
    """The requested message is not an Inbox message."""


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str | None
    sender: str
    subject: str
    date: str
    snippet: str


@dataclass
class _HTMLNode:
    tag: str
    attrs: dict[str, str]
    children: list["_HTMLNode | str"]


def _ensure_access_token(credentials: Credentials) -> str:
    if not credentials.valid:
        if not credentials.refresh_token:
            raise GmailAuthenticationError("Gmail authorization has expired.")
        try:
            credentials.refresh(GoogleRequest())
        except GoogleAuthError as error:
            raise GmailAuthenticationError("Gmail authorization has expired.") from error
    if not credentials.token:
        raise GmailAuthenticationError("Gmail authorization has expired.")
    return credentials.token


def _request(
    credentials: Credentials,
    url: str,
    *,
    params: dict[str, str | int | list[str]],
) -> dict[str, Any]:
    token = _ensure_access_token(credentials)
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=GMAIL_REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        raise GmailServiceError("Gmail is temporarily unavailable.") from error
    if response.status_code == 401:
        raise GmailAuthenticationError("Gmail authorization is no longer valid.")
    if response.status_code == 403:
        raise GmailServiceError("Gmail denied the inbox request.")
    if response.status_code == 429:
        raise GmailRateLimitError("Gmail is rate limiting requests.")
    if response.status_code >= 400:
        raise GmailServiceError("Gmail could not complete the request.")
    try:
        payload = response.json()
    except ValueError as error:
        raise GmailServiceError("Gmail returned an invalid response.") from error
    if not isinstance(payload, dict):
        raise GmailServiceError("Gmail returned an invalid response.")
    return payload


def _header_value(headers: list[dict[str, Any]], name: str) -> str:
    expected = name.casefold()
    for header in headers:
        if str(header.get("name", "")).casefold() == expected:
            value = header.get("value")
            return value if isinstance(value, str) else ""
    return ""


class _HTMLStructuredParser(HTMLParser):
    _BLOCK_TAGS = {
        "address", "article", "blockquote", "div", "dl", "dt", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "ol", "p", "pre", "section",
        "body", "html", "table", "td", "th", "tr", "ul",
    }
    _IGNORED_TAGS = {"head", "iframe", "noscript", "script", "style", "template", "svg"}
    _DECORATIVE_ALT = {
        "image", "logo", "icon", "spacer", "divider", "graphic", "bullet",
        "banner", "header image", "email image",
    }

    def __init__(self) -> None:
        super().__init__()
        self.root = _HTMLNode("root", {}, [])
        self._stack = [self.root]
        self._ignored_depth = 0

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self._stack[-1].children.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if normalized_tag == "img":
            self._stack[-1].children.append(_HTMLNode("img", dict(attrs), []))
            return
        node = _HTMLNode(normalized_tag, dict(attrs), [])
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    @classmethod
    def _meaningful_alt(cls, alt: str) -> bool:
        normalized = _normalize_inline_text(alt).casefold()
        if not normalized or normalized in cls._DECORATIVE_ALT:
            return False
        return not any(
            re.search(rf"\b{re.escape(marker)}\b", normalized)
            for marker in ("banner", "divider", "icon", "spacer")
        )

    def content(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        inline: list[dict[str, str]] = []

        def add_text(value: str) -> None:
            value = _normalize_inline_text(value.replace("\u200b", "").replace("\ufeff", ""))
            value = _remove_tracking_url_noise(value)
            if value:
                if inline and inline[-1]["type"] == "text":
                    inline[-1]["text"] += f" {value}"
                else:
                    inline.append({"type": "text", "text": value})

        def render_inline(node: _HTMLNode | str) -> None:
            if isinstance(node, str):
                add_text(node)
                return
            if node.tag == "a":
                label = _normalize_inline_text(_node_text(node))
                href = node.attrs.get("href", "").strip()
                if label and _is_safe_http_url(href):
                    inline.append({"type": "link", "text": label, "href": href})
                elif label:
                    add_text(label)
                else:
                    for image_label in _image_labels(node):
                        add_text(f"[Image: {image_label}]")
                return
            if node.tag == "img":
                alt = node.attrs.get("alt", "")
                if self._meaningful_alt(alt):
                    add_text(f"[Image: {_normalize_inline_text(alt)}]")
                return
            for child in node.children:
                render_inline(child)

        def flush() -> None:
            if inline:
                blocks.append({"type": "paragraph", "children": inline.copy()})
                inline.clear()

        def visit(node: _HTMLNode | str) -> None:
            if isinstance(node, str):
                add_text(node)
                return
            if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                flush()
                render_inline_children = []
                for child in node.children:
                    render_inline_children.append(child)
                before = len(inline)
                for child in render_inline_children:
                    render_inline(child)
                if len(inline) > before:
                    blocks.append({"type": "heading", "level": int(node.tag[1]), "children": inline.copy()})
                    inline.clear()
                return
            if node.tag == "li":
                flush()
                for child in node.children:
                    render_inline(child)
                if inline:
                    blocks.append({"type": "list_item", "children": inline.copy()})
                    inline.clear()
                return
            if node.tag in self._BLOCK_TAGS:
                flush()
                for child in node.children:
                    if isinstance(child, _HTMLNode) and (
                        child.tag in self._BLOCK_TAGS or child.tag == "li"
                    ):
                        visit(child)
                    else:
                        render_inline(child)
                flush()
                return
            for child in node.children:
                render_inline(child)

        for child in self.root.children:
            visit(child)
        flush()
        return blocks

    def text(self) -> str:
        lines: list[str] = []
        for block in self.content():
            text = " ".join(
                child["text"] for child in block["children"] if child["type"] in {"text", "link"}
            )
            if text:
                lines.append(text)
        return _normalize_readable_text("\n\n".join(lines))


def _normalize_inline_text(value: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", value).strip()


def _normalize_readable_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_normalize_inline_text(line) for line in value.split("\n")]
    output: list[str] = []
    blank_pending = False
    for line in lines:
        if line:
            if blank_pending and output:
                output.append("")
            output.append(line)
            blank_pending = False
        elif output:
            blank_pending = True
    return "\n".join(output).strip()


def _remove_tracking_url_noise(value: str) -> str:
    tokens = value.split(" ")
    kept: list[str] = []
    for token in tokens:
        candidate = token.rstrip(".,;:!?)]}")
        if candidate.startswith(("http://", "https://")) and not _is_safe_visible_url(candidate):
            continue
        kept.append(token)
    return " ".join(kept)


def _is_safe_http_url(value: str) -> bool:
    if any(character in value for character in ("\r", "\n", "\x00")):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _is_safe_visible_url(value: str) -> bool:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        return False
    lowered = normalized.casefold()
    if any(marker in lowered for marker in ("utm_", "upn=", "click.", "tracking.", "trk.")):
        return False
    return _is_safe_http_url(normalized)


def _node_text(node: _HTMLNode) -> str:
    return "".join(
        child if isinstance(child, str) else _node_text(child)
        for child in node.children
        if not (
            isinstance(child, _HTMLNode)
            and child.tag in (_HTMLStructuredParser._IGNORED_TAGS | {"img", "a"})
        )
    )


def _image_labels(node: _HTMLNode) -> list[str]:
    labels: list[str] = []
    if node.tag == "img":
        alt = _normalize_inline_text(node.attrs.get("alt", ""))
        if alt and _HTMLStructuredParser._meaningful_alt(alt):
            labels.append(alt)
    for child in node.children:
        if isinstance(child, _HTMLNode):
            labels.extend(_image_labels(child))
    return labels


def _decode_body(data: Any) -> str:
    if not isinstance(data, str) or not data:
        return ""
    try:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except (binascii.Error, UnicodeError):
        return ""


def _body_parts(payload: dict[str, Any]) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        filename = part.get("filename")
        body = part.get("body")
        if filename:
            return
        if isinstance(body, dict) and not body.get("attachmentId"):
            mime_type = part.get("mimeType")
            data = _decode_body(body.get("data"))
            if mime_type == "text/plain" and data:
                plain_parts.append(data)
            elif mime_type == "text/html" and data:
                html_parts.append(data)
        for child in part.get("parts", []):
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    return "\n\n".join(plain_parts).strip(), "\n\n".join(html_parts).strip()


def _readable_body(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]] | None]:
    plain_body, html_body = _body_parts(payload)
    if plain_body:
        return _normalize_readable_text(plain_body), None
    if not html_body:
        return "", []
    parser = _HTMLStructuredParser()
    parser.feed(html_body)
    parser.close()
    return parser.text(), parser.content()


def retrieve_inbox_messages(
    credentials: Credentials,
    *,
    max_results: int,
    page_token: str | None,
) -> dict[str, Any]:
    list_params: dict[str, str | int] = {
        "userId": "me",
        "labelIds": "INBOX",
        "maxResults": max_results,
    }
    if page_token:
        list_params["pageToken"] = page_token
    listing = _request(credentials, GMAIL_MESSAGES_URL, params=list_params)
    messages: list[dict[str, Any]] = []
    for listed_message in listing.get("messages", []):
        message_id = listed_message.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        metadata = _request(
            credentials,
            f"{GMAIL_MESSAGES_URL}/{message_id}",
            params={
                "format": "metadata",
                "metadataHeaders": list(MESSAGE_HEADERS),
            },
        )
        payload = metadata.get("payload")
        headers = payload.get("headers", []) if isinstance(payload, dict) else []
        if not isinstance(headers, list):
            headers = []
        messages.append(
            {
                "id": message_id,
                "thread_id": metadata.get("threadId"),
                "from": _header_value(headers, "From"),
                "subject": _header_value(headers, "Subject"),
                "date": _header_value(headers, "Date"),
                "snippet": metadata.get("snippet", "")
                if isinstance(metadata.get("snippet", ""), str)
                else "",
            }
        )
    return {
        "messages": messages,
        "next_page_token": listing.get("nextPageToken"),
        "result_size_estimate": listing.get("resultSizeEstimate"),
    }


def retrieve_inbox_message(
    credentials: Credentials,
    *,
    message_id: str,
) -> dict[str, str | None]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", message_id):
        raise GmailMessageNotFound("Inbox message not found.")
    metadata = _request(
        credentials,
        f"{GMAIL_MESSAGES_URL}/{message_id}",
        params={
            "format": "full",
            "fields": "id,threadId,labelIds,payload",
        },
    )
    labels = metadata.get("labelIds")
    if not isinstance(labels, list) or "INBOX" not in labels:
        raise GmailMessageNotFound("Inbox message not found.")
    payload = metadata.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    headers = payload.get("headers", [])
    if not isinstance(headers, list):
        headers = []
    body, body_content = _readable_body(payload)
    result: dict[str, Any] = {
        "id": message_id,
        "thread_id": metadata.get("threadId")
        if isinstance(metadata.get("threadId"), str)
        else None,
        "from": _header_value(headers, "From"),
        "subject": _header_value(headers, "Subject"),
        "date": _header_value(headers, "Date"),
        "body": body,
    }
    if body_content is not None:
        result["body_content"] = body_content
    return result
