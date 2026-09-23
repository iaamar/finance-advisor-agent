from app.services.form4 import parse_form4, summarize_insider_activity
from app.services.text import find_mdna_by_title, html_to_text, scan_red_flags, split_sections
from app.services.xbrl import format_amount, key_financials
from tests.conftest import FORM4_DOC as FORM4

TEN_Q = """
<html><body>
<div style="display:none"><ix:header>hidden facts</ix:header></div>
<p>PART I</p><p>Item 1. Financial Statements</p><p>1</p>
<p>Item 2. Management's Discussion</p><p>5</p>
<p>PART II</p><p>Item 1A. Risk Factors</p><p>9</p>
<p>PART I. FINANCIAL INFORMATION</p>
<p>Item 1. Financial Statements</p><p>Balance sheet stuff.</p>
<p>Item 2. Management's Discussion and Analysis</p><p>Revenue grew 20%.</p>
<p>PART I</p><p>Item 2</p><p>Page two of MD&amp;A: margins expanded.</p>
<p>Item 4. Controls and Procedures</p><p>Controls were effective.</p>
<p>PART II. OTHER INFORMATION</p>
<p>Item 1A. Risk Factors</p><p>There have been no material changes to our risk factors.</p>
</body></html>
"""


def test_split_sections_handles_toc_part_numbers_and_page_headers():
    text = html_to_text(TEN_Q)
    assert "hidden facts" not in text
    s = split_sections(text, "10-Q")
    assert "Revenue grew 20%" in s["I-2"]
    assert "margins expanded" in s["I-2"]  # running page header didn't split the section
    assert "no material changes" in s["II-1A"]
    assert "I-1A" not in s


def test_red_flags_positive_and_negative():
    assert scan_red_flags("We identified a material weakness in our internal control over revenue.")
    assert not scan_red_flags("We did not identify any material weakness in internal control.")
    assert not scan_red_flags("A material weakness in our internal control could harm us.")
    assert not scan_red_flags("Item 9. Changes in and Disagreements with Accountants\nNone")


def test_find_mdna_by_title():
    text = "Cover\nManagement's discussion and analysis\n" + ("Net interest income rose. " * 200)
    assert find_mdna_by_title(text).startswith("Management")





def test_form4_parse_and_aggregate():
    txns = parse_form4(FORM4)
    assert [t.code for t in txns] == ["S", "F"]
    assert txns[0].role == "CFO" and txns[0].planned_10b5_1 and txns[0].value == 200000
    agg = summarize_insider_activity(txns)
    assert agg["open_market_sells"] == 1 and agg["sell_value"] == 200000  # tax withholding excluded
    assert agg["planned_10b5_1_sells"] == 1


def test_key_financials_quarter_yoy_and_ytd_filter():
    def row(start, end, val, filed="2026-08-26"):
        return {"start": start, "end": end, "val": val, "form": "10-Q", "filed": filed, "accn": "x"}

    facts = {
        "entityName": "Test",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            row("2025-04-28", "2025-07-27", 100),
                            row("2026-04-27", "2026-07-26", 150),
                            row("2026-01-26", "2026-07-26", 280),  # YTD, ignored for quarter
                        ]
                    }
                },
                # Only a Q1-length value exists for cash flow -> must be dropped from "latest quarter".
                "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [row("2026-01-26", "2026-04-26", 9)]}},
                "Assets": {"units": {"USD": [{"end": "2026-07-26", "val": 500, "filed": "2026-08-26"}]}},
            }
        },
    }
    kf = key_financials(facts)
    rev = kf["latest_quarter"]["revenue"]
    assert rev["value"] == 150 and rev["yoy_pct"] == 50.0
    assert "operating_cash_flow" not in kf["latest_quarter"]
    assert kf["balance_sheet"]["total_assets"]["value"] == 500


def test_key_financials_ifrs_in_reporting_currency():
    facts = {
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "TWD": [
                            {"start": "2024-01-01", "end": "2024-12-31", "val": 2894e9, "filed": "2025-04-17"},
                            {"start": "2023-01-01", "end": "2023-12-31", "val": 2161e9, "filed": "2024-04-18"},
                        ],
                        "USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 90e9, "filed": "2025-04-17"}],
                    }
                },
                "DilutedEarningsLossPerShare": {
                    "units": {"TWD/shares": [{"start": "2024-01-01", "end": "2024-12-31", "val": 45.25}]}
                },
            }
        }
    }
    fy = key_financials(facts)["latest_fiscal_year"]
    assert fy["revenue"]["unit"] == "TWD" and fy["revenue"]["yoy_pct"] == 33.9
    assert fy["eps_diluted"]["unit"] == "TWD/shares"
    assert format_amount(fy["revenue"]["value"], "TWD") == "TWD 2.89T"
    assert format_amount(96221000000, "USD") == "$96.22B"
    assert format_amount(2.46, "USD/shares") == "$2.46"


def test_stale_quarter_dropped_when_newer_annual_exists():
    def row(start, end, val):
        return {"start": start, "end": end, "val": val, "filed": "2026-06-01"}

    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        row("2020-10-01", "2020-12-31", 5), row("2025-04-01", "2026-03-31", 30)]}}}}}
    kf = key_financials(facts)
    assert kf["latest_quarter"] == {} and kf["latest_fiscal_year"]["revenue"]["value"] == 30
