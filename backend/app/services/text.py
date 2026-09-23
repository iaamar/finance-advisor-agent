"""Filing HTML -> clean text -> sections keyed by Item number, plus red-flag scan."""

from __future__ import annotations

import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Inline-XBRL filings start with an <?xml?> prolog but are really XHTML.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_PART_RE = re.compile(r"^\s*part\s+(iv|iii|ii|i)\b", re.I)
# Item numbers: "7", "1A" (10-K), "16E" (20-F); "3.D" keys as "3".
_ITEM_RE = re.compile(r"^\s*item\s+(\d{1,2}[a-j]?)\b\s*[\.:\-–—]?\s*(.*)$", re.I)
_ITEM8K_RE = re.compile(r"^\s*item\s+(\d\.\d{2})\b", re.I)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    # Inline XBRL puts a hidden header block with all tagged facts; drop it.
    for tag in soup.find_all(re.compile(r"^ix:header$", re.I)):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        tag.decompose()
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def split_sections(text: str, form: str) -> dict[str, str]:
    """Split a 10-K/10-Q into {item_key: text}.

    10-Q keys include the part ("I-2", "II-1A") because item numbers repeat
    across Part I and Part II. When a heading occurs more than once (table of
    contents + body) the longest body wins, which discards the TOC.
    """
    is_q = form.upper() == "10-Q"
    part = "I"
    sections: dict[str, str] = {}
    cur_key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur_key is not None:
            body = "\n".join(buf).strip()
            if len(body) > len(sections.get(cur_key, "")):
                sections[cur_key] = body

    for ln in text.splitlines():
        pm = _PART_RE.match(ln)
        if pm and len(ln) < 120:
            part = pm.group(1).upper()
        im = _ITEM_RE.match(ln)
        if im and len(ln) < 200:
            num = im.group(1).upper()
            key = f"{part}-{num}" if is_q else num
            if key == cur_key:
                continue  # running page header ("PART I / Item 2") - same section continues
            flush()
            cur_key = key
            buf = [ln]
            continue
        if cur_key is not None:
            buf.append(ln)
    flush()
    return sections


def find_section_by_title(text: str, title_re: str, min_len: int = 3000) -> str:
    """Fallback when a section isn't under a standard Item heading (banks,
    40-F exhibits): the longest run of text starting at a heading-like line
    matching `title_re` and ending at the next such line."""
    pat = re.compile(rf"^\s*(?:item\s+[\d.a-z]+\s*[\.:\-–—]?\s*)?(?:{title_re})", re.I)
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if len(ln) < 150 and pat.match(ln)]
    best = ""
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end])
        if len(body) > len(best):
            best = body
    return best if len(best) >= min_len else ""


MDNA_TITLE = r"management.s discussion and analysis|operating and financial review"


def find_mdna_by_title(text: str, min_len: int = 3000) -> str:
    return find_section_by_title(text, MDNA_TITLE, min_len)


def split_8k_items(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for ln in text.splitlines():
        m = _ITEM8K_RE.match(ln)
        if m and len(ln) < 200:
            if cur:
                sections[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), [ln]
        elif cur:
            buf.append(ln)
    if cur:
        sections[cur] = "\n".join(buf).strip()
    return sections


def is_boilerplate_no_change(section: str) -> bool:
    body = section[:1500].lower()
    return len(section) < 2500 and bool(re.search(r"no material changes?|have not materially changed", body))


# Red flags are detected in code, not by the LLM, so they can't be missed or invented.
RED_FLAG_PATTERNS = {
    "material weakness": r"material weakness(?:es)? in (?:our )?internal control",
    "controls not effective": r"(?:disclosure controls|internal control)[^.]{0,200}\bwere not effective",
    "restatement": r"\brestate(?:d|ment)\b[^.]{0,120}(?:financial statements|previously issued)",
    "going concern": r"substantial doubt[^.]{0,80}going concern",
    "auditor disagreement": r"\bdisagreements? with (?:the )?(?:former )?(?:independent )?(?:registered )?(?:public )?account",
}

RED_FLAG_8K_ITEMS = {
    "3.01": "Notice of delisting or failure to meet listing standards",
    "4.01": "Change in the company's certifying accountant",
    "4.02": "Non-reliance on previously issued financial statements",
}


_HEDGE_RE = re.compile(r"\b(may|might|could|if|would|any)\b", re.I)
_NEGATION_RE = re.compile(r"\b(no|not|none|did not identify|without|free of)\b", re.I)


def scan_red_flags(text: str) -> list[str]:
    """Scan *disclosure* sections (controls, auditor changes) for red flags.

    Callers should pass only those sections: risk factors are full of
    hypothetical language ("a material weakness could...") that isn't a flag.
    Sentences that are hedged or negated are ignored.
    """
    flags = []
    for label, pat in RED_FLAG_PATTERNS.items():
        for m in re.finditer(pat, text, re.I):
            start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            end_dot = text.find(".", m.end())
            sentence = re.sub(r"\s+", " ", text[start : end_dot if end_dot != -1 else m.end() + 200]).strip()
            if (
                _HEDGE_RE.search(sentence)
                or _NEGATION_RE.search(text[start : m.start()])
                or re.search(r"\b(none|not applicable)\b", sentence, re.I)  # e.g. Item 9 heading + "None"
            ):
                continue
            flags.append(f"{label}: \u201c{sentence[:240]}\u201d")
            break
    return flags
