"""Thin wrapper around the Anthropic SDK.

Every caller has a deterministic fallback, so the app still works (with
less polished text) when no API key is configured or a call fails.
"""

from __future__ import annotations

import logging
from typing import TypeVar

import anthropic
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_anthropic
from pydantic import BaseModel

from app.config import get_settings
from app.observability import traceable

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLM:
    def __init__(self) -> None:
        s = get_settings()
        self.model = s.llm_model
        # wrap_anthropic traces messages.create (model, prompt, tokens) when tracing is on.
        self.client = wrap_anthropic(anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)) if s.anthropic_api_key else None

    @property
    def available(self) -> bool:
        return self.client is not None

    @traceable(run_type="llm", name="anthropic.messages.parse", process_inputs=lambda i: {
        "system": i.get("system"), "user": i.get("user"), "schema": getattr(i.get("schema"), "__name__", ""),
        "effort": i.get("effort"),
    })
    async def parse(
        self, system: str, user: str, schema: type[T], *, effort: str = "medium", max_tokens: int = 8000
    ) -> T | None:
        """Structured output validated against a Pydantic model. None on failure."""
        if not self.client:
            return None
        try:
            resp = await self.client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": effort},
            )
        except anthropic.APIError as exc:
            log.warning("LLM parse failed: %s", exc)
            return None
        _record_usage(self.model, resp)
        if resp.stop_reason != "end_turn":
            log.warning("LLM parse stopped with %s", resp.stop_reason)
            return None
        return resp.parsed_output

    async def text(
        self, system: str, messages: list[dict[str, str]], *, effort: str = "medium", max_tokens: int = 8000
    ) -> str | None:
        if not self.client:
            return None
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": effort},
            )
        except anthropic.APIError as exc:
            log.warning("LLM text failed: %s", exc)
            return None
        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip() or None


def _record_usage(model: str, resp: object) -> None:
    """Attach model + token usage to the current LangSmith span (no-op when not tracing)."""
    run = get_current_run_tree()
    usage = getattr(resp, "usage", None)
    if run is None or usage is None:
        return
    run.metadata.update({"ls_provider": "anthropic", "ls_model_name": model, "stop_reason": getattr(resp, "stop_reason", None)})
    inp, out = usage.input_tokens, usage.output_tokens
    run.metadata["usage_metadata"] = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm


def set_llm(llm: LLM | None) -> None:
    """Test hook."""
    global _llm
    _llm = llm
