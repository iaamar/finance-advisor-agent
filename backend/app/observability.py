"""Observability: LangSmith tracing for every conversation turn.

When LANGSMITH_TRACING=true and LANGSMITH_API_KEY is set:
  * each /api/chat turn is one trace ("advisor_chat_turn") whose children are
    the LangGraph nodes, including both workflows and their parallel branches;
  * turns carry `thread_id` / `conversation_id` metadata, so LangSmith's
    Threads view groups a whole conversation together;
  * LLM calls (with token usage), EDGAR requests, quote-provider calls and
    per-filing summarization show up as their own spans.

With tracing off, `traceable` is a cheap pass-through and nothing is sent.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langsmith import traceable  # re-exported for the rest of the app

from app.config import get_settings

log = logging.getLogger(__name__)

__all__ = ["configure_tracing", "drop_self", "traceable", "tracing_enabled"]


def configure_tracing() -> bool:
    """Export LangSmith settings from .env into os.environ (where the SDK and
    LangGraph read them). Call once at startup, before the first request."""
    s = get_settings()
    enabled = s.langsmith_tracing and bool(s.langsmith_api_key)
    if s.langsmith_tracing and not s.langsmith_api_key:
        log.warning("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is empty; tracing disabled")
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    os.environ["LANGCHAIN_TRACING_V2"] = os.environ["LANGSMITH_TRACING"]  # older langchain-core readers
    if enabled:
        os.environ["LANGSMITH_API_KEY"] = s.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = s.langsmith_project
        if s.langsmith_endpoint:
            os.environ["LANGSMITH_ENDPOINT"] = s.langsmith_endpoint
        log.info("LangSmith tracing enabled (project=%s)", s.langsmith_project)
    return enabled


def tracing_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"


def drop_self(inputs: dict[str, Any]) -> dict[str, Any]:
    """process_inputs hook for traced methods: don't serialize `self`."""
    return {k: v for k, v in inputs.items() if k != "self"}


def turn_config(conversation_id: str, run_id: Any, **metadata: Any) -> dict[str, Any]:
    """RunnableConfig for one conversation turn."""
    return {
        "run_name": "advisor_chat_turn",
        "run_id": run_id,
        "tags": ["advisor-chat"],
        "metadata": {"thread_id": conversation_id, "conversation_id": conversation_id, **metadata},
    }
