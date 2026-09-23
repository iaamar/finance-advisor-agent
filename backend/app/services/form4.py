"""Form 4 (insider transactions) parser. Pure XML parsing, no LLM."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from xml.etree import ElementTree as ET

CODE_MEANING = {
    "P": "open-market buy",
    "S": "open-market sell",
    "A": "grant/award",
    "M": "option exercise",
    "F": "shares withheld for tax",
    "G": "gift",
    "C": "conversion",
    "D": "disposition to issuer",
    "J": "other",
}


@dataclass
class InsiderTxn:
    insider: str
    role: str
    date: str
    code: str
    action: str
    acquired: bool
    shares: float
    price: float | None
    value: float | None
    owned_after: float | None
    planned_10b5_1: bool
    issuer_cik: int | None = None
    filed: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _v(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    val = node.find("value")
    text = (val.text if val is not None else node.text) or ""
    return text.strip() or None


def _f(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def parse_form4(xml: str) -> list[InsiderTxn]:
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    issuer_cik = _f(_v(root, "issuer/issuerCik"))
    owner = root.find("reportingOwner")
    name = _v(owner, "reportingOwnerId/rptOwnerName") or "Unknown"
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    roles = []
    if rel is not None:
        if (_v(rel, "isDirector") or "").lower() in ("1", "true"):
            roles.append("Director")
        if (_v(rel, "isOfficer") or "").lower() in ("1", "true"):
            roles.append(_v(rel, "officerTitle") or "Officer")
        if (_v(rel, "isTenPercentOwner") or "").lower() in ("1", "true"):
            roles.append("10% owner")
    role = ", ".join(roles) or "Insider"

    footnotes = " ".join((fn.text or "") for fn in root.iter("footnote")).lower()
    plan_flag = (_v(root, "aff10b5One") or "").lower() in ("1", "true") or "10b5-1" in footnotes

    txns: list[InsiderTxn] = []
    for t in root.iter("nonDerivativeTransaction"):
        code = _v(t, "transactionCoding/transactionCode") or ""
        shares = _f(_v(t, "transactionAmounts/transactionShares")) or 0.0
        price = _f(_v(t, "transactionAmounts/transactionPricePerShare"))
        ad = _v(t, "transactionAmounts/transactionAcquiredDisposedCode") or ""
        txns.append(
            InsiderTxn(
                insider=name.title(),
                role=role,
                date=_v(t, "transactionDate") or "",
                code=code,
                action=CODE_MEANING.get(code, code),
                acquired=ad == "A",
                shares=shares,
                price=price,
                value=round(shares * price, 2) if price else None,
                owned_after=_f(_v(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                planned_10b5_1=plan_flag,
                issuer_cik=int(issuer_cik) if issuer_cik else None,
            )
        )
    return txns


def summarize_insider_activity(txns: list[InsiderTxn]) -> dict:
    """Aggregate open-market activity; grants/exercises/tax withholding are noise."""
    buys = [t for t in txns if t.code == "P"]
    sells = [t for t in txns if t.code == "S"]
    buy_val = sum(t.value or 0 for t in buys)
    sell_val = sum(t.value or 0 for t in sells)
    by_insider: dict[str, dict] = {}
    for t in buys + sells:
        d = by_insider.setdefault(t.insider, {"insider": t.insider, "role": t.role, "net_shares": 0.0, "net_value": 0.0})
        sign = 1 if t.code == "P" else -1
        d["net_shares"] += sign * t.shares
        d["net_value"] += sign * (t.value or 0)
    return {
        "open_market_buys": len(buys),
        "open_market_sells": len(sells),
        "buy_value": round(buy_val, 2),
        "sell_value": round(sell_val, 2),
        "net_value": round(buy_val - sell_val, 2),
        "planned_10b5_1_sells": sum(1 for t in sells if t.planned_10b5_1),
        "by_insider": sorted(by_insider.values(), key=lambda d: d["net_value"]),
        "headline": (
            f"{len(sells)} open-market sale(s) totalling ${sell_val:,.0f} and {len(buys)} purchase(s) "
            f"totalling ${buy_val:,.0f}"
            + (f"; {sum(1 for t in sells if t.planned_10b5_1)} sale(s) under pre-set 10b5-1 plans" if sells else "")
        ),
    }
