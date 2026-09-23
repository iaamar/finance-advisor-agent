from __future__ import annotations

import fakeredis
import pytest

from app.llm import set_llm
from app.memory import MemoryStore, set_store
from app.services.edgar import Company, EdgarClient, FilingRef, set_edgar
from app.services.quote import MockProvider, set_quote_provider

FORM4_DOC = """<?xml version="1.0"?>
<ownershipDocument>
  <aff10b5One>1</aff10b5One>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-01</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>200</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-01</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>50</value></transactionShares>
        <transactionPricePerShare><value>200</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

# A small slice of company_tickers.json covering the shapes the resolver must handle.
COMPANIES = [
    Company("NVDA", 1045810, "NVIDIA CORP"),
    Company("AAPL", 320193, "Apple Inc."),
    Company("KO", 21344, "COCA COLA CO"),
    Company("BAC", 70858, "BANK OF AMERICA CORP /DE/"),
    Company("TSM", 1046179, "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"),
    Company("F", 37996, "FORD MOTOR CO"),
    Company("FCNCA", 798941, "FIRST CITIZENS BANCSHARES INC /DE/"),
    Company("FRME", 712534, "FIRST MERCHANTS CORP"),
    Company("GOOGL", 1652044, "Alphabet Inc."),
    Company("GOOG", 1652044, "Alphabet Inc."),
]


class FakeEdgar(EdgarClient):
    def __init__(self) -> None:
        super().__init__()
        self._tickers = COMPANIES
        self.docs: dict[str, str] = {}
        self.filings: dict[str, list[FilingRef]] = {}
        self.profile = {"filer_type": "domestic", "has": {}}

    async def get_text(self, url: str) -> str:
        return self.docs[url]

    async def recent_filings(self, company, forms, limit):
        forms = (forms,) if isinstance(forms, str) else forms
        rows = [f for form in forms for f in self.filings.get(form, [])]
        return sorted(rows, key=lambda r: r.filed, reverse=True)[:limit]

    async def filing_profile(self, company):
        return self.profile

    async def exhibits(self, filing, type_prefix="EX-99"):
        return []


class FakeLLM:
    """Records calls; returns canned structured outputs / text."""

    available = True

    def __init__(self, route=None) -> None:
        self.route = route
        self.calls: list[tuple[str, str]] = []

    async def parse(self, system, user, schema, **kw):
        self.calls.append(("parse", schema.__name__))
        if schema.__name__ == "Route":
            return self.route
        fields = {
            name: ([] if "list" in str(f.annotation) else f"{name} text") for name, f in schema.model_fields.items()
        }
        return schema(**fields)

    async def text(self, system, messages, **kw):
        self.calls.append(("text", messages[-1]["content"]))
        return "LLM reply"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    store = MemoryStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    set_store(store)
    edgar = FakeEdgar()
    set_edgar(edgar)
    set_quote_provider(MockProvider())

    class NoLLM:
        available = False

    set_llm(NoLLM())
    yield {"store": store, "edgar": edgar}
    set_store(None)
    set_edgar(None)
    set_quote_provider(None)
    set_llm(None)
