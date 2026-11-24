"""OpenRouter LLM client. Used only for invoice extraction and anomaly explanation.

OpenAI SDK pointed at OpenRouter, wrapped with instructor for validated structured output.
Token usage, cost and retry count are captured per call via `track_usage()`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import instructor
from openai import OpenAI

from ap_agent.config import get_settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_SECONDS = 60.0

# Mode preference for OpenRouter: TOOLS -> JSON_SCHEMA -> JSON. TOOLS is the default; the right
# value depends on the configured model, so it is overridable via get_client(mode=...).
LLM_MODE = instructor.Mode.TOOLS

# Ask OpenRouter to include usage accounting (tokens and cost) in every response.
USAGE_EXTRA_BODY: dict[str, Any] = {"usage": {"include": True}}


@dataclass
class LLMUsage:
    """Accumulated over every attempt (including validation retries) of one call."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None  # None when OpenRouter did not return a cost
    attempts: int = 0
    failed_attempts: int = 0

    @property
    def retries(self) -> int:
        return max(self.attempts - 1, 0)

    def add_response(self, response: Any) -> None:
        self.attempts += 1
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.tokens_in += getattr(usage, "prompt_tokens", 0) or 0
        self.tokens_out += getattr(usage, "completion_tokens", 0) or 0
        cost = getattr(usage, "cost", None)
        if cost is None:
            extra = getattr(usage, "model_extra", None) or {}
            cost = extra.get("cost")
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + float(cost)


_current: ContextVar[LLMUsage | None] = ContextVar("llm_usage", default=None)


@contextmanager
def track_usage() -> Iterator[LLMUsage]:
    """Collect usage for all LLM calls made inside the `with` block."""
    usage = LLMUsage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


def _on_response(response: Any) -> None:
    usage = _current.get()
    if usage is not None:
        usage.add_response(response)


def _on_parse_error(_error: Exception) -> None:
    usage = _current.get()
    if usage is not None:
        usage.failed_attempts += 1


def build_client(
    api_key: str, mode: instructor.Mode = LLM_MODE, base_url: str = OPENROUTER_BASE_URL
) -> instructor.Instructor:
    client = instructor.from_openai(
        OpenAI(base_url=base_url, api_key=api_key, timeout=TIMEOUT_SECONDS), mode=mode
    )
    client.on("completion:response", _on_response)
    client.on("parse:error", _on_parse_error)
    return client


@lru_cache
def get_client() -> instructor.Instructor:
    """Instructor client for OpenRouter. The key comes from config only."""
    return build_client(get_settings().OPENROUTER_API_KEY.get_secret_value())
