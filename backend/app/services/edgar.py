"""SEC EDGAR client: company lookup, filing index, documents, XBRL facts.

Scope: every registrant in https://www.sec.gov/files/company_tickers.json.
Nothing here is company-specific; the same code path serves US domestic
filers (10-K/10-Q/8-K), foreign private issuers (20-F/40-F/6-K), banks, etc.

EDGAR rules we honour:
  * descriptive User-Agent header (SEC_USER_AGENT)
  * < 10 requests/second (simple async rate limiter)
  * zero-padded 10-digit CIK for data.sec.gov, un-padded CIK in archive URLs
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.memory import get_store
from app.observability import drop_self, traceable

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# Report categories -> the SEC form types that fill that role, for domestic
# and foreign filers alike. Workflows ask for a category, never a form.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "annual": ("10-K", "20-F", "40-F"),
    "quarterly": ("10-Q",),
    "current": ("8-K", "6-K"),
    "insider": ("4",),
}
CATEGORY_LABELS = {
    "annual": "Annual report (10-K / 20-F / 40-F)",
    "quarterly": "Quarterly report (10-Q)",
    "current": "Current reports (8-K / 6-K)",
    "insider": "Insider transactions (Form 4)",
}
FORM_TO_CATEGORY = {form: cat for cat, forms in CATEGORIES.items() for form in forms}

# A few colloquial names that don't prefix-match the SEC registrant title.
ALIASES = {
    "google": "GOOGL",
    "facebook": "META",
    "amazon": "AMZN",
    "jp morgan": "JPM",
    "jpmorgan": "JPM",
    "jp morgan chase": "JPM",
    "berkshire": "BRK-B",
    "exxon": "XOM",
    "tsmc": "TSM",
}

_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|sa|se|nv|ag|lp|llc|"
    r"oyj|asa|ab|spa|bv|adr|class [a-c]|new|de)\b\.?",
    re.I,
)
# Words that are also tickers or common in questions; never treat as a ticker.
_NOT_TICKERS = {
    "A", "I", "AN", "AND", "ARE", "AT", "BE", "BY", "CAN", "CEO", "CFO", "DO", "EPS", "FOR", "GO", "HAS", "HOW",
    "IN", "IS", "IT", "ITS", "ME", "MY", "NEW", "NOW", "OF", "ON", "ONE", "OR", "OUT", "SEC", "SO", "THE", "TO",
    "UP", "US", "USA", "WAS", "WE", "WHAT", "WHO", "YOU", "YTD", "YOY", "Q", "K", "FORM", "ANY", "ALL", "BIG",
}


# Leading words shared by many registrants: a bare "First" or "American" is
# ambiguous, so it never resolves on a prefix match alone.
_GENERIC_WORDS = {
    "first", "american", "america", "united", "national", "general", "international", "global", "bank", "capital",
    "energy", "financial", "royal", "southern", "western", "eastern", "northern", "pacific", "atlantic", "china",
    "great", "community", "home", "health", "medical", "technologies", "technology", "trust", "investors",
    "invesco", "ishares", "fund", "income", "growth", "real", "estate", "resources", "mining", "gold", "oil",
    "power", "data", "digital", "life", "new", "north", "south", "east", "west", "city", "state", "us", "the",
}
# Words that start questions; never treat them as the start of a company name.
_QUESTION_WORDS = {w.lower() for w in _NOT_TICKERS} | {
    "tell", "show", "give", "get", "latest", "recent", "stock", "stocks", "price", "quote", "compare", "summarize",
    "summary", "about", "please", "news", "report", "insider", "annual", "quarterly", "filing", "filings", "risk",
    "risks", "hi", "hello", "thanks", "why", "when", "which", "did", "does", "should", "could", "would", "brief",
}


def _norm(name: str) -> str:
    name = name.lower().replace("&", " and ")
    name = re.sub(r"/[a-z]{2,3}/", " ", name)  # "/DE/", "/NEW/" state suffixes
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = _SUFFIX_RE.sub(" ", name)
    name = re.sub(r"\band\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class Company:
    ticker: str
    cik: int
    name: str

    @property
    def cik10(self) -> str:
        return f"{self.cik:010d}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FilingRef:
    form: str
    accession: str
    filed: str
    period: str
    primary_doc: str
    items: list[str]
    url: str
    index_url: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NoDataError(Exception):
    """The company exists in the ticker file but EDGAR has no data of this kind."""


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / rps
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class EdgarClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        s = get_settings()
        self.http = http or httpx.AsyncClient(
            headers={"User-Agent": s.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=30.0,
            follow_redirects=True,
        )
        self.limiter = _RateLimiter(s.sec_max_rps)
        self._tickers: list[Company] | None = None
        self._by_ticker: dict[str, Company] = {}
        self._norm_names: list[str] = []
        self._prefix_index: dict[str, int] = {}  # first 1-3 words of a name -> first (largest) company

    @traceable(run_type="tool", name="sec_edgar.get", process_inputs=drop_self, process_outputs=lambda r: {
        "status": getattr(r, "status_code", None), "bytes": len(getattr(r, "content", b"") or b"")
    })
    async def _get(self, url: str) -> httpx.Response:
        await self.limiter.wait()
        resp = await self.http.get(url)
        resp.raise_for_status()
        return resp

    async def get_json(self, url: str) -> Any:
        return (await self._get(url)).json()

    async def get_text(self, url: str) -> str:
        return (await self._get(url)).text

    # ---- company universe (company_tickers.json) ---------------------------
    async def _load_tickers(self) -> list[Company]:
        if self._tickers is None:
            store = await get_store()
            data = await store.cache_get("sec:tickers")
            if data is None:
                data = await self.get_json(TICKERS_URL)
                await store.cache_set("sec:tickers", data, ttl=60 * 60 * 24)
            self._tickers = [
                Company(ticker=v["ticker"].upper(), cik=int(v["cik_str"]), name=v["title"]) for v in data.values()
            ]
        if not self._by_ticker:
            # The file is ordered by market cap, so for share classes of the
            # same company the first (largest) listing wins.
            for c in self._tickers:
                self._by_ticker.setdefault(c.ticker, c)
            self._norm_names = [_norm(c.name) for c in self._tickers]
            for i, n in enumerate(self._norm_names):
                words = n.split()
                for k in range(1, min(3, len(words)) + 1):
                    self._prefix_index.setdefault(" ".join(words[:k]), i)
        return self._tickers

    async def search(self, query: str, limit: int = 8) -> list[Company]:
        """Rank companies for a free-text query (ticker or name). One entry per CIK."""
        companies = await self._load_tickers()
        q = query.strip().strip("$").strip()
        if not q:
            return []
        scored: list[tuple[float, int, Company]] = []

        def add(score: float, idx: int, c: Company) -> None:
            scored.append((score, -idx, c))

        tq = q.upper().replace(".", "-")
        if tq in self._by_ticker:
            c = self._by_ticker[tq]
            add(100, companies.index(c), c)
        alias = ALIASES.get(q.lower())
        if alias and alias in self._by_ticker:
            add(99, 0, self._by_ticker[alias])

        nq = _norm(q)
        if nq:
            compact = nq.replace(" ", "")
            for i, (c, n) in enumerate(zip(companies, self._norm_names)):
                if n == nq or n.replace(" ", "") == compact:
                    add(95, i, c)
                elif len(nq) >= 3 and n.startswith(nq + " "):
                    add(85, i, c)
                elif len(nq) >= 4 and n.startswith(nq):
                    add(75, i, c)
                elif len(nq) >= 4 and f" {nq}" in f" {n}":
                    add(60, i, c)
            if len(scored) < limit:
                for m in difflib.get_close_matches(nq, self._norm_names, n=limit, cutoff=0.8):
                    i = self._norm_names.index(m)
                    add(50 * difflib.SequenceMatcher(None, nq, m).ratio(), i, companies[i])

        seen: set[int] = set()
        out: list[Company] = []
        for _, _, c in sorted(scored, key=lambda t: (t[0], t[1]), reverse=True):
            if c.cik not in seen:
                seen.add(c.cik)
                out.append(c)
            if len(out) >= limit:
                break
        return out

    async def resolve(self, query: str) -> Company | None:
        """Best match only when it's confident; otherwise None (use `search` for candidates)."""
        matches = await self.search(query, limit=2)
        if not matches:
            return None
        top = matches[0]
        q = query.strip().strip("$")
        tq = q.upper().replace(".", "-")
        nq = _norm(q)
        n = _norm(top.name)
        specific = nq not in _GENERIC_WORDS and not all(w in _GENERIC_WORDS for w in nq.split())
        confident = (
            tq == top.ticker
            or ALIASES.get(q.lower()) == top.ticker
            or n == nq
            or n.replace(" ", "") == nq.replace(" ", "")
            or (specific and len(nq) >= 3 and n.startswith(nq))
            or len(matches) == 1
        )
        return top if confident else None

    async def find_in_text(self, text: str) -> Company | None:
        """Spot a company mention inside a sentence (used by the no-LLM router).

        Tries explicit tickers ("$KO", "AMZN") and then word n-grams (longest
        first) that exactly match a registrant's normalized name.
        """
        await self._load_tickers()
        for tok in re.findall(r"\$?\b[A-Z][A-Z.\-]{0,5}\b", text):
            t = tok.lstrip("$").replace(".", "-")
            if (tok.startswith("$") or t not in _NOT_TICKERS) and t in self._by_ticker and len(t) >= 2:
                return self._by_ticker[t]
        words = re.findall(r"[A-Za-z0-9&.'\-]+", text)
        name_set = {n: i for i, n in reversed(list(enumerate(self._norm_names)))}
        companies = self._tickers or []
        for size in (5, 4, 3, 2, 1):
            for i in range(len(words) - size + 1):
                chunk = words[i : i + size]
                phrase = " ".join(chunk)
                if phrase.lower() in ALIASES:
                    return self._by_ticker.get(ALIASES[phrase.lower()])
                n = _norm(phrase)
                if not n or len(n) < 3 or chunk[0].lower() in _QUESTION_WORDS:
                    continue
                if n in name_set:
                    return companies[name_set[n]]
                # Capitalised phrase = the leading word(s) of a registrant's name ("Ford", "Coca Cola").
                if chunk[0][0].isupper() and n in self._prefix_index and n not in _GENERIC_WORDS and len(n) >= 4:
                    return companies[self._prefix_index[n]]
        return None

    # ---- filings ------------------------------------------------------------
    async def _submissions(self, company: Company) -> dict[str, Any]:
        store = await get_store()
        key = f"sec:submissions:{company.cik10}"
        sub = await store.cache_get(key)
        if sub is None:
            sub = await self.get_json(SUBMISSIONS_URL.format(cik10=company.cik10))
            await store.cache_set(key, sub, ttl=60 * 15)
        return sub

    def _rows(self, company: Company, block: dict[str, list], forms: tuple[str, ...]) -> list[FilingRef]:
        out: list[FilingRef] = []
        n = len(block.get("form", []))

        def col(name: str, i: int) -> str:
            vals = block.get(name) or []
            return (vals[i] if i < len(vals) else "") or ""

        for i in range(n):
            form = block["form"][i]
            if form not in forms:
                continue
            acc = block["accessionNumber"][i]
            acc_nodash = acc.replace("-", "")
            doc = col("primaryDocument", i)
            if form == "4" and "/" in doc:
                doc = doc.split("/", 1)[1]  # strip xslF345X0n/ rendering prefix -> raw XML
            out.append(
                FilingRef(
                    form=form,
                    accession=acc,
                    filed=col("filingDate", i),
                    period=col("reportDate", i),
                    primary_doc=doc,
                    items=[x for x in col("items", i).split(",") if x],
                    url=ARCHIVE_URL.format(cik=company.cik, acc=acc_nodash, doc=doc),
                    index_url=ARCHIVE_URL.format(cik=company.cik, acc=acc_nodash, doc="index.json"),
                    description=col("primaryDocDescription", i),
                )
            )
        return out

    async def recent_filings(self, company: Company, forms: str | tuple[str, ...], limit: int) -> list[FilingRef]:
        """Newest-first filings of the given form type(s).

        Looks in the "recent" block first and pages into older submission
        files only when it hasn't found enough (large filers overflow ~1000).
        """
        forms = (forms,) if isinstance(forms, str) else forms
        sub = await self._submissions(company)
        found = self._rows(company, sub["filings"]["recent"], forms)
        for f in sub["filings"].get("files", [])[:3]:
            if len(found) >= limit:
                break
            older = await self.get_json(SUBMISSIONS_FILE_URL.format(name=f["name"]))
            found += self._rows(company, older, forms)
        found.sort(key=lambda r: r.filed, reverse=True)
        return found[:limit]

    async def filing_profile(self, company: Company) -> dict[str, Any]:
        """Which periodic/current report types this registrant actually files."""
        sub = await self._submissions(company)
        forms = set(sub["filings"]["recent"]["form"])
        return {
            "filer_type": (
                "foreign private issuer" if forms & {"20-F", "40-F", "6-K"} and not forms & {"10-K", "10-Q"}
                else "domestic" if forms & {"10-K", "10-Q", "8-K"}
                else "no periodic reports"
            ),
            "has": {cat: bool(forms & set(fs)) for cat, fs in CATEGORIES.items()},
            "sic_description": sub.get("sicDescription", ""),
            "exchanges": sub.get("exchanges", []),
            "fiscal_year_end": sub.get("fiscalYearEnd", ""),
        }

    async def exhibits(self, filing: FilingRef, type_prefix: str = "EX-99") -> list[tuple[str, str]]:
        """(exhibit type, url) pairs, e.g. [("EX-99.1", ".../q2fy27pr.htm")].

        File names are arbitrary, so we read the declared document types from
        the filing's -index.html page rather than guessing from names.
        """
        base = filing.index_url.rsplit("/", 1)[0]
        html = await self.get_text(f"{base}/{filing.accession}-index.html")
        soup = BeautifulSoup(html, "lxml")
        out: list[tuple[str, str]] = []
        for row in soup.select("table.tableFile tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            link = cells[2].find("a")
            doc_type = cells[3].get_text(strip=True).upper()
            name = link.get_text(strip=True) if link else ""
            if link and doc_type.startswith(type_prefix) and name.lower().endswith((".htm", ".html", ".txt")):
                out.append((doc_type, f"{base}/{name}"))
        return sorted(out)

    async def company_facts(self, company: Company) -> dict[str, Any]:
        store = await get_store()
        key = f"sec:facts:{company.cik10}"
        facts = await store.cache_get(key)
        if facts is None:
            try:
                facts = await self.get_json(FACTS_URL.format(cik10=company.cik10))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise NoDataError(f"EDGAR has no XBRL financial data for {company.name}.") from exc
                raise
            await store.cache_set(key, facts, ttl=60 * 60 * 6)
        return facts


_client: EdgarClient | None = None


def get_edgar() -> EdgarClient:
    global _client
    if _client is None:
        _client = EdgarClient()
    return _client


def set_edgar(client: EdgarClient | None) -> None:
    """Test hook."""
    global _client
    _client = client
