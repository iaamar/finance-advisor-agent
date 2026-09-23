"""Tracing wiring: one trace per turn, grouped by conversation, with nested workflow/LLM spans."""

from unittest.mock import MagicMock

from langsmith import Client
from langsmith.run_helpers import tracing_context

from app import observability
from app.graphs.conversation import Route, chat
from app.llm import set_llm
from tests.conftest import FakeLLM


def _runs(client: MagicMock) -> list[dict]:
    runs = [c.kwargs for c in client.create_run.call_args_list]
    for call in client.batch_ingest_runs.call_args_list + client.multipart_ingest.call_args_list:
        runs += list(call.kwargs.get("create") or (call.args[0] if call.args else []))
    return runs


async def test_turn_trace_is_tagged_with_conversation_and_nests_workflows(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    async def fake_links():
        return {"project_url": "https://ls/o/t/projects/p/p1", "run_url_prefix": "https://ls/o/t/projects/p/p1/r/"}

    monkeypatch.setattr(observability, "dashboard_links", fake_links)
    client = MagicMock(spec=Client)
    set_llm(FakeLLM(Route(intent="quote", company="NVDA", categories=[])))
    with tracing_context(enabled=True, client=client, project_name="test"):
        r = await chat("conv-42", "price of nvidia?")
    assert r["trace_id"]
    assert r["trace_url"] == f"https://ls/o/t/projects/p/p1/r/{r['trace_id']}?poll=true"
    runs = _runs(client)
    names = {run.get("name") for run in runs}
    assert {"advisor_chat_turn", "understand", "quote_workflow", "respond"} <= names
    root = next(run for run in runs if run.get("name") == "advisor_chat_turn")
    assert str(root["id"]) == r["trace_id"]
    meta = (root.get("extra") or {}).get("metadata", {})
    assert meta["thread_id"] == "conv-42" and meta["conversation_id"] == "conv-42"
