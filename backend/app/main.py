"""HTTP API.

POST /api/chat                      conversational endpoint (text reply + data)
GET  /api/companies?q=              search the SEC ticker universe (autocomplete)
GET  /api/conversations/{id}        history for a conversation
DELETE /api/conversations/{id}      clear a conversation
POST /api/workflows/quote           Workflow #1 directly -> {stock_price, summary, ...}
POST /api/workflows/filings         Workflow #2 directly -> per-form summaries
GET  /api/observability             LangSmith dashboard link for this environment
GET  /api/health
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.config import get_settings
from app.graphs.conversation import chat
from app.graphs.filings_workflow import run_filings_workflow
from app.graphs.quote_workflow import run_quote_workflow
from app.llm import get_llm
from app.memory import get_store
from app.observability import configure_tracing, dashboard_links, tracing_enabled
from app.services.edgar import get_edgar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
configure_tracing()

app = FastAPI(title="Finance Advisor Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    company: str | None = Field(default=None, description="Ticker picked in the UI; becomes the active company.")


class CompanyRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    categories: list[str] | None = Field(
        default=None, description="annual | quarterly | current | insider (form names like 10-K also accepted)"
    )


@app.get("/api/health")
async def health() -> dict:
    store = await get_store()
    return {
        "ok": True,
        "llm": get_llm().available,
        "memory": type(store.r).__name__,
        "quote_provider": get_settings().quote_provider,
        "tracing": tracing_enabled(),
        "tracing_project": get_settings().langsmith_project if tracing_enabled() else None,
    }


@app.get("/api/observability")
async def observability() -> dict:
    """Where this environment's traces go. Open `project_url`; the Threads tab groups by conversation."""
    links = await dashboard_links()
    return {
        "tracing": tracing_enabled(),
        "environment": get_settings().app_env,
        "project": get_settings().langsmith_project,
        "project_url": links["project_url"] if links else None,
        "dashboard_home": "https://smith.langchain.com",
    }


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest) -> dict:
    cid = req.conversation_id or uuid.uuid4().hex
    return await chat(cid, req.message.strip(), req.company)


@app.get("/api/companies")
async def companies(q: str = Query(min_length=1, max_length=100), limit: int = Query(8, ge=1, le=25)) -> dict:
    """Autocomplete over every registrant in SEC company_tickers.json."""
    results = await get_edgar().search(q, limit=limit)
    return {"query": q, "results": [c.to_dict() for c in results]}


@app.get("/api/conversations/{cid}")
async def get_conversation(cid: str) -> dict:
    store = await get_store()
    ctx = await store.get_context(cid)
    return {"conversation_id": cid, "messages": await store.get_history(cid), "company": ctx.get("company")}


@app.delete("/api/conversations/{cid}")
async def clear_conversation(cid: str) -> dict:
    await (await get_store()).clear(cid)
    return {"ok": True}


@app.post("/api/workflows/quote")
async def quote_endpoint(req: CompanyRequest) -> dict:
    result = await run_quote_workflow(req.company)
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@app.post("/api/workflows/filings")
async def filings_endpoint(req: CompanyRequest) -> dict:
    result = await run_filings_workflow(req.company, req.categories)
    if not result.get("company"):
        raise HTTPException(404, "; ".join(result.get("errors", [])) or "not found")
    return result
