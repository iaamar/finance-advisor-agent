"""Company resolution works for any registrant in the ticker file, not a fixed list."""

from app.services.edgar import get_edgar


async def test_resolve_by_ticker_name_and_colloquial_name():
    e = get_edgar()
    assert (await e.resolve("KO")).ticker == "KO"
    assert (await e.resolve("$ko")).ticker == "KO"
    assert (await e.resolve("Coca-Cola")).ticker == "KO"
    assert (await e.resolve("bank of america")).ticker == "BAC"  # "/DE/" suffix ignored
    assert (await e.resolve("Taiwan Semiconductor")).ticker == "TSM"
    assert (await e.resolve("tsmc")).ticker == "TSM"
    assert (await e.resolve("Alphabet")).ticker == "GOOGL"  # first share class (largest) wins


async def test_ambiguous_or_unknown_returns_none_with_candidates():
    e = get_edgar()
    assert await e.resolve("first") is None
    names = [c.ticker for c in await e.search("first")]
    assert set(names) == {"FCNCA", "FRME"}
    assert await e.resolve("Totally Fake Widgets") is None
    assert len({c.cik for c in await e.search("alphabet")}) == 1  # share classes collapse to one CIK


async def test_find_company_in_free_text():
    e = get_edgar()
    assert (await e.find_in_text("how is coca cola doing lately")).ticker == "KO"
    assert (await e.find_in_text("What's the latest on $TSM?")).ticker == "TSM"
    assert (await e.find_in_text("Tell me about Ford")).ticker == "F"
    assert await e.find_in_text("WHAT IS THE SEC doing") is None  # common words aren't tickers
