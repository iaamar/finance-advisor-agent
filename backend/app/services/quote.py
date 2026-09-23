"""Quote data providers.

To plug in your own market-data API, subclass `QuoteProvider`, implement
`history_6m`, and register it in `PROVIDERS`; then set QUOTE_PROVIDER.
The workflow only needs the current price plus a 6-month daily close series,
everything else (delta, high/low) is computed in `compute_quote_metrics`.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings
from app.observability import drop_self, traceable


@dataclass
class PriceHistory:
    symbol: str
    currency: str
    price: float
    as_of: str  # ISO timestamp of the current price
    closes: list[tuple[str, float]]  # (ISO date, close), oldest first
    source: str


@dataclass
class QuoteMetrics:
    symbol: str
    currency: str
    stock_price: float
    as_of: str
    price_6m_ago: float
    date_6m_ago: str
    delta_6m_abs: float
    delta_6m_pct: float
    high_6m: float
    low_6m: float
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


class QuoteError(Exception):
    pass


class QuoteProvider:
    name = "base"

    async def history_6m(self, symbol: str) -> PriceHistory:
        raise NotImplementedError


class YahooChartProvider(QuoteProvider):
    """Free, keyless Yahoo Finance chart endpoint. Unofficial: fine for a
    prototype, swap for a licensed feed in production."""

    name = "yahoo"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)

    @traceable(run_type="tool", name="quote_provider.history_6m", process_inputs=drop_self,
               process_outputs=lambda h: {"symbol": h.symbol, "price": h.price, "points": len(h.closes)})
    async def history_6m(self, symbol: str) -> PriceHistory:
        resp = await self.http.get(self.URL.format(symbol=symbol), params={"range": "6mo", "interval": "1d"})
        if resp.status_code != 200:
            raise QuoteError(f"quote provider returned HTTP {resp.status_code} for {symbol}")
        body = resp.json().get("chart", {})
        if body.get("error") or not body.get("result"):
            raise QuoteError(f"no quote data for {symbol}")
        r = body["result"][0]
        meta = r["meta"]
        ts = r.get("timestamp") or []
        raw = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        closes = [
            (datetime.fromtimestamp(t, UTC).date().isoformat(), float(c))
            for t, c in zip(ts, raw)
            if c is not None and not math.isnan(c)
        ]
        price = meta.get("regularMarketPrice") or (closes[-1][1] if closes else None)
        if price is None or not closes:
            raise QuoteError(f"incomplete quote data for {symbol}")
        as_of = datetime.fromtimestamp(meta.get("regularMarketTime", ts[-1]), UTC).isoformat()
        return PriceHistory(symbol, meta.get("currency", "USD"), float(price), as_of, closes, self.name)


class MockProvider(QuoteProvider):
    """Deterministic data for tests / offline demos."""

    name = "mock"

    async def history_6m(self, symbol: str) -> PriceHistory:
        today = datetime.now(UTC).date()
        closes = [((today - timedelta(days=182 - i)).isoformat(), 100.0 + i * 0.25) for i in range(0, 183, 1)]
        return PriceHistory(symbol, "USD", closes[-1][1], datetime.now(UTC).isoformat(), closes, self.name)


PROVIDERS: dict[str, type[QuoteProvider]] = {"yahoo": YahooChartProvider, "mock": MockProvider}


def compute_quote_metrics(h: PriceHistory) -> QuoteMetrics:
    first_date, first_close = h.closes[0]
    values = [c for _, c in h.closes] + [h.price]
    delta = h.price - first_close
    return QuoteMetrics(
        symbol=h.symbol,
        currency=h.currency,
        stock_price=round(h.price, 2),
        as_of=h.as_of,
        price_6m_ago=round(first_close, 2),
        date_6m_ago=first_date,
        delta_6m_abs=round(delta, 2),
        delta_6m_pct=round(delta / first_close * 100, 2),
        high_6m=round(max(values), 2),
        low_6m=round(min(values), 2),
        source=h.source,
    )


_provider: QuoteProvider | None = None


def get_quote_provider() -> QuoteProvider:
    global _provider
    if _provider is None:
        _provider = PROVIDERS[get_settings().quote_provider]()
    return _provider


def set_quote_provider(p: QuoteProvider | None) -> None:
    """Test hook."""
    global _provider
    _provider = p
