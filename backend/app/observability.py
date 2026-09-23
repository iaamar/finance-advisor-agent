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

import asyncio
import logging
import os
import time
from typing import Any

from langsmith import traceable  # re-exported for the rest of the app

from app.config import get_settings

log = logging.getLogger(__name__)

__all__ = ["configure_tracing", "dashboard_links", "drop_self", "trace_url", "traceable", "tracing_enabled"]


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
        "metadata": {
            "thread_id": conversation_id,
            "conversation_id": conversation_id,
            "environment": get_settings().app_env,
            **metadata,
        },
    }


# ---- dashboard links -----------------------------------------------------------
# Resolved once (org + project IDs), then every trace URL is built locally.
_links: dict[str, str] | None = None
_links_failed_at = 0.0


def _resolve_links() -> dict[str, str]:
    from langsmith import Client

    client = Client()
    name = get_settings().langsmith_project
    try:
        project = client.read_project(project_name=name)
    except Exception:  # noqa: BLE001 - project is created on first trace; create it now
        project = client.create_project(name, upsert=True)
    host = client._host_url  # noqa: SLF001 - same host the SDK uses for its own links
    tenant = client._get_tenant_id()  # noqa: SLF001
    base = f"{host}/o/{tenant}/projects/p/{project.id}"
    return {"project": name, "project_url": base, "run_url_prefix": f"{base}/r/"}


async def dashboard_links() -> dict[str, str] | None:
    """LangSmith project URL (None when tracing is off or LangSmith is unreachable)."""
    global _links, _links_failed_at
    if not tracing_enabled():
        return None
    if _links is None and time.monotonic() - _links_failed_at > 300:
        try:
            _links = await asyncio.to_thread(_resolve_links)
        except Exception as exc:  # noqa: BLE001 - links are a convenience, never fail a request
            _links_failed_at = time.monotonic()
            log.warning("Could not resolve LangSmith dashboard links: %s", exc)
    return _links


async def trace_url(run_id: Any) -> str | None:
    links = await dashboard_links()
    return f"{links['run_url_prefix']}{run_id}?poll=true" if links else None
