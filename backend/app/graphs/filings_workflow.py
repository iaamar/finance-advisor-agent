"""Workflow #2: summarize key information from a company's recent SEC filings.

Works for any registrant in the SEC ticker file. The caller asks for report
*categories*; each category is filled by whichever form the company files:

    annual    10-K | 20-F | 40-F        current   8-K | 6-K
    quarterly 10-Q                      insider   Form 4

    Input : company name or ticker + categories (default: all four)
    Output: {"company", "profile", "key_financials",
             "annual", "quarterly", "current", "insider", "errors"}

                                   +-> fetch_financials ---+
                                   +-> summarize_annual ---+
    START -> resolve_company -> ---+-> summarize_quarterly +--> assemble -> END
             (+ filer profile)     +-> summarize_current --+
                                   +-> summarize_insider --+
    (fan-out is conditional: only the requested categories run, in parallel;
     a category the company doesn't file comes back as an explained note)
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.edgar import CATEGORIES, CATEGORY_LABELS, Company, NoDataError, get_edgar
from app.services.xbrl import key_financials
from app.summarize.summarizer import recent_for, summarize_current, summarize_form4, summarize_periodic

ALL_CATEGORIES = list(CATEGORIES)
# Accept form names too ("10-K", "8-K", "4", "20-F") and map them to categories.
_ALIASES = {form: cat for cat, forms in CATEGORIES.items() for form in forms} | {"form 4": "insider"}


def normalize_categories(requested: list[str] | None) -> list[str]:
    out: list[str] = []
    for r in requested or []:
        cat = r if r in CATEGORIES else _ALIASES.get(r.upper(), _ALIASES.get(r.lower()))
        if cat and cat not in out:
            out.append(cat)
    return out or ALL_CATEGORIES


class FilingsState(TypedDict, total=False):
    company_query: str
    categories: list[str]
    company: dict[str, Any]
    profile: dict[str, Any]
    key_financials: dict[str, Any]
    annual: dict[str, Any]
    quarterly: dict[str, Any]
    current: list[dict[str, Any]]
    insider: dict[str, Any]
    errors: Annotated[list[str], operator.add]
    result: dict[str, Any]


def _company(state: FilingsState) -> Company:
    return Company(**state["company"])


async def resolve_company(state: FilingsState) -> FilingsState:
    edgar = get_edgar()
    company = await edgar.resolve(state["company_query"])
    if not company:
        return {"errors": [f"Couldn't find a company matching “{state['company_query']}” in the SEC ticker list."]}
    profile = await edgar.filing_profile(company)
    errors = []
    if profile["filer_type"] == "no periodic reports":
        # Nothing to summarize (unsponsored ADR, brand-new listing): one clear note, no fan-out.
        errors.append(
            f"{company.name} files no periodic reports with the SEC (e.g. an unsponsored ADR or a newly listed "
            "company), so only market data is available."
        )
        return {"company": company.to_dict(), "profile": profile, "categories": [], "errors": errors}
    return {
        "company": company.to_dict(),
        "profile": profile,
        "categories": normalize_categories(state.get("categories")),
        "errors": errors,
    }


def _not_found(state: FilingsState, category: str) -> list[str]:
    c = _company(state)
    foreign = (state.get("profile") or {}).get("filer_type") == "foreign private issuer"
    reason = (
        "foreign private issuers report interim results on Form 6-K instead"
        if category == "quarterly" and foreign
        else "foreign private issuers are exempt from Form 4"
        if category == "insider" and foreign
        else "none on EDGAR"
    )
    return [f"{CATEGORY_LABELS[category]}: not filed by {c.name} ({reason})."]


# Node names must differ from state keys, hence the prefix.
NODE = {cat: f"summarize_{cat}" for cat in ALL_CATEGORIES}


def fan_out(state: FilingsState) -> list[str]:
    if not state.get("company"):
        return [END]
    if not state["categories"]:
        return ["assemble"]
    return ["fetch_financials", *(NODE[c] for c in state["categories"])]


async def financials_node(state: FilingsState) -> FilingsState:
    try:
        return {"key_financials": key_financials(await get_edgar().company_facts(_company(state)))}
    except NoDataError as exc:
        return {"errors": [str(exc)]}
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"XBRL financials unavailable: {exc}"]}


def _periodic_node(category: str):
    async def node(state: FilingsState) -> FilingsState:
        c = _company(state)
        try:
            filings = await recent_for(c, category)
            if not filings:
                return {"errors": _not_found(state, category)}
            return {category: await summarize_periodic(c, filings[0])}
        except Exception as exc:  # noqa: BLE001
            return {"errors": [f"{CATEGORY_LABELS[category]} summary failed: {exc}"]}

    return node


async def current_node(state: FilingsState) -> FilingsState:
    c = _company(state)
    try:
        filings = await recent_for(c, "current")
        if not filings:
            return {"errors": _not_found(state, "current")}
        return {"current": list(await asyncio.gather(*(summarize_current(c, f) for f in filings)))}
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"Current report summary failed: {exc}"]}


async def insider_node(state: FilingsState) -> FilingsState:
    c = _company(state)
    try:
        filings = await recent_for(c, "insider")
        if not filings:
            return {"errors": _not_found(state, "insider")}
        summary = await summarize_form4(c, filings)
        if not summary["filings_reviewed"]:  # every Form 4 was the company trading *other* stocks
            return {"errors": _not_found(state, "insider")}
        return {"insider": summary}
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"Form 4 summary failed: {exc}"]}


def assemble(state: FilingsState) -> FilingsState:
    result: dict[str, Any] = {
        "company": state.get("company"),
        "profile": state.get("profile"),
        "key_financials": state.get("key_financials"),
    }
    for cat in ALL_CATEGORIES:
        if cat in state:
            result[cat] = state[cat]
    result["errors"] = state.get("errors", [])
    return {"result": result}


def build_filings_graph():
    g = StateGraph(FilingsState)
    g.add_node("resolve_company", resolve_company)
    g.add_node("fetch_financials", financials_node)
    g.add_node(NODE["annual"], _periodic_node("annual"))
    g.add_node(NODE["quarterly"], _periodic_node("quarterly"))
    g.add_node(NODE["current"], current_node)
    g.add_node(NODE["insider"], insider_node)
    g.add_node("assemble", assemble)
    g.add_edge(START, "resolve_company")
    branches = ["fetch_financials", *NODE.values()]
    g.add_conditional_edges("resolve_company", fan_out, [*branches, "assemble", END])
    # All branches run in the same superstep, so assemble fires once after they finish.
    for node in branches:
        g.add_edge(node, "assemble")
    g.add_edge("assemble", END)
    return g.compile()


filings_graph = build_filings_graph()


async def run_filings_workflow(company_query: str, categories: list[str] | None = None) -> dict[str, Any]:
    s = await filings_graph.ainvoke({"company_query": company_query, "categories": categories or [], "errors": []})
    return s.get("result") or {"company": None, "errors": s.get("errors", [])}
