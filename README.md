# Finance Advisor Agent

A conversational agent that helps wealth-management advisors get up to speed on a stock. It pulls current quote data and summarizes key information from recent SEC filings.

**Scope:** every company in the SEC's [`company_tickers.json`](https://www.sec.gov/files/company_tickers.json) (~10,400 registrants). Nothing is company-specific. US domestic filers, foreign private issuers (20-F/40-F/6-K, IFRS, non-USD), banks, and multi-class share structures all go through the same workflows.

```
frontend/  TypeScript + HTML + CSS chat UI (Vite)          -> text replies
backend/   FastAPI + LangGraph workflows + Redis memory     -> JSON
```

Stakeholder deck: [`presentation_deck/`](presentation_deck) ([PDF](presentation_deck/presentation_deck.pdf)).

## Architecture

```
Frontend (TS/HTML/CSS) ──POST /api/chat {message, conversation_id}──▶ Backend (FastAPI)
                                                                        │
  Conversation graph (app/graphs/conversation.py)                        │
  load_memory ─▶ understand ─┬─▶ Workflow #1 quote   ─┐                  │
     ▲ Redis                 ├─▶ Workflow #2 filings ─┼─▶ respond ─▶ save_memory ─▶ Redis
     │ conv:{id}:messages    └────────────────────────┘   (LLM, grounded       conv:{id}:context
     │ conv:{id}:context                                    in JSON only)
```

| Piece | File | Notes |
|---|---|---|
| **Workflow #1: quote** | `app/graphs/quote_workflow.py` | company name → resolve (SEC ticker map) → fetch 6M daily history → `{stock_price, summary, delta_6m…}`. All numbers are computed, not generated. |
| **Workflow #2: filings** | `app/graphs/filings_workflow.py` | resolve + filer profile → **parallel fan-out** over the requested *report categories* plus XBRL key financials → assemble |
| **Conversation wrapper** | `app/graphs/conversation.py` | routes each message to `quote`, `filings`, `briefing` (both workflows in parallel), `followup` (answer from memory), `clarify` ("did you mean…" when a name matches several registrants), or `chitchat` |
| Company resolution | `app/services/edgar.py` | ticker, `$TICKER`, exact or partial name, and colloquial aliases. Generic words ("First", "American") never auto-resolve; they return ranked candidates. Share classes collapse to one CIK. |
| **Short-term memory** | `app/memory.py` | Redis keyed by conversation ID: the last 20 messages plus the active company and its latest results (24h TTL). Follow-ups like "what about insider selling?" need no company name. |
| Quote provider | `app/services/quote.py` | pluggable; the default is the free Yahoo chart endpoint (unofficial, prototype-grade) |
| SEC client | `app/services/edgar.py` | ticker/name → CIK, submissions, archives, exhibits, companyfacts; rate-limited and cached |

## Report categories (form-agnostic)

Workflows ask for a **category**. The newest filing of whichever form the company actually files fills it:

| Category | US domestic filer | Foreign private issuer | If not filed |
|---|---|---|---|
| `annual` | 10-K | 20-F, or 40-F (Canada; content in exhibits) | explained note |
| `quarterly` | 10-Q | none (interim results arrive on 6-K) | "reports interim results on 6-K instead" |
| `current` | 8-K | 6-K | explained note |
| `insider` | Form 4 | usually exempt | "exempt from Form 4" |

Registrants with no periodic reports at all (such as unsponsored ADRs) get one clear note plus quote data. Form 4s are filtered to those where the company is the **issuer**: EDGAR also lists filings where the company is the *reporting owner* of another company's stock.

## What gets summarized for each form

| Form | Kept (in priority order, with character budgets) | Skipped | How |
|---|---|---|---|
| **10-K** | MD&A (Item 7), Risk Factors (1A, company-specific only), Business (1), Buybacks/dividends (5), Legal (3), Other info (9B, insider 10b5-1 plans) | Items 10–14 (in the proxy), 7A, 1B/1C/2/4/6, Items 8/15 (numbers come from XBRL instead) | LLM, structured output |
| **10-Q** | MD&A (Part I Item 2), **changed** risk factors (II-1A; dropped when it only says "no material changes"), Buybacks (II-2), Legal (II-1), Other info (II-5) | Part I Item 1 statements (XBRL), Item 3, exhibits | LLM, structured output |
| **20-F** | Operating & Financial Review (Item 5), Key Information / Risk Factors (3.D), Information on the Company (4), Financial Information (8: dividends, legal), Share Repurchases (16E); red flags from 15/16F | Items 1–2, 6–7, 9–12, 17–19 (statements → XBRL) | LLM, structured output |
| **40-F** | The MD&A, Risk Factors, and Business sections found **by title** in the EX-99 exhibits (AIF, MD&A) | wrapper boilerplate | LLM, structured output |
| **8-K** | Item codes mapped to plain English. For 2.02 (earnings), 7.01, and 8.01, the EX-99 press release is pulled, with guidance extracted. 1.01/2.03/5.02 get item-specific extraction. | 9.01 (exhibit list) | LLM (low effort) |
| **Form 4** | Insider, role, buy or sell, shares, price, value, holdings after, 10b5-1 flag. Totals are **open-market only** (grants, exercises, and tax withholding excluded). | – | **No LLM**: parsed from the XML |
| **6-K** | EX-99 exhibits (press releases, interim results) | – | LLM (low effort) |
| **Key financials** | Revenue, gross profit, operating income, net income, diluted EPS (latest quarter and fiscal year, with YoY); cash, assets, liabilities, equity, long-term debt | stale quarters (older than the latest fiscal year) | **No LLM**: XBRL companyfacts, **US-GAAP or IFRS**, in the company's **reporting currency** (USD, EUR, JPY, TWD…) |

Design choices:
- **Red flags are found in code, not by the LLM.** The controls sections (10-K 9A/9, 10-Q I-4) are regex-scanned for material weakness, ineffective controls, restatement, going concern, and auditor disagreement, with hedged or negated sentences ignored. 8-K items 3.01, 4.01, and 4.02 are always flagged.
- **Numbers shown to advisors come from XBRL or the quote feed.** The reply prompt only allows figures that appear in the JSON.
- Filing summaries are **cached by accession number** in Redis (a filing never changes once filed).
- Section splitting handles tables of contents, Part I/II item numbering in 10-Qs, running page headers (Microsoft), and sections outside the standard Item layout (banks such as JPMorgan, 40-F exhibits), which are located by title.
- Filing lookup pages into EDGAR's older submission files when the "recent" window (~1,000 filings) doesn't reach back far enough, which matters for banks with thousands of prospectus filings.
- With no `ANTHROPIC_API_KEY` everything still runs: a keyword router replaces the LLM router, and replies are templated from the deterministic data.

## Frontend

A single chat page (TypeScript, HTML, CSS; no framework):
- **Company search**: type-ahead over all SEC registrants. Picking one makes it the active company, shown as a pill.
- **Message box**: ask anything. The company can be named in the question instead, and follow-ups reuse the active company from Redis memory.
- **Output**: a text reply, a quote strip (price, 6-month change, range), and source chips linking to each EDGAR filing used. Ambiguous names show clickable "did you mean" options.

## Observability (LangSmith)

Every conversation turn is one trace. It contains the LangGraph nodes, both workflows and their parallel branches, LLM calls with token usage, SEC and quote API calls, and filing summaries (with cache hit or miss). Turns carry `thread_id` = conversation ID, so the project's **Threads** tab shows whole conversations.

| Environment | LangSmith project | Configure in |
|---|---|---|
| Local | `finance-advisor-agent-local` | `backend/.env`: `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=…` |
| Production (Fly.io) | `finance-advisor-agent-prod` | `fly secrets set LANGSMITH_API_KEY=…` (the rest is in `fly.toml`) |

- Dashboard: https://smith.langchain.com → Projects → pick the project above.
- `GET /api/observability` returns the direct project link for that environment.
- Each chat answer shows a **View trace ↗** link when tracing is on.

## Running it

```bash
redis-server --daemonize yes            # or: docker compose up -d redis
cd backend && cp .env.example .env      # set ANTHROPIC_API_KEY and SEC_USER_AGENT
uv sync && uv run uvicorn app.main:app --port 8000 --reload
```

```bash
cd frontend && npm install && npm run dev     # http://localhost:5173 (proxies /api to :8000)
```

Tests (offline; EDGAR, quotes, LLM, and Redis are all faked):

```bash
cd backend && uv run pytest -q
```

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/chat` | `{message, conversation_id?, company?}` | `{conversation_id, reply, intent, company, candidates, data, errors}` (`company` = ticker picked in the UI) |
| GET | `/api/companies?q=` | – | autocomplete over the full SEC ticker list |
| POST | `/api/workflows/quote` | `{company}` | Workflow #1 JSON: `{stock_price, summary, delta_6m, …}` |
| POST | `/api/workflows/filings` | `{company, categories?}` | Workflow #2 JSON, per category (`annual`, `quarterly`, `current`, `insider`; form names like `10-K` also accepted) |
| GET / DELETE | `/api/conversations/{id}` | – | history / clear |
| GET | `/api/health` | – | LLM, memory, and provider status |

## Plugging in your quote API

Subclass `QuoteProvider` in `backend/app/services/quote.py`, implement `history_6m(symbol)` so it returns the current price and daily closes for the last 6 months, register it in `PROVIDERS`, and set `QUOTE_PROVIDER=<name>`. The delta, high/low, and summary are computed from that data.
