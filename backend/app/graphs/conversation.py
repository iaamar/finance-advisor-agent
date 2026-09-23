"""Conversational wrapper around the two workflows.

    START -> load_memory -> understand --+--> quote_workflow ---+
                                         +--> filings_workflow -+--> respond -> save_memory -> END
                                         +--------------------------^
    - `briefing` runs both workflows in parallel.
    - `followup` / `chitchat` answer from data already in short-term memory.
    - `clarify` fires when the company can't be pinned down to one registrant
      in the SEC ticker file; the reply lists "did you mean" candidates.

Nothing here is specific to any company: the company comes from the message
(or from memory for follow-ups) and is resolved against company_tickers.json.

Short-term memory lives in Redis under the conversation ID: the last N
messages plus the active company and its most recent workflow outputs, so
follow-ups like "what about insider selling?" need no company name.
"""

from __future__ import annotations

import json
import logging
import operator
import re
import uuid
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.graphs.filings_workflow import ALL_CATEGORIES, run_filings_workflow
from app.graphs.quote_workflow import run_quote_workflow
from app.llm import get_llm
from app.memory import get_store
from app.observability import tracing_enabled, turn_config
from app.services.edgar import _QUESTION_WORDS, get_edgar
from app.services.xbrl import format_amount

log = logging.getLogger(__name__)

Intent = Literal["quote", "filings", "briefing", "followup", "chitchat"]
Category = Literal["annual", "quarterly", "current", "insider"]


class Route(BaseModel):
    intent: Intent = Field(
        description=(
            "quote: price/performance question. filings: SEC filing content (results, risks, guidance, "
            "insider trades, events). briefing: general 'get me up to speed / tell me about X'. "
            "followup: answerable from data ALREADY loaded. chitchat: greetings, help, off-topic."
        )
    )
    company: str = Field(description="Company name or ticker the user refers to; empty string if none mentioned.")
    categories: list[Category] = Field(
        description="Report categories a filings question needs: annual (10-K/20-F/40-F: business, risks, full-year "
        "results), quarterly (10-Q: latest quarter), current (8-K/6-K: earnings releases, deals, exec changes, "
        "events), insider (Form 4: insider buying/selling). Empty list means all four."
    )


class ChatState(TypedDict, total=False):
    conversation_id: str
    message: str
    company_hint: str  # ticker picked explicitly in the UI, if any
    history: list[dict[str, str]]
    context: dict[str, Any]
    intent: str
    company: dict[str, Any]
    candidates: list[dict[str, Any]]
    unresolved: str
    categories: list[str]
    quote: dict[str, Any]
    filings: dict[str, Any]
    errors: Annotated[list[str], operator.add]
    reply: str


# ---- nodes -----------------------------------------------------------------
async def load_memory(state: ChatState) -> ChatState:
    store = await get_store()
    cid = state["conversation_id"]
    return {"history": await store.get_history(cid), "context": await store.get_context(cid)}


ROUTER_PROMPT = """You route messages for an assistant that helps wealth-management advisors research any US-listed company (every registrant in the SEC's company_tickers.json).
Classify the latest user message and extract the company exactly as the user referred to it (name or ticker).
Resolve pronouns ("they", "it", "the company") to the active company.
Active company: {active}
Data already loaded for it: {loaded}"""


_CATEGORY_HINTS = [
    (r"\b10-?k\b|\b20-?f\b|\b40-?f\b|annual report|annual filing|risk factors?|business model", "annual"),
    (r"\b10-?q\b|quarterly report|last quarter|this quarter|latest quarter", "quarterly"),
    (r"\b8-?k\b|\b6-?k\b|earnings release|press release|events?|announce", "current"),
    (r"form ?4\b|insider", "insider"),
]


async def _heuristic_route(msg: str, ctx: dict[str, Any]) -> Route:
    """No-LLM fallback router: keyword intent + company spotted via the SEC ticker list."""
    low = msg.lower()
    categories = [c for pat, c in _CATEGORY_HINTS if re.search(pat, low)]
    found = await get_edgar().find_in_text(msg)
    company = found.ticker if found else ""
    if not company:
        # An unrecognised capitalised name ("price of First?") -> let resolution offer candidates.
        runs = re.findall(r"(?:[A-Z][\w&.\-]*\s?)+", msg)
        runs = [r.strip() for r in runs if r.split()[0].lower() not in _QUESTION_WORDS]
        company = max(runs, key=len, default="")
    if re.search(r"\b(price|quote|trading|stock (?:do|doing|done|did)|perform\w*|6 ?m|six months)\b", low) and not categories:
        intent = "quote"
    elif categories or re.search(r"\b(filing|sec|risk|guidance|revenue|earnings|debt|buyback|dividend)\b", low):
        intent = "filings"
    elif company or re.search(r"\b(brief|overview|up to speed|tell me about|summar)", low):
        intent = "briefing"
    elif ctx.get("company"):
        intent = "followup"
    else:
        intent = "chitchat"
    return Route(intent=intent, company=company, categories=categories)


def _loaded(ctx: dict[str, Any]) -> list[str]:
    out = ["quote"] if ctx.get("quote") else []
    return out + [c for c in ALL_CATEGORIES if c in (ctx.get("filings") or {})]


async def understand(state: ChatState) -> ChatState:
    ctx = state.get("context", {})
    edgar = get_edgar()
    active: dict[str, Any] = ctx.get("company") or {}
    picked: dict[str, Any] | None = None
    if hint := state.get("company_hint"):
        if found := await edgar.resolve(hint):
            picked = active = found.to_dict()

    llm = get_llm()
    route: Route | None = None
    if llm.available:
        recent = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in state.get("history", [])[-6:])
        route = await llm.parse(
            ROUTER_PROMPT.format(
                active=f"{active.get('name')} ({active.get('ticker')})" if active else "none",
                loaded=", ".join(_loaded(ctx)) if active.get("ticker") == (ctx.get("company") or {}).get("ticker") else "nothing",
            ),
            f"Recent conversation:\n{recent or '(none)'}\n\nLatest message: {state['message']}",
            Route,
            effort="low",
            max_tokens=2000,
        )
    route = route or await _heuristic_route(state["message"], ctx)
    intent = route.intent
    categories = list(route.categories)

    # Resolve the company against the SEC ticker universe.
    company: dict[str, Any] | None = None
    mentioned = route.company.strip()
    if picked:
        company = picked  # chosen explicitly in the UI (search box or "did you mean")
    elif mentioned and mentioned.upper() != active.get("ticker"):
        resolved = await edgar.resolve(mentioned)
        if not resolved:
            candidates = [c.to_dict() for c in await edgar.search(mentioned, limit=6)]
            return {"intent": "clarify", "unresolved": mentioned, "candidates": candidates, "categories": categories}
        company = resolved.to_dict()
    else:
        company = active or None

    if intent in ("quote", "filings", "briefing") and not company:
        intent = "chitchat"  # respond will ask which company
    switched = bool(company) and company.get("ticker") != (ctx.get("company") or {}).get("ticker")
    if intent == "followup":
        loaded = [] if switched else _loaded(ctx)
        missing = [c for c in categories if c not in loaded]
        if missing:
            intent, categories = "filings", missing
        elif not loaded:
            intent = "briefing" if company else "chitchat"
    return {"intent": intent, "company": company or {}, "categories": categories}


def route_after_understand(state: ChatState) -> list[str]:
    return {
        "quote": ["quote_workflow"],
        "filings": ["filings_workflow"],
        "briefing": ["quote_workflow", "filings_workflow"],
    }.get(state["intent"], ["respond"])


async def quote_node(state: ChatState) -> ChatState:
    q = await run_quote_workflow(state["company"]["ticker"])
    return {"quote": q, "errors": [q["error"]] if q.get("error") else []}


async def filings_node(state: ChatState) -> ChatState:
    f = await run_filings_workflow(state["company"]["ticker"], state.get("categories") or None)
    return {"filings": f, "errors": f.get("errors", [])}


RESPOND_PROMPT = """You are a research assistant for wealth-management advisors. You help them get up to speed on a stock fast.

Answer the advisor's latest message using ONLY the JSON data provided (live quote data and SEC filing summaries) and the conversation so far.
- Lead with the direct answer. Then give supporting detail in short bullets or brief sections.
- Every figure must come from the data. When citing filing facts, name the form and filing date (e.g. "10-Q filed 2026-08-26").
- key_financials values are exact XBRL figures in raw units with a "unit" (USD, EUR, TWD...); format them readably and keep the currency (e.g. 96221000000 USD -> $96.2B).
- Surface red_flags and notable_items when relevant; never omit a red flag if you are summarizing that filing.
- Foreign issuers file 20-F/40-F (annual) and 6-K (current) instead of 10-K/10-Q/8-K; say so if relevant. If a category is missing, the errors list says why.
- If the data doesn't answer the question, say so plainly and suggest what you can fetch (quote, annual report, quarterly report, current reports, insider trades).
- No investment advice, recommendations, or price targets. Plain text with simple "-" bullets; no markdown tables."""


def _compact(obj: Any, limit: int = 60000) -> str:
    s = json.dumps(obj, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


def _clarify_reply(state: ChatState) -> str:
    q = state.get("unresolved", "")
    cands = state.get("candidates") or []
    if not cands:
        return (
            f"I couldn't find “{q}” among companies registered with the SEC. "
            "Try the exact company name or its ticker symbol."
        )
    lines = [f"“{q}” could match more than one company. Which one did you mean?"]
    lines += [f"- {c['name']} ({c['ticker']})" for c in cands]
    return "\n".join(lines)


def _financial_lines(kf: dict[str, Any]) -> list[str]:
    for bucket, label in (("latest_quarter", "Latest quarter"), ("latest_fiscal_year", "Latest fiscal year")):
        rows = (kf or {}).get(bucket) or {}
        if "revenue" not in rows and "net_income" not in rows:
            continue
        end = next(iter(rows.values()))["period_end"]
        out = [f"\n{label} (ended {end}, SEC XBRL):"]
        for key, name in (
            ("revenue", "Revenue"),
            ("operating_income", "Operating income"),
            ("net_income", "Net income"),
            ("eps_diluted", "Diluted EPS"),
        ):
            if row := rows.get(key):
                yoy = f" ({row['yoy_pct']:+.1f}% YoY)" if "yoy_pct" in row else ""
                out.append(f"- {name}: {format_amount(row['value'], row.get('unit'))}{yoy}")
        return out
    return []


def _fallback_reply(state: ChatState, data: dict[str, Any]) -> str:
    if state["intent"] == "chitchat":
        return (
            "I can research any company registered with the SEC: a live quote with 6-month performance, key "
            "financials, and summaries of its latest annual and quarterly reports, current reports (8-K/6-K) and "
            "insider trades. Which company would you like to look at?"
        )
    lines: list[str] = []
    if q := data.get("quote"):
        lines.append(q["summary"])
    f = data.get("filings") or {}
    lines += _financial_lines(f.get("key_financials") or {})
    for cat in ("quarterly", "annual"):
        if s := f.get(cat):
            if s["headline"].startswith("Automated summary unavailable"):
                lines.append(f"\n{s['form']} filed {s['filed']}: {s['url']}")
            else:
                lines.append(f"\n{s['form']} (filed {s['filed']}): {s['headline']}")
                lines += [f"- {p}" for p in s.get("key_points", [])[:5]]
            lines += [f"- RED FLAG: {r}" for r in s.get("red_flags", [])]
    for s in (f.get("current") or [])[:3]:
        lines.append(f"\n{s['form']} (filed {s['filed']}): {s['headline']}")
        lines += [f"- RED FLAG: {r}" for r in s.get("red_flags", [])]
    if s := f.get("insider"):
        lines.append(f"\nInsider activity (last {s['filings_reviewed']} Form 4s): {s['headline']}")
    lines += [f"\nNote: {e}" for e in state.get("errors", [])]
    if not lines:
        return "I couldn't find data for that request."
    if not get_llm().available:
        lines.append("\n(Narrative filing summaries are off: set ANTHROPIC_API_KEY to enable them.)")
    return "\n".join(lines).strip()


async def respond(state: ChatState) -> ChatState:
    if state["intent"] == "clarify":
        return {"reply": _clarify_reply(state)}
    ctx = state.get("context", {})
    new = state.get("company") or {}
    same_company = new.get("ticker") == (ctx.get("company") or {}).get("ticker")
    # Merge fresh results over what's already in memory for the same company.
    data = {
        "company": new or None,
        "quote": state.get("quote") or (ctx.get("quote") if same_company else None),
        "filings": {**((ctx.get("filings") or {}) if same_company else {}), **(state.get("filings") or {})},
    }
    llm = get_llm()
    reply = None
    if llm.available:
        content = f"<data>\n{_compact(data)}\n</data>\n"
        if state.get("errors"):
            content += f"<errors>{state['errors']}</errors>\n"
        messages = [*state.get("history", [])[-10:], {"role": "user", "content": f"{content}\n{state['message']}"}]
        reply = await llm.text(RESPOND_PROMPT, messages, effort="low" if state["intent"] == "chitchat" else "medium")
    if not reply:
        # Template fallback: show what was just fetched, or memory for pure follow-ups.
        fresh = {k: state[k] for k in ("quote", "filings") if state.get(k)}
        reply = _fallback_reply(state, fresh or data)
    return {"reply": reply}


async def save_memory(state: ChatState) -> ChatState:
    store = await get_store()
    cid = state["conversation_id"]
    ctx = dict(state.get("context", {}))
    new_company = state.get("company")
    if new_company:
        if (ctx.get("company") or {}).get("ticker") != new_company["ticker"]:
            ctx = {}  # switched companies: drop the old company's data
        ctx["company"] = new_company
    if state.get("quote") and not state["quote"].get("error"):
        ctx["quote"] = state["quote"]
    if (state.get("filings") or {}).get("company"):
        merged = dict(ctx.get("filings") or {})
        merged.update({k: v for k, v in state["filings"].items() if k != "errors" and v})
        ctx["filings"] = merged
    await store.set_context(cid, ctx)
    await store.append_messages(
        cid, {"role": "user", "content": state["message"]}, {"role": "assistant", "content": state["reply"]}
    )
    return {}


def build_conversation_graph():
    g = StateGraph(ChatState)
    g.add_node("load_memory", load_memory)
    g.add_node("understand", understand)
    g.add_node("quote_workflow", quote_node)
    g.add_node("filings_workflow", filings_node)
    g.add_node("respond", respond)
    g.add_node("save_memory", save_memory)
    g.add_edge(START, "load_memory")
    g.add_edge("load_memory", "understand")
    g.add_conditional_edges("understand", route_after_understand, ["quote_workflow", "filings_workflow", "respond"])
    g.add_edge("quote_workflow", "respond")
    g.add_edge("filings_workflow", "respond")
    g.add_edge("respond", "save_memory")
    g.add_edge("save_memory", END)
    return g.compile()


conversation_graph = build_conversation_graph()


async def chat(conversation_id: str, message: str, company_hint: str | None = None) -> dict[str, Any]:
    run_id = uuid.uuid4()
    s = await conversation_graph.ainvoke(
        {"conversation_id": conversation_id, "message": message, "company_hint": company_hint or "", "errors": []},
        config=turn_config(conversation_id, run_id, company_hint=company_hint or None),
    )
    return {
        "conversation_id": conversation_id,
        "reply": s["reply"],
        "intent": s["intent"],
        "company": s.get("company") or None,
        "candidates": s.get("candidates", []),
        "data": {k: s[k] for k in ("quote", "filings") if s.get(k)},
        "errors": s.get("errors", []),
        # Look this up in LangSmith (search by run ID) to see the full trace of the turn.
        "trace_id": str(run_id) if tracing_enabled() else None,
    }

