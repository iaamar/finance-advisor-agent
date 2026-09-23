from fastapi.testclient import TestClient

from app.graphs import conversation
from app.graphs.conversation import Route, chat
from app.graphs.filings_workflow import run_filings_workflow
from app.graphs.quote_workflow import run_quote_workflow
from app.llm import set_llm
from app.services.edgar import FilingRef
from tests.conftest import FORM4_DOC, FakeLLM


async def test_quote_workflow_contract():
    out = await run_quote_workflow("nvidia")
    assert out["stock_price"] == 145.5
    assert out["company"]["ticker"] == "NVDA"
    assert out["delta_6m"]["from_price"] == 100.0 and out["delta_6m"]["pct"] == 45.5
    assert "NVIDIA" in out["summary"] and "+45.50%" in out["summary"]


async def test_quote_workflow_unknown_company():
    out = await run_quote_workflow("Totally Fake Widgets")
    assert out["stock_price"] is None and "Couldn't find" in out["error"]


def _ref(form, acc, url, items=()):
    return FilingRef(form, acc, "2026-08-26", "2026-07-26", url.rsplit("/", 1)[1], list(items), url, url + "/index.json")


async def test_filings_workflow_fans_out_only_requested_forms(isolated):
    edgar = isolated["edgar"]
    edgar.filings["4"] = [_ref("4", "a-1", "https://sec/f4.xml")]
    edgar.docs["https://sec/f4.xml"] = FORM4_DOC

    async def no_facts(company):
        return {"facts": {}}

    edgar.company_facts = no_facts
    out = await run_filings_workflow("NVDA", ["insider"])
    assert set(out) == {"company", "profile", "key_financials", "insider", "errors"}
    assert out["insider"]["open_market_sells"] == 1
    # Form names are accepted as aliases for categories.
    assert "insider" in await run_filings_workflow("NVDA", ["4"])


async def test_filings_workflow_llm_summary_and_cache(isolated):
    edgar = isolated["edgar"]
    url = "https://sec/q.htm"
    edgar.filings["10-Q"] = [_ref("10-Q", "q-1", url)]
    edgar.docs[url] = (
        "<p>PART I</p><p>Item 2. MD&amp;A</p><p>" + "Revenue rose strongly. " * 30 + "</p>"
        "<p>Item 4. Controls</p><p>We identified a material weakness in our internal control over leases.</p>"
    )
    llm = FakeLLM()
    set_llm(llm)
    out = await run_filings_workflow("NVDA", ["quarterly"])
    q = out["quarterly"]
    assert q["headline"] == "headline text" and q["sections_used"] == ["MD&A"]
    assert q["red_flags"] and "material weakness" in q["red_flags"][0]
    # Second call is served from the accession-number cache: no new LLM call.
    n = len(llm.calls)
    await run_filings_workflow("NVDA", ["quarterly"])
    assert len(llm.calls) == n


async def test_conversation_memory_carries_company(monkeypatch, isolated):
    calls = []

    async def fake_filings(company, categories):
        calls.append((company, categories))
        return {"company": {"ticker": "NVDA", "cik": 1045810, "name": "NVIDIA CORP"}, "insider": {"headline": "h"}, "errors": []}

    monkeypatch.setattr(conversation, "run_filings_workflow", fake_filings)

    set_llm(FakeLLM(Route(intent="quote", company="NVIDIA", categories=[])))
    r1 = await chat("c1", "How's NVIDIA doing?")
    assert r1["intent"] == "quote" and r1["company"]["ticker"] == "NVDA"

    # Follow-up with no company: router returns empty company -> memory supplies it.
    set_llm(FakeLLM(Route(intent="filings", company="", categories=["insider"])))
    r2 = await chat("c1", "any insider selling?")
    assert calls == [("NVDA", ["insider"])]
    assert r2["reply"] == "LLM reply"

    store = isolated["store"]
    ctx = await store.get_context("c1")
    assert ctx["company"]["ticker"] == "NVDA" and "quote" in ctx and "insider" in ctx["filings"]
    assert len(await store.get_history("c1")) == 4

    # A follow-up answerable from memory triggers no workflow.
    set_llm(FakeLLM(Route(intent="followup", company="", categories=["insider"])))
    r3 = await chat("c1", "who sold the most?")
    assert r3["intent"] == "followup" and len(calls) == 1


async def test_heuristic_router_without_llm():
    r = await chat("c2", "What's the stock price of Apple?")
    assert r["intent"] == "quote" and r["company"]["ticker"] == "AAPL"
    assert "Apple Inc." in r["reply"]
    r = await chat("c3", "hello")
    assert r["intent"] == "chitchat" and "Which company" in r["reply"]


def test_http_api():
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/chat", json={"message": "quote for nvidia"})
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] and body["data"]["quote"]["stock_price"] == 145.5
    r = client.post("/api/workflows/quote", json={"company": "NVDA"})
    assert r.json()["stock_price"] == 145.5
    assert client.post("/api/workflows/quote", json={"company": "zzzz nothing"}).status_code == 404


async def test_foreign_issuer_uses_20f_and_explains_missing_categories(isolated):
    edgar = isolated["edgar"]
    edgar.profile = {"filer_type": "foreign private issuer", "has": {}}
    url = "https://sec/tsm-20f.htm"
    edgar.filings["20-F"] = [_ref("20-F", "t-1", url)]
    edgar.docs[url] = (
        "<p>Item 3. Key Information</p><p>D. Risk Factors " + "Geopolitical risk. " * 40 + "</p>"
        "<p>Item 5. Operating and Financial Review and Prospects</p><p>" + "Revenue grew. " * 40 + "</p>"
    )
    set_llm(FakeLLM())
    out = await run_filings_workflow("TSM", ["annual", "quarterly", "insider"])
    assert out["annual"]["form"] == "20-F"
    assert out["annual"]["sections_used"] == ["Operating & Financial Review (MD&A)", "Key Information / Risk Factors"]
    assert any("6-K instead" in e for e in out["errors"])
    assert any("exempt from Form 4" in e for e in out["errors"])


async def test_clarify_when_company_is_ambiguous():
    set_llm(FakeLLM(Route(intent="quote", company="First", categories=[])))
    r = await chat("c9", "price of First?")
    assert r["intent"] == "clarify" and {c["ticker"] for c in r["candidates"]} == {"FCNCA", "FRME"}
    assert "Which one did you mean" in r["reply"]


async def test_company_picked_in_ui_becomes_active():
    r = await chat("c10", "get me up to speed", company_hint="KO")
    assert r["company"]["ticker"] == "KO" and r["intent"] == "briefing"


def test_companies_search_endpoint():
    from app.main import app

    client = TestClient(app)
    body = client.get("/api/companies", params={"q": "coca"}).json()
    assert body["results"][0]["ticker"] == "KO"


async def test_form4_where_company_is_reporting_owner_is_excluded(isolated):
    edgar = isolated["edgar"]
    own = FORM4_DOC.replace("<ownershipDocument>", "<ownershipDocument><issuer><issuerCik>0001045810</issuerCik></issuer>")
    other = FORM4_DOC.replace("<ownershipDocument>", "<ownershipDocument><issuer><issuerCik>0000999999</issuerCik></issuer>")
    edgar.filings["4"] = [_ref("4", "own-1", "https://sec/own.xml"), _ref("4", "oth-1", "https://sec/other.xml")]
    edgar.docs.update({"https://sec/own.xml": own, "https://sec/other.xml": other})
    out = await run_filings_workflow("NVDA", ["insider"])
    assert out["insider"]["filings_reviewed"] == 1 and out["insider"]["open_market_sells"] == 1


async def test_registrant_without_periodic_reports_gets_one_note(isolated):
    isolated["edgar"].profile = {"filer_type": "no periodic reports", "has": {}}
    out = await run_filings_workflow("NVDA")
    assert len(out["errors"]) == 1 and "no periodic reports" in out["errors"][0]


async def test_heuristic_router_offers_candidates_for_unknown_name():
    r = await chat("c11", "price of First?")
    assert r["intent"] == "clarify" and r["candidates"]


async def test_did_you_mean_pick_overrides_ambiguous_name():
    r = await chat("c12", "price of First?")
    assert r["intent"] == "clarify"
    r = await chat("c12", "price of First?", company_hint="FRME")
    assert r["intent"] == "quote" and r["company"]["ticker"] == "FRME"
