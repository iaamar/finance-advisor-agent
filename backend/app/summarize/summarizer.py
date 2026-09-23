"""Per-form summarizers. Each returns a JSON-serializable dict.

Filing summaries are cached by accession number: a filing never changes once
it's submitted, so each one is fetched and summarized at most once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm import get_llm
from app.memory import get_store
from app.observability import traceable
from app.services.edgar import CATEGORIES, Company, FilingRef, get_edgar
from app.services.form4 import InsiderTxn, parse_form4, summarize_insider_activity
from app.services.text import (
    RED_FLAG_8K_ITEMS,
    find_section_by_title,
    html_to_text,
    is_boilerplate_no_change,
    scan_red_flags,
    split_8k_items,
    split_sections,
)
from app.summarize import rules

log = logging.getLogger(__name__)
CACHE_VERSION = "v2"


class FilingSummary(BaseModel):
    headline: str = Field(description="One sentence: the single most important takeaway from this filing.")
    key_points: list[str] = Field(description="3-6 bullets on results, drivers and business developments.")
    outlook: str = Field(description="Management guidance/outlook in one or two sentences, or empty string if none.")
    risks: list[str] = Field(description="Most important company-specific risks or concerns, 0-6 bullets.")
    capital_return: str = Field(description="Buybacks/dividends/authorization, or empty string if not mentioned.")
    notable_items: list[str] = Field(
        description="Unusual items worth scrutiny (big commitments, guarantees, concentration, accounting changes)."
    )


class EightKSummary(BaseModel):
    headline: str = Field(description="One sentence describing the event.")
    key_points: list[str] = Field(description="2-5 bullets with the concrete facts (amounts, parties, dates).")
    guidance: str = Field(description="Forward guidance figures if this is an earnings release, else empty string.")


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[...truncated...]"


def _fallback_summary(sections: dict[str, str]) -> dict[str, Any]:
    """No-LLM fallback: first sentences of each kept section."""
    pts = []
    for label, body in sections.items():
        snippet = " ".join(body.split())[:300]
        pts.append(f"{label}: {snippet}…")
    return {
        "headline": "Automated summary unavailable (LLM not configured); showing section excerpts.",
        "key_points": pts,
        "outlook": "",
        "risks": [],
        "capital_return": "",
        "notable_items": [],
    }


async def _cached(key: str, producer) -> Any:
    store = await get_store()
    full_key = f"summary:{CACHE_VERSION}:{key}"
    hit = await store.cache_get(full_key)
    if hit is not None:
        _tag_span(cache="hit")
        return hit
    _tag_span(cache="miss")
    value = await producer()
    if not value.pop("_uncacheable", False):
        await store.cache_set(full_key, value)
    return value


def _tag_span(**meta: Any) -> None:
    from langsmith.run_helpers import get_current_run_tree

    if run := get_current_run_tree():
        run.metadata.update(meta)


def _filing_inputs(i: dict[str, Any]) -> dict[str, Any]:
    c, f = i.get("company"), i.get("filing")
    return {"ticker": getattr(c, "ticker", None), "form": getattr(f, "form", None),
            "filed": getattr(f, "filed", None), "accession": getattr(f, "accession", None)}


def _meta(company: Company, f: FilingRef) -> dict[str, Any]:
    return {
        "company": company.name,
        "ticker": company.ticker,
        "form": f.form,
        "filed": f.filed,
        "period": f.period,
        "accession": f.accession,
        "url": f.url,
    }


# ---- annual / quarterly (10-K, 10-Q, 20-F, 40-F) -------------------------
async def _periodic_text(filing: FilingRef) -> str:
    edgar = get_edgar()
    text = html_to_text(await edgar.get_text(filing.url))
    if filing.form == "40-F":
        # The 40-F body is a wrapper; the AIF / MD&A / financials are exhibits.
        for ex_type, url in (await edgar.exhibits(filing))[:4]:
            try:
                text += f"\n\n{ex_type}\n" + html_to_text(await edgar.get_text(url))
            except Exception as exc:  # noqa: BLE001
                log.warning("40-F exhibit %s failed: %s", url, exc)
    return text


@traceable(run_type="chain", name="summarize_periodic_filing", process_inputs=_filing_inputs)
async def summarize_periodic(company: Company, filing: FilingRef) -> dict[str, Any]:
    async def produce() -> dict[str, Any]:
        text = await _periodic_text(filing)
        sections = split_sections(text, filing.form)
        section_rules = rules.PERIODIC_RULES.get(filing.form, rules.PERIODIC_RULES["10-K"])

        kept: dict[str, str] = {}
        focus_lines = []
        skipped = []
        for r in section_rules:
            body = sections.get(r.key, "") if r.key else ""
            if r.title and len(body) < 3000:
                body = find_section_by_title(text, r.title) or body
            if len(body) < 200:
                continue
            if r.skip_if_no_change and is_boilerplate_no_change(body):
                skipped.append(f"{r.label}: no material changes reported")
                continue
            kept[r.label] = _clip(body, r.max_chars)
            focus_lines.append(f"- {r.label}: focus on {r.focus}")

        flag_text = "\n".join(sections.get(k, "") for k in rules.RED_FLAG_SECTIONS.get(filing.form, []))
        red_flags = scan_red_flags(flag_text)

        llm = get_llm()
        summary: dict[str, Any] | None = None
        if kept and llm.available:
            doc = "\n\n".join(f"=== {label} ===\n{body}" for label, body in kept.items())
            prompt = (
                f"{company.name} ({company.ticker}) {filing.form} for period ending {filing.period}, "
                f"filed {filing.filed}.\n\nWhat to extract from each section:\n"
                + "\n".join(focus_lines)
                + f"\n\n{doc}"
            )
            parsed = await llm.parse(rules.SYSTEM_PROMPT, prompt, FilingSummary)
            summary = parsed.model_dump() if parsed else None
        result = summary or _fallback_summary(kept)
        result.update(
            _meta(company, filing),
            red_flags=red_flags,
            sections_used=list(kept),
            sections_skipped=skipped,
            _uncacheable=summary is None,  # retry later once an LLM is available
        )
        if not kept:
            result["headline"] = "Could not locate the standard Item sections in this filing."
        return result

    return await _cached(f"{filing.accession}", produce)


# ---- current reports (8-K, 6-K) -----------------------------------------
@traceable(run_type="chain", name="summarize_current_report", process_inputs=_filing_inputs)
async def summarize_current(company: Company, filing: FilingRef) -> dict[str, Any]:
    async def produce() -> dict[str, Any]:
        edgar = get_edgar()
        text = html_to_text(await edgar.get_text(filing.url))
        items = filing.items or list(split_8k_items(text))
        items_described = [f"{i} – {rules.EIGHT_K_ITEMS.get(i, 'Other')}" for i in items]
        red_flags = [f"8-K Item {i}: {RED_FLAG_8K_ITEMS[i]}" for i in items if i in RED_FLAG_8K_ITEMS]

        parts = [f"=== {filing.form} body ===\n{_clip(text, rules.EIGHT_K_MAX_CHARS)}"]
        # Earnings (2.02) / Reg FD (7.01) 8-Ks and all 6-Ks keep their content
        # in EX-99 exhibits (press releases, interim reports).
        exhibits_used = []
        if filing.form == "6-K" or {"2.02", "7.01", "8.01"} & set(items):
            try:
                for ex_type, url in (await edgar.exhibits(filing))[:2]:
                    ex_text = html_to_text(await edgar.get_text(url))
                    parts.append(f"=== {ex_type} ===\n{_clip(ex_text, rules.EIGHT_K_EXHIBIT_MAX_CHARS)}")
                    exhibits_used.append({"type": ex_type, "url": url})
            except Exception as exc:  # noqa: BLE001
                log.warning("exhibit fetch failed for %s: %s", filing.accession, exc)

        focus = [f"- Item {i}: {rules.EIGHT_K_FOCUS[i]}" for i in items if i in rules.EIGHT_K_FOCUS]
        llm = get_llm()
        parsed = None
        if llm.available:
            prompt = (
                f"{company.name} ({company.ticker}) {filing.form} filed {filing.filed}. "
                + (f"Items: {', '.join(items_described)}.\n" if items else "\n")
                + ("What to extract:\n" + "\n".join(focus) + "\n\n" if focus else "\n")
                + "\n\n".join(parts)
            )
            parsed = await llm.parse(rules.SYSTEM_PROMPT, prompt, EightKSummary, effort="low")
        result = (
            parsed.model_dump()
            if parsed
            else {
                "headline": "; ".join(items_described) or (filing.description or f"{filing.form} report"),
                "key_points": [],
                "guidance": "",
            }
        )
        result.update(
            _meta(company, filing),
            items=items_described,
            exhibits=exhibits_used,
            red_flags=red_flags,
            _uncacheable=parsed is None,
        )
        return result

    return await _cached(f"{filing.accession}", produce)


# ---- Form 4 ----------------------------------------------------------------
@traceable(run_type="chain", name="summarize_insider_trades",
           process_inputs=lambda i: {"ticker": getattr(i.get("company"), "ticker", None), "filings": len(i.get("filings") or [])})
async def summarize_form4(company: Company, filings: list[FilingRef]) -> dict[str, Any]:
    edgar = get_edgar()

    async def one(f: FilingRef) -> list[dict]:
        async def produce() -> dict[str, Any]:
            txns = parse_form4(await edgar.get_text(f.url))
            for t in txns:
                t.filed, t.url = f.filed, f.url
            return {"txns": [t.to_dict() for t in txns]}

        try:
            return (await _cached(f"form4:{f.accession}", produce))["txns"]
        except Exception as exc:  # noqa: BLE001
            log.warning("form 4 parse failed for %s: %s", f.url, exc)
            return []

    batches = await asyncio.gather(*(one(f) for f in filings))
    # EDGAR lists a Form 4 under a company's CIK both when the company is the
    # issuer and when it is the *reporting owner* (e.g. it bought into another
    # company). Only the former are insider trades in this company's stock.
    own = [b for b in batches if b and all(t.get("issuer_cik") in (None, company.cik) for t in b)]
    filings_reviewed = len(own)
    txns = [InsiderTxn(**t) for batch in own for t in batch]
    agg = summarize_insider_activity(txns)
    notable = sorted((t for t in txns if t.code in ("P", "S")), key=lambda t: -(t.value or 0))[:8]
    return {
        "company": company.name,
        "ticker": company.ticker,
        "form": "4",
        "filings_reviewed": filings_reviewed,
        "window": f"{filings[-1].filed} to {filings[0].filed}" if filings else "",
        **agg,
        "largest_transactions": [t.to_dict() for t in notable],
        "note": "Open-market trades only; grants, option exercises and tax withholding excluded from totals.",
    }


async def recent_for(company: Company, category: str) -> list[FilingRef]:
    """Newest filings for a report category, whatever form this company uses."""
    s = get_settings()
    limit = {"insider": s.form4_lookback, "current": s.eightk_lookback}.get(category, 1)
    return await get_edgar().recent_filings(company, CATEGORIES[category], limit)
