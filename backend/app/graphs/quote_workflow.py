"""Workflow #1: fetch current quote data.

    Input : company name (or ticker)
    Output: {"stock_price": ..., "summary": ..., plus current price + 6M delta detail}

    START -> resolve_company -> fetch_quote -> summarize -> END
                  |                 |
                  +---- error ------+-----> END
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.edgar import get_edgar
from app.services.quote import QuoteError, compute_quote_metrics, get_quote_provider


class QuoteState(TypedDict, total=False):
    company_query: str
    company: dict[str, Any]
    metrics: dict[str, Any]
    summary: str
    error: str


async def resolve_company(state: QuoteState) -> QuoteState:
    company = await get_edgar().resolve(state["company_query"])
    if not company:
        return {"error": f"Couldn't find a US-listed company matching “{state['company_query']}”."}
    return {"company": company.to_dict()}


async def fetch_quote(state: QuoteState) -> QuoteState:
    ticker = state["company"]["ticker"]
    try:
        history = await get_quote_provider().history_6m(ticker)
    except QuoteError as exc:
        return {"error": str(exc)}
    return {"metrics": compute_quote_metrics(history).to_dict()}


def summarize(state: QuoteState) -> QuoteState:
    # Deterministic template: every number here is computed, never generated.
    m, c = state["metrics"], state["company"]
    sign = "+" if m["delta_6m_abs"] >= 0 else "−"
    direction = "up" if m["delta_6m_abs"] >= 0 else "down"
    summary = (
        f"{c['name']} ({c['ticker']}) last traded at {m['stock_price']:,.2f} {m['currency']} "
        f"(as of {m['as_of'][:16].replace('T', ' ')} UTC). Over the last 6 months it is {direction} "
        f"{sign}{abs(m['delta_6m_abs']):,.2f} ({sign}{abs(m['delta_6m_pct']):.2f}%) from "
        f"{m['price_6m_ago']:,.2f} on {m['date_6m_ago']}, trading in a 6-month range of "
        f"{m['low_6m']:,.2f}–{m['high_6m']:,.2f}."
    )
    return {"summary": summary}


def _ok(state: QuoteState) -> str:
    return "error" if state.get("error") else "ok"


def build_quote_graph():
    g = StateGraph(QuoteState)
    g.add_node("resolve_company", resolve_company)
    g.add_node("fetch_quote", fetch_quote)
    g.add_node("summarize", summarize)
    g.add_edge(START, "resolve_company")
    g.add_conditional_edges("resolve_company", _ok, {"ok": "fetch_quote", "error": END})
    g.add_conditional_edges("fetch_quote", _ok, {"ok": "summarize", "error": END})
    g.add_edge("summarize", END)
    return g.compile()


quote_graph = build_quote_graph()


async def run_quote_workflow(company_query: str) -> dict[str, Any]:
    """Returns the JSON contract from the architecture diagram."""
    s = await quote_graph.ainvoke({"company_query": company_query})
    if s.get("error"):
        return {"stock_price": None, "summary": s["error"], "error": s["error"]}
    m = s["metrics"]
    return {
        "stock_price": m["stock_price"],
        "summary": s["summary"],
        "company": s["company"],
        "currency": m["currency"],
        "as_of": m["as_of"],
        "delta_6m": {
            "abs": m["delta_6m_abs"],
            "pct": m["delta_6m_pct"],
            "from_price": m["price_6m_ago"],
            "from_date": m["date_6m_ago"],
            "high": m["high_6m"],
            "low": m["low_6m"],
        },
        "source": m["source"],
    }
