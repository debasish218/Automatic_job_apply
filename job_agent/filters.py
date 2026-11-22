"""Cheap, deterministic pre-filters applied before spending Gemini calls."""
from __future__ import annotations

import re

INDIA_RE = re.compile(
    r"\b(india|hyderabad|secunderabad|bengaluru|bangalore|pune|noida|gurgaon|gurugram|chennai|mumbai|navi mumbai|"
    r"kolkata|delhi|new delhi|ncr|ahmedabad|thiruvananthapuram|trivandrum|kochi|coimbatore|jaipur|indore|"
    r"chandigarh|mohali|bhubaneswar|vadodara|mysore|mysuru|visakhapatnam)\b", re.I)

# Titles that indicate the role is in the AI/ML family we target.
RELEVANT_TITLE_RE = re.compile(
    r"machine learning|\bml\b|mlops|ml ops|\bai\b|a\.i\.|artificial intelligence|computer vision|\bcv\b|"
    r"deep learning|gen ?ai|generative|\bllm|\brag\b|agentic|nlp|vision|perception|applied scien|"
    r"data scien|inference|model (optimi|deploy)", re.I)

# Titles that clearly target seniority well beyond ~2 years.
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|head|director|manager|architect|vp|vice president|chief|"
    r"distinguished|fellow|expert|specialist ii|iii|iv|l5|l6|l7|e5|e6)\b", re.I)

MTS_RE = re.compile(r"member\s+(of\s+)?(the\s+)?technical\s+staff", re.I)

NON_ENGINEERING_RE = re.compile(
    r"\b(sales|account executive|marketing|recruit|talent|legal|counsel|finance|accountant|hr\b|"
    r"business process analyst|success manager|gtm|go-to-market|product manag\w*|program manag\w*|"
    r"tool admin|operations manager|support)\b", re.I)

# "3+ years", "3-5 years", "minimum of 4 years", "at least 5 yrs", "5 or more years"
_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:\+|plus)\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:-|–|—|to)\s*\d{1,2}\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?:minimum|min\.?|at least|atleast)\s*(?:of\s*)?(\d{1,2}(?:\.\d)?)\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:or more|and above)\s*(?:years?|yrs?)", re.I),
]
_EXP_CONTEXT = re.compile(r"experience|exp\b|industry|professional|working|hands-on", re.I)


def is_india(location: str) -> bool:
    return bool(INDIA_RE.search(location or ""))


def title_check(title: str) -> tuple[bool, str]:
    """Return (keep, reason)."""
    if NON_ENGINEERING_RE.search(title):
        return False, "non-engineering role"
    if not RELEVANT_TITLE_RE.search(title):
        return False, "title not in AI/ML target family"
    m = SENIOR_TITLE_RE.search(MTS_RE.sub("", title))
    if m:
        return False, f"senior-level title ('{m.group(0)}')"
    return True, "relevant title"


def extract_min_years(text: str) -> tuple[float | None, str | None]:
    """Best-effort extraction of the minimum years of experience required.

    Looks only at sentences that mention experience. Returns the smallest stated minimum
    (so '1-3 years' -> 1.0) and the matching snippet. Returns (None, None) if not stated.
    """
    if not text:
        return None, None
    best: tuple[float, str] | None = None
    for sentence in re.split(r"(?<=[.;\n•])\s+", text):
        if not _EXP_CONTEXT.search(sentence):
            continue
        for pat in _YEARS_PATTERNS:
            for m in pat.finditer(sentence):
                yrs = float(m.group(1))
                if yrs > 25:
                    continue
                snippet = sentence.strip()[:200]
                if best is None or yrs < best[0]:
                    best = (yrs, snippet)
    return (best[0], best[1]) if best else (None, None)
