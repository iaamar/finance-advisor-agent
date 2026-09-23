"""What we summarize for each SEC form, and why.

Company-agnostic: rules are keyed by *form type*, so any registrant in the
SEC ticker file gets the same treatment. Workflows ask for a report category
(annual / quarterly / current / insider) and the newest filing of whichever
form the company actually uses fills it:

    annual    10-K (domestic) | 20-F (foreign private issuer) | 40-F (Canadian MJDS)
    quarterly 10-Q            | - (foreign issuers report interim results on 6-K)
    current   8-K             | 6-K
    insider   Form 4          (foreign private issuers are exempt, so often none)

An advisor getting up to speed wants: how the business is doing (numbers +
drivers), what management expects, what could go wrong, how cash goes back to
shareholders, and anything unusual. Each rule lists the sections that answer
those questions, in priority order, with a character budget so one filing
stays around 40-50K characters of LLM input. When a section isn't under its
standard Item heading (banks, 40-F exhibits) it is located by `title`.

Sections deliberately skipped:
  10-K: Items 10-14 (governance/comp, usually incorporated from the proxy),
        7A (market-risk boilerplate), 1B/1C/2/4/6, Item 8/15 financial
        statements (numbers come from XBRL instead - exact, not paraphrased).
  10-Q: Part I Item 1 statements (XBRL covers them), Item 3 market risk,
        Part II Item 6 exhibits, cover page and forward-looking boilerplate.
  20-F: Items 1-2, 6-7 (people/shareholders), 9-12 (offer/securities detail),
        17-19 (financial statements -> XBRL).
  8-K:  Item 9.01 (exhibit list only).
  Form 4: no LLM at all - parsed straight from XML (app/services/form4.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectionRule:
    key: str  # item key from split_sections(); "" = title lookup only
    label: str
    max_chars: int
    focus: str
    title: str = ""  # regex for the heading, used when the item key is missing/short
    skip_if_no_change: bool = False


MDNA = r"management.s discussion and analysis|operating and financial review"
RISKS = r"risk factors"

PERIODIC_RULES: dict[str, list[SectionRule]] = {
    "10-K": [
        SectionRule("7", "MD&A", 22000, "revenue/segment drivers, margin changes, liquidity, capital allocation, outlook", MDNA),
        SectionRule("1A", "Risk Factors", 14000, "the 5-7 most company-specific risks; ignore generic macro boilerplate", RISKS),
        SectionRule("1", "Business", 7000, "what the company sells, to whom, segments, competitive position"),
        SectionRule("5", "Market for Equity / Buybacks", 3000, "repurchases, dividends, remaining authorization"),
        SectionRule("3", "Legal Proceedings", 3000, "material litigation or regulatory actions"),
        SectionRule("9B", "Other Information", 2000, "insider 10b5-1 trading plan adoptions/terminations"),
    ],
    "10-Q": [
        SectionRule("I-2", "MD&A", 24000, "quarter results vs prior year/quarter, drivers, guidance, liquidity, commitments", MDNA),
        SectionRule("II-1A", "Risk Factor Changes", 8000, "only NEW or CHANGED risks vs the last 10-K", skip_if_no_change=True),
        SectionRule("II-2", "Buybacks", 3000, "shares repurchased, average price, remaining authorization"),
        SectionRule("II-1", "Legal Proceedings", 3000, "new or updated litigation"),
        SectionRule("II-5", "Other Information", 2500, "insider 10b5-1 plan adoptions/terminations"),
    ],
    "20-F": [
        SectionRule("5", "Operating & Financial Review (MD&A)", 22000, "revenue/segment drivers, margins, liquidity, outlook", MDNA),
        SectionRule("3", "Key Information / Risk Factors", 14000, "the 5-7 most company-specific risks (Item 3.D)", RISKS),
        SectionRule("4", "Information on the Company", 7000, "business overview, segments, customers, competitive position"),
        SectionRule("8", "Financial Information", 3000, "dividend policy and material legal proceedings"),
        SectionRule("16E", "Share Repurchases", 2500, "shares repurchased, average price, remaining program"),
    ],
    # 40-F wraps Canadian documents that are filed as exhibits (AIF, MD&A);
    # the summarizer appends exhibit text, then these title lookups apply.
    "40-F": [
        SectionRule("", "MD&A", 22000, "results, drivers, liquidity, outlook", MDNA),
        SectionRule("", "Risk Factors", 12000, "the 5-7 most company-specific risks", RISKS),
        SectionRule("", "Business", 7000, "what the company does, segments, competitive position",
                    r"description of the business|general development of the business"),
    ],
}
# Back-compat names.
TEN_K_SECTIONS = PERIODIC_RULES["10-K"]
TEN_Q_SECTIONS = PERIODIC_RULES["10-Q"]

# Sections scanned (in code) for red flags rather than summarized.
RED_FLAG_SECTIONS = {"10-K": ["9A", "9"], "10-Q": ["I-4"], "20-F": ["15", "16F"]}

EIGHT_K_ITEMS = {
    "1.01": "Entry into a material definitive agreement",
    "1.02": "Termination of a material definitive agreement",
    "1.05": "Material cybersecurity incident",
    "2.01": "Completion of acquisition or disposition of assets",
    "2.02": "Results of operations (earnings)",
    "2.03": "Creation of a direct financial obligation",
    "2.05": "Exit or restructuring costs",
    "2.06": "Material impairments",
    "3.01": "Delisting notice",
    "3.02": "Unregistered sale of equity",
    "4.01": "Change in certifying accountant",
    "4.02": "Non-reliance on prior financial statements",
    "5.02": "Departure/appointment of directors or officers",
    "5.03": "Amendment to articles or bylaws",
    "5.07": "Shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}

# What to extract from an 8-K, by item code. 6-Ks carry no item codes; their
# content is in EX-99 exhibits (press releases, interim results).
EIGHT_K_FOCUS = {
    "2.02": "headline results, segment revenue, margins, and the NEXT-QUARTER GUIDANCE figures from the press release",
    "1.01": "counterparty, what was agreed, dollar value, term",
    "2.03": "amount, instrument type, interest rate, maturity",
    "5.02": "who is leaving/joining, role, effective date, compensation terms",
    "1.05": "nature and scope of the incident, operational impact",
    "2.05": "size of charges, headcount, rationale",
    "2.06": "size and cause of impairment",
    "5.07": "which proposals passed or failed",
}
EIGHT_K_MAX_CHARS = 8000
EIGHT_K_EXHIBIT_MAX_CHARS = 18000

SYSTEM_PROMPT = """You summarize SEC filings for wealth-management advisors who need to get up to speed on a stock quickly.

Rules:
- Use only facts stated in the provided text. Never invent or estimate numbers; if a figure is not in the text, leave it out.
- Quote numbers exactly as written (units included, e.g. "$96.2 billion", "75.0%").
- Be specific to this company; skip boilerplate that applies to any public company.
- Write for a busy professional: short, concrete bullets.
- Do not give investment advice or buy/sell opinions."""
