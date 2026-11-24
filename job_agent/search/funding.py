"""Find recently funded (pre-seed / seed / Series A) AI startups from funding news, via SearXNG + Gemini.

Rules enforced in code (not only in the prompt):
- Only pre-seed, seed, pre-Series A and Series A rounds.
- Funding announced within the window (default 30 days); deals with no determinable date are rejected.
- Must be AI / ML related.
- Location: India-based (or with a team in India) is preferred. Japan / UAE companies are kept only when the
  source shows they hire from India. Everything else is excluded.
- The company name must literally appear in the article text it was extracted from.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field

from ..filters import is_india
from ..gemini_client import GeminiClient
from .fetch import PageFetcher, host_of, is_never_fetch
from .searx import SearchResult

log = logging.getLogger(__name__)

# Funding-news queries (run through SearXNG with time_range=month). Indian sources first.
FUNDING_QUERIES = [
    "AI startup raises seed funding India",
    "AI startup raises Series A funding India",
    "Indian AI startup pre-seed round",
    "generative AI startup funding round Bengaluru",
    "site:inc42.com AI startup raises funding",
    "site:entrackr.com AI startup raises",
    "site:yourstory.com AI startup raises seed round",
    "site:vccircle.com AI startup funding",
    "site:economictimes.indiatimes.com AI startup raises seed",
    "site:moneycontrol.com AI startup raises Series A",
    "Indian startups funding this week AI",
    "machine learning startup raises seed India",
    "computer vision startup raises funding India",
    "LLM agentic AI startup raises seed India",
    "deep tech AI startup Series A India investors",
    "UAE AI startup raises seed India team",
    "Japan AI startup raises funding India engineering team",
]

ALLOWED_ROUNDS = {"pre-seed", "seed", "pre-series a", "series a"}
JAPAN_RE = re.compile(r"\b(japan|tokyo|osaka|kyoto|fukuoka|yokohama|nagoya)\b", re.I)
UAE_RE = re.compile(r"\b(uae|u\.a\.e|united arab emirates|dubai|abu dhabi|sharjah|ajman)\b", re.I)
# Startups not headquartered in India get this many points less (the user prefers Indian companies).
NON_INDIAN_PENALTY = 10


class Deal(BaseModel):
    company: str = Field(description="Startup name exactly as written")
    website: str = Field(description="Company website/domain if written in the text, else empty")
    description: str = Field(description="What the company builds, 1-2 sentences, only from the text")
    sector: str = Field(description="Sector / domain, e.g. 'Generative AI for legal', 'Computer vision for retail'")
    is_ai_related: bool = Field(description="True if the product is AI/ML-based (AI, ML, LLMs, computer vision, NLP, "
                                            "data science, MLOps, robotics/autonomy, AI infrastructure)")
    funding_round: str = Field(description="pre-seed | seed | pre-series a | series a | series b | other | unknown")
    amount: str = Field(description="Amount as written (e.g. '$4 million', 'Rs 25 crore'), or empty")
    funding_date: str = Field(description="Date the funding was announced as YYYY-MM-DD if stated/derivable, else empty")
    investors: list[str] = Field(description="Investors named in the text")
    hq_city: str = Field(description="Headquarters city as written, else empty")
    hq_country: str = Field(description="Headquarters country as written or clearly implied by the city, else empty")
    founded_year: str = Field(description="Year founded if written, else empty")
    founders: list[str] = Field(description="Founder names (with role if written)")
    india_presence: str = Field(description="If the text says the company has an office/team/operations in India, "
                                            "a short verbatim quote; else empty")
    hires_from_india_evidence: str = Field(description="Verbatim quote showing it hires people from India / builds its "
                                                       "team in India / hires remotely in India; else empty")
    use_of_funds: str = Field(description="What the money will be used for (e.g. hiring, R&D), as written, or empty")
    evidence_quote: str = Field(description="Short verbatim sentence from the text announcing this funding")


class DealsOut(BaseModel):
    article_date: str = Field(description="Publication date of the article as YYYY-MM-DD if visible, else empty")
    deals: list[Deal]


EXTRACT_PROMPT = """Extract startup funding announcements from this news article / search result.
Use ONLY the text below. Never guess or add outside knowledge: leave a field empty when it is not written.
Include every startup whose funding round is announced in the text (funding roundup articles list several).
Ignore acquisitions, IPOs, debt-only rounds, and companies merely mentioned.

URL: {url}
SEARCH RESULT TITLE: {title}
SEARCH SNIPPET: {snippet}
SEARCH ENGINE DATE: {published}
ARTICLE TEXT (may be empty):
{page}
"""


@dataclass
class DealCandidate:
    deal: Deal
    source_url: str
    source_title: str
    source_text: str
    published: str | None
    snippet: str = ""
    funding_date: date | None = None
    region: str | None = None
    reason: str | None = None          # why it was rejected (None = kept)
    extra: dict = field(default_factory=dict)


def norm_round(r: str) -> str:
    r = re.sub(r"[\s_]+", " ", (r or "").lower().replace("–", "-")).strip()
    r = r.replace("preseed", "pre-seed").replace("pre seed", "pre-seed").replace("pre-series", "pre-series")
    r = re.sub(r"^pre series", "pre-series", r)
    if r in ("series-a", "series a round"):
        r = "series a"
    if r in ("seed round", "seed funding"):
        r = "seed"
    return r


def parse_date(*vals: str | None) -> date | None:
    for v in vals:
        if not v:
            continue
        v = v.strip()
        for cand in (v[:10], v):
            try:
                return datetime.fromisoformat(cand.replace("Z", "+00:00")).date()
            except ValueError:
                continue
    return None


def region_of(deal: Deal) -> str:
    """india | india_team | japan | uae | other"""
    hq = f"{deal.hq_city} {deal.hq_country}"
    if is_india(hq):
        return "india"
    if JAPAN_RE.search(hq):
        return "japan"
    if UAE_RE.search(hq):
        return "uae"
    if deal.india_presence and is_india(deal.india_presence):
        return "india_team"
    if not hq.strip() and is_india(f"{deal.description} {deal.evidence_quote}"):
        return "india"
    return "other"


def location_policy(deal: Deal) -> tuple[str, str | None]:
    """Returns (region, rejection_reason or None) according to the user's location preferences."""
    region = region_of(deal)
    where = ", ".join(x for x in (deal.hq_city, deal.hq_country) if x) or "location not stated"
    if region in ("india", "india_team"):
        return region, None
    if region in ("japan", "uae"):
        ev = deal.hires_from_india_evidence or (deal.india_presence if is_india(deal.india_presence) else "")
        if ev.strip():
            return region, None
        return region, f"based in {where}; no evidence they hire from India"
    return region, f"based in {where} (only Indian companies, or Japan/UAE companies hiring from India, are kept)"


def check_deal(c: DealCandidate, window_days: int, today: date | None = None) -> str | None:
    """Apply the hard rules. Returns a rejection reason, or None if the deal is kept."""
    d = c.deal
    today = today or date.today()
    if not d.company.strip():
        return "no company name"
    if d.company.strip().lower() not in c.source_text.lower():
        return "company name not found in the source text"
    rnd = norm_round(d.funding_round)
    if rnd not in ALLOWED_ROUNDS:
        return f"round is '{d.funding_round or 'unknown'}' (only pre-seed, seed, Series A)"
    if not d.is_ai_related:
        return f"not AI/ML related ({d.sector or 'sector unknown'})"
    fd = c.funding_date
    if fd is None:
        return "funding date unknown"
    if fd > today + timedelta(days=2):
        return f"funding date {fd} is in the future"
    if (today - fd).days > window_days:
        return f"funded {fd} (older than {window_days} days)"
    c.region, why = location_policy(d)
    return why


def extract_deals(gemini: GeminiClient, fetcher: PageFetcher, r: SearchResult) -> tuple[list[DealCandidate], str]:
    """Read one news result and return candidate deals (unfiltered) and the page status."""
    page_text, status = "", "not_fetched"
    if not is_never_fetch(r.url):
        page = fetcher.fetch(r.url)
        status = page.status
        if page.ok:
            page_text = page.text[:14000]
    source_text = f"{r.title}\n{r.snippet}\n{page_text}"
    out = gemini.generate_json(
        EXTRACT_PROMPT.format(url=r.url, title=r.title, snippet=r.snippet, published=r.published or "unknown",
                              page=page_text or "(not available — use the title and snippet only)"),
        DealsOut, temperature=0, task="light")
    cands = []
    for d in out.deals[:15]:
        fd = parse_date(d.funding_date, out.article_date, r.published)
        cands.append(DealCandidate(d, r.url, r.title, source_text, r.published, r.snippet, fd))
    return cands, status


def domain_from_website(website: str) -> str | None:
    w = (website or "").strip()
    if not w or " " in w or "." not in w:
        return None
    return host_of(w if "://" in w else f"https://{w}") or None


def verified_evidence(c: DealCandidate) -> str:
    """The evidence quote if it literally appears in the source, else the search snippet."""
    q = re.sub(r"\s+", " ", c.deal.evidence_quote or "").strip()
    text = re.sub(r"\s+", " ", c.source_text)
    if q and q.lower() in text.lower():
        return q[:400]
    return f"{c.source_title} — {c.snippet}"[:400]
