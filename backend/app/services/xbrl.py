"""Deterministic key financials from the XBRL companyfacts API.

Numbers shown to advisors come from here, not from the LLM. Works for any
filer: each metric has a fallback list of concepts across the US-GAAP and
IFRS taxonomies (filers tag the same thing differently), and values are kept
in the company's own reporting currency (USD, TWD, EUR, ...).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

FLOW_METRICS: dict[str, list[str]] = {
    "revenue": [
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:SalesRevenueNet",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap:RevenuesNetOfInterestExpense",  # banks
        "us-gaap:InterestAndDividendIncomeOperating",  # banks, fallback
        "ifrs-full:Revenue",
        "ifrs-full:RevenueFromContractsWithCustomers",
    ],
    "gross_profit": ["us-gaap:GrossProfit", "ifrs-full:GrossProfit"],
    "operating_income": ["us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities"],
    "net_income": [
        "us-gaap:NetIncomeLoss",
        "us-gaap:ProfitLoss",
        "ifrs-full:ProfitLossAttributableToOwnersOfParent",
        "ifrs-full:ProfitLoss",
    ],
    "eps_diluted": ["us-gaap:EarningsPerShareDiluted", "ifrs-full:DilutedEarningsLossPerShare"],
    "operating_cash_flow": [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "ifrs-full:CashFlowsFromUsedInOperatingActivities",
    ],
}
INSTANT_METRICS: dict[str, list[str]] = {
    "cash": [
        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "ifrs-full:CashAndCashEquivalents",
    ],
    "total_assets": ["us-gaap:Assets", "ifrs-full:Assets"],
    "total_liabilities": ["us-gaap:Liabilities", "ifrs-full:Liabilities"],
    "stockholders_equity": ["us-gaap:StockholdersEquity", "ifrs-full:EquityAttributableToOwnersOfParent", "ifrs-full:Equity"],
    "long_term_debt": ["us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebt", "ifrs-full:NoncurrentPortionOfNoncurrentBorrowings"],
}


def _days(f: dict) -> int:
    return (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days


def _series(facts: dict, qualified: str, per_share: bool) -> tuple[list[dict], str | None]:
    """Rows for a concept in its dominant unit (the reporting currency)."""
    taxonomy, concept = qualified.split(":", 1)
    node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not node:
        return [], None
    units = {u: rows for u, rows in node.get("units", {}).items() if u.endswith("/shares") == per_share and u != "shares"}
    if not units:
        return [], None
    unit = Counter({u: len(r) for u, r in units.items()}).most_common(1)[0][0]
    return units[unit], unit


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("start"), r["end"])
        if key not in seen or r.get("filed", "") > seen[key].get("filed", ""):
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r["end"])


def _pick_flow(facts: dict, concepts: list[str], lo: int, hi: int, per_share: bool):
    """Latest period with duration in [lo, hi] days, plus the same period a year earlier."""
    best: tuple[dict | None, dict | None, str | None, str | None] = (None, None, None, None)
    for c in concepts:
        rows, unit = _series(facts, c, per_share)
        rows = _dedupe([r for r in rows if "start" in r and lo <= _days(r) <= hi])
        if not rows:
            continue
        latest = rows[-1]
        if best[0] is None or latest["end"] > best[0]["end"]:
            end = date.fromisoformat(latest["end"])
            prior = next(
                (r for r in reversed(rows) if abs((end - date.fromisoformat(r["end"])).days - 364) <= 21), None
            )
            best = (latest, prior, c, unit)
    return best


def _pick_instant(facts: dict, concepts: list[str]):
    best: tuple[dict | None, str | None, str | None] = (None, None, None)
    for c in concepts:
        rows, unit = _series(facts, c, per_share=False)
        rows = _dedupe([r for r in rows if "start" not in r])
        if rows and (best[0] is None or rows[-1]["end"] > best[0]["end"]):
            best = (rows[-1], c, unit)
    return best


def _fmt_row(latest: dict, prior: dict | None, concept: str, unit: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "value": latest["val"],
        "unit": unit,
        "period_start": latest.get("start"),
        "period_end": latest["end"],
        "form": latest.get("form"),
        "filed": latest.get("filed"),
        "accession": latest.get("accn"),
        "concept": concept,
    }
    if prior and prior["val"]:
        out["prior_year_value"] = prior["val"]
        out["yoy_pct"] = round((latest["val"] - prior["val"]) / abs(prior["val"]) * 100, 1)
    return out


def key_financials(facts: dict) -> dict[str, Any]:
    quarterly: dict[str, Any] = {}
    annual: dict[str, Any] = {}
    for name, concepts in FLOW_METRICS.items():
        per_share = name == "eps_diluted"
        q, qp, qc, qu = _pick_flow(facts, concepts, 80, 100, per_share)
        if q:
            quarterly[name] = _fmt_row(q, qp, qc, qu)
        a, ap, ac, au = _pick_flow(facts, concepts, 350, 380, per_share)
        if a:
            annual[name] = _fmt_row(a, ap, ac, au)
    # Some filers only tag year-to-date values for a metric (typically cash
    # flow); a "quarter" from an older period would be misleading, so drop it.
    for bucket in (quarterly, annual):
        if "revenue" in bucket:
            end = bucket["revenue"]["period_end"]
            for k in [k for k, v in bucket.items() if v["period_end"] != end]:
                del bucket[k]
    # Filers that stopped reporting quarterly XBRL (e.g. switched to 20-F) leave
    # an old quarter behind; never present it as "latest".
    if quarterly and annual and "revenue" in annual:
        q_end = next(iter(quarterly.values()))["period_end"]
        if q_end < annual["revenue"]["period_end"]:
            quarterly = {}
    balance: dict[str, Any] = {}
    for name, concepts in INSTANT_METRICS.items():
        b, bc, bu = _pick_instant(facts, concepts)
        if b:
            balance[name] = _fmt_row(b, None, bc, bu)
    return {
        "entity": facts.get("entityName"),
        "latest_quarter": quarterly,
        "latest_fiscal_year": annual,
        "balance_sheet": balance,
        "source": "SEC XBRL companyfacts API",
    }


def format_amount(value: float, unit: str | None) -> str:
    """96221000000, "USD" -> "$96.22B";  2.46, "USD/shares" -> "$2.46";  x, "TWD" -> "TWD 1.23T"."""
    cur = (unit or "").split("/")[0]
    prefix = "$" if cur == "USD" else f"{cur} " if cur else ""
    if unit and unit.endswith("/shares"):
        return f"{prefix}{value:,.2f}"
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= div:
            return f"{prefix}{value / div:,.2f}{suffix}"
    return f"{prefix}{value:,.0f}"
