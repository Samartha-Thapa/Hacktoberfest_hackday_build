from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from .config import Settings


class SummarizationUnavailable(Exception):
    """The configured local model could not produce a summary."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_length: int | None = None,
        json_parsed: bool = False,
        parse_stage: str | None = None,
        finish_reason: str | None = None,
        generated_content_length: int | None = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_length = response_length
        self.json_parsed = json_parsed
        self.parse_stage = parse_stage
        self.finish_reason = finish_reason
        self.generated_content_length = generated_content_length
        self.content_type = content_type


class SummarizationInputError(Exception):
    """The selected email cannot be summarized."""


# Keep the prompt safely below the local model's 2048-token context after
# accounting for the instruction and requested output.
SUMMARY_PROMPT = (
    "Summarize the email below for the recipient. Return only valid JSON with exactly "
    'these keys: "gist" (2-3 concise sentences), "key_points" (up to 3 strings), '
    '"action_items" (an array of evidence-based action items, or an empty array). '
    "Do not invent facts or actions. Do not include markdown fences.\n\n"
)
SUMMARY_RETRY_PROMPT = (
    "Return only compact valid JSON with exactly three keys: "
    '"gist" (1-2 sentences), "key_points" (at most 3 short strings), '
    '"action_items" (at most 3 short strings, or []). '
    "Use only facts in this email. Do not explain your answer.\n\n"
)
SUMMARY_CONTEXT_SAFETY_TOKENS = 64
SUMMARY_FALLBACK_INPUT = 3000
MARKDOWN_JSON_FENCE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<content>.*?)\n```\s*$",
    re.IGNORECASE | re.DOTALL,
)
logger = logging.getLogger(__name__)


def validate_summary_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    gist = value.get("gist")
    key_points = value.get("key_points")
    action_items = value.get("action_items")
    if (
        not isinstance(gist, str)
        or not gist.strip()
        or not isinstance(key_points, list)
        or len(key_points) > 3
        or not all(isinstance(item, str) and item.strip() for item in key_points)
        or not isinstance(action_items, list)
        or len(action_items) > 3
        or not all(isinstance(item, str) and item.strip() for item in action_items)
    ):
        return None
    return {
        "gist": gist.strip(),
        "key_points": [item.strip() for item in key_points],
        "action_items": [item.strip() for item in action_items],
    }


def _parse_model_json(content: str) -> Any:
    content = content.strip()
    fenced = MARKDOWN_JSON_FENCE.match(content)
    if fenced:
        content = fenced.group("content")
    elif content.startswith("```") and content.endswith("```"):
        content = content.split("\n", 1)[-1][:-3].strip()
    if not content.startswith("{"):
        content = content[content.find("{") :]
    return json.loads(content)


def _prompt(body: str, instruction: str = SUMMARY_PROMPT) -> str:
    return f"{instruction}EMAIL:\n{body}"


def _shorten_body(body: str, token_count: int, token_budget: int) -> str:
    if token_count <= token_budget:
        return body
    target_chars = max(1, int(len(body) * token_budget / token_count))
    head_chars = max(1, int(target_chars * 0.75))
    tail_chars = max(1, target_chars - head_chars)
    return f"{body[:head_chars]}\n\n[...content shortened...]\n\n{body[-tail_chars:]}"


def _fit_body_to_context(
    body: str,
    settings: Settings,
    *,
    instruction: str = SUMMARY_PROMPT,
) -> str:
    token_budget = (
        settings.llama_cpp_context_size
        - settings.llama_cpp_max_tokens
        - SUMMARY_CONTEXT_SAFETY_TOKENS
    )
    if token_budget <= 0:
        raise SummarizationUnavailable("The local summarizer has no available context budget.")
    try:
        response = requests.post(
            f"{settings.llama_cpp_base_url.rstrip('/')}/tokenize",
            json={"content": _prompt(body, instruction)},
            timeout=min(settings.llama_cpp_timeout, 5.0),
        )
        if response.status_code < 400:
            payload = response.json()
            tokens = payload.get("tokens")
            if isinstance(tokens, list) and all(isinstance(token, int) for token in tokens):
                shortened = _shorten_body(body, len(tokens), token_budget)
                if shortened == body:
                    return body
                for _ in range(4):
                    shortened_response = requests.post(
                        f"{settings.llama_cpp_base_url.rstrip('/')}/tokenize",
                        json={"content": _prompt(shortened, instruction)},
                        timeout=min(settings.llama_cpp_timeout, 5.0),
                    )
                    if shortened_response.status_code >= 400:
                        break
                    shortened_payload = shortened_response.json()
                    shortened_tokens = shortened_payload.get("tokens")
                    if not isinstance(shortened_tokens, list):
                        break
                    if len(shortened_tokens) <= token_budget:
                        return shortened
                    shortened = _shorten_body(
                        shortened,
                        len(shortened_tokens),
                        token_budget,
                    )
    except (requests.RequestException, ValueError, TypeError):
        pass
    head = body[: SUMMARY_FALLBACK_INPUT * 3 // 4]
    tail = body[-SUMMARY_FALLBACK_INPUT // 4 :]
    return f"{head}\n\n[...content shortened...]\n\n{tail}"


def _request_summary(
    body: str,
    settings: Settings,
    *,
    instruction: str,
) -> tuple[dict[str, Any], str | None]:
    prompt = _prompt(
        _fit_body_to_context(body, settings, instruction=instruction),
        instruction,
    )
    try:
        response = requests.post(
            f"{settings.llama_cpp_base_url.rstrip('/')}/v1/chat/completions",
            json={
                "model": settings.llama_cpp_model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You produce concise, evidence-based email summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": settings.llama_cpp_max_tokens,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
                "reasoning_format": "none",
            },
            timeout=settings.llama_cpp_timeout,
        )
    except requests.Timeout as error:
        raise SummarizationUnavailable("The local summarizer took too long to respond.") from error
    except requests.RequestException as error:
        raise SummarizationUnavailable("The local summarizer is unavailable.") from error

    response_length = len(getattr(response, "content", b""))
    content_type = getattr(response, "headers", {}).get("content-type")
    if response.status_code >= 400:
        raise SummarizationUnavailable(
            "The local summarizer could not complete the request.",
            status_code=response.status_code,
            response_length=response_length,
        )
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise SummarizationUnavailable(
            "The local summarizer returned an invalid response.",
            status_code=response.status_code,
            response_length=response_length,
            json_parsed=False,
            parse_stage="http_response",
            content_type=content_type,
        ) from error
    try:
        content = payload["choices"][0]["message"]["content"]
        choice = payload["choices"][0]
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        generated_content_length = len(content) if isinstance(content, str) else None
    except (KeyError, IndexError, TypeError) as error:
        raise SummarizationUnavailable(
            "The local summarizer returned an invalid response.",
            status_code=response.status_code,
            response_length=response_length,
            json_parsed=False,
            parse_stage="response_fields",
            content_type=content_type,
        ) from error
    try:
        result = _parse_model_json(content) if isinstance(content, str) else content
    except (TypeError, ValueError) as error:
        raise SummarizationUnavailable(
            "The local summarizer returned an invalid response.",
            status_code=response.status_code,
            response_length=response_length,
            json_parsed=True,
            parse_stage="message_content",
            finish_reason=finish_reason,
            generated_content_length=generated_content_length,
            content_type=content_type,
        ) from error
    if not isinstance(result, dict):
        raise SummarizationUnavailable(
            "The local summarizer returned an invalid response.",
            status_code=response.status_code,
            response_length=response_length,
            json_parsed=True,
            parse_stage="message_content",
            finish_reason=finish_reason,
            generated_content_length=generated_content_length,
            content_type=content_type,
        )

    validated = validate_summary_result(result)
    if validated is None:
        raise SummarizationUnavailable(
            "The local summarizer returned incomplete results.",
            status_code=response.status_code,
            response_length=response_length,
            json_parsed=True,
            parse_stage="schema",
            finish_reason=finish_reason,
            generated_content_length=generated_content_length,
            content_type=content_type,
        )
    return validated, finish_reason


def summarize_email(body: str, settings: Settings) -> dict[str, Any]:
    if not body.strip():
        raise SummarizationInputError("This message has no readable content to summarize.")

    for attempt, instruction in enumerate((SUMMARY_PROMPT, SUMMARY_RETRY_PROMPT)):
        try:
            result, finish_reason = _request_summary(
                body,
                settings,
                instruction=instruction,
            )
        except SummarizationUnavailable as error:
            if attempt == 0 and error.parse_stage == "message_content":
                continue
            raise
        if finish_reason != "length":
            return result
        if attempt == 1:
            raise SummarizationUnavailable(
                "The local summarizer returned incomplete results.",
                status_code=200,
                json_parsed=True,
                parse_stage="message_content",
                finish_reason=finish_reason,
            )
    raise SummarizationUnavailable("The local summarizer returned an invalid response.")
