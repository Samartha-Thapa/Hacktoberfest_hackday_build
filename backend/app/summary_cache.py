from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .config import Settings
from .summarizer import (
    SUMMARY_PROMPT,
    SUMMARY_RETRY_PROMPT,
    validate_summary_result,
)

logger = logging.getLogger(__name__)
CACHE_VERSION = "v1"
CACHE_PREFIX = "talkie-mail:summary"


class SummaryCache:
    """Best-effort Redis cache for validated summaries."""

    def __init__(self, settings: Settings) -> None:
        self._client: Any = None
        self._ttl = settings.redis_summary_cache_ttl
        if not settings.redis_url:
            return
        try:
            import redis

            self._client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.redis_connect_timeout,
                socket_timeout=settings.redis_socket_timeout,
                retry_on_timeout=False,
                decode_responses=True,
            )
        except Exception as error:
            logger.warning(
                "Summary cache disabled: operation=initialize exception_type=%s",
                type(error).__name__,
            )

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def key(self, session_id: str, message_id: str, body: str, settings: Settings) -> str:
        account_scope = self._digest({"session_id": session_id})
        content_fingerprint = self._digest(
            {
                "body": body,
                "summary_version": CACHE_VERSION,
                "prompt": [SUMMARY_PROMPT, SUMMARY_RETRY_PROMPT],
                "base_url": settings.llama_cpp_base_url,
                "model": settings.llama_cpp_model,
                "context_size": settings.llama_cpp_context_size,
                "max_tokens": settings.llama_cpp_max_tokens,
                "temperature": 0.2,
            }
        )
        message_scope = self._digest({"message_id": message_id})
        return f"{CACHE_PREFIX}:{CACHE_VERSION}:{account_scope}:{message_scope}:{content_fingerprint}"

    def get(self, key: str) -> dict[str, Any] | None:
        if self._client is None:
            return None
        try:
            raw = self._client.get(key)
            if raw is None:
                return None
            cached = json.loads(raw)
            validated = validate_summary_result(cached)
            if validated is None:
                self._delete(key)
            return validated
        except Exception as error:
            logger.warning(
                "Summary cache operation failed: operation=get exception_type=%s",
                type(error).__name__,
            )
            return None

    def set(self, key: str, summary: dict[str, Any]) -> None:
        if self._client is None:
            return
        validated = validate_summary_result(summary)
        if validated is None:
            return
        try:
            self._client.setex(key, self._ttl, json.dumps(validated, ensure_ascii=False))
        except Exception as error:
            logger.warning(
                "Summary cache operation failed: operation=set exception_type=%s",
                type(error).__name__,
            )

    def _delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception as error:
            logger.warning(
                "Summary cache operation failed: operation=delete exception_type=%s",
                type(error).__name__,
            )
