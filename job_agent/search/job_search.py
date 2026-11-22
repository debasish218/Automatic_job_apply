"""Discover individual job postings on job sites through web search."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field

from ..gemini_client import GeminiClient
from ..sources.base import JobPosting, html_to_text
from .fetch import Page, PageFetcher
from .searx import SearchResult

log = logging.getLogger(__name__)

# Individual-posting URL patterns (listing/search pages are ignored).
POSTING_PATTERNS: dict[str, re.Pattern] = {
    "naukri": re.compile(r"naukri\.com/job-listings-[\w-]+", re.I),
    "linkedin": re.compile(r"linkedin\.com/jobs/view/[\w%-]+", re.I),
    "indeed": re.compile(r"indeed\.com/(viewjob\?|rc/clk\?|m/viewjob\?|cmp/[^/]+/jobs/)", re.I),
    "foundit": re.compile(r"foundit\.in/job/[\w-]+", re.I),
    "hirist": re.compile(r"hirist\.(tech|com)/j/[\w-]+", re.I),
    "iimjobs": re.compile(r"iimjobs\.com/j/[\w-]+", re.I),
    "cutshort": re.compile(r"cutshort\.io/job/[\w-]+", re.I),
    "wellfound": re.compile(r"wellfound\.com/jobs/\d+", re.I),
    "instahyre": re.compile(r"instahyre\.com/job-\d+", re.I),
    "shine": re.compile(r"shine\.com/jobs/[\w-]+/[\w-]+/\d+", re.I),
    "greenhouse": re.compile(r"greenhouse\.io/[\w-]+/jobs/\d+", re.I),
    "lever": re.compile(r"jobs\.lever\.co/[\w.-]+/[0-9a-f-]{36}", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/[\w.%-]+/[0-9a-f-]{36}", re.I),
    "workday": re.compile(r"myworkdayjobs\.com/.+/job/", re.I),
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/[\w-]+/\d+", re.I),
}

SITE_FILTERS = {
    "naukri": "site:naukri.com",
    "linkedin": "site:linkedin.com/jobs/view",
    "foundit": "site:foundit.in/job",
    "hirist": "site:hirist.tech",
    "indeed": "site:in.indeed.com",
    "cutshort": "site:cutshort.io/job",
    "wellfound": "site:wellfound.com/jobs",
    "instahyre": "site:instahyre.com",
    "iimjobs": "site:iimjobs.com",
    "ats": "(site:jobs.lever.co OR site:boards.greenhouse.io OR site:job-boards.greenhouse.io OR "
           "site:jobs.ashbyhq.com OR site:myworkdayjobs.com)",
    "web": "",
}
EXPERIENCE_PHRASES = ["0-2 years", "1-3 years", "1-2 years", "2 years experience", "junior"]
SEARCH_CITIES = ["Hyderabad", "Bangalore", "Pune", "Chennai", "Noida", "Gurgaon", "Mumbai", "Kolkata", "India"]


def site_of(url: str) -> str | None:
    for name, pat in POSTING_PATTERNS.items():
        if pat.search(url):
            return name
    return None


def canonical_url(url: str) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    keep = {k: v for k, v in q.items() if k in ("jk", "vjk", "gh_jid", "jobId", "id")}
    host = p.netloc.lower().replace("in.linkedin.com", "www.linkedin.com").replace("m.", "", 1) \
        if p.netloc.lower().startswith("m.") else p.netloc.lower().replace("in.linkedin.com", "www.linkedin.com")
    return urlunparse((p.scheme, host, p.path.rstrip("/"), "", urlencode(keep, doseq=True), ""))


def build_queries(roles: list[str]) -> list[str]:
    """Deterministic, de-duplicated query set: role x experience phrase x site, plus city-specific web queries."""
    qs: list[str] = []
    for i, role in enumerate(roles):
        for j, (site, flt) in enumerate(SITE_FILTERS.items()):
            exp = EXPERIENCE_PHRASES[(i + j) % len(EXPERIENCE_PHRASES)]
            loc = "India" if site in ("linkedin", "ats", "wellfound") else SEARCH_CITIES[(i + j) % len(SEARCH_CITIES)]
            qs.append(f'"{role}" {exp} {loc} {flt}'.strip())
        for city in SEARCH_CITIES[:-1]:
            qs.append(f'"{role}" job {city} 0-2 years experience apply')
    return list(dict.fromkeys(qs))


# ---------------------------------------------------------------- structured data
def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _ld_location(ld: dict) -> str:
    locs = ld.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    parts = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            country = country.get("name") if isinstance(country, dict) else country
            parts.append(", ".join(str(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"), country) if x))
    if ld.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    return "; ".join(p for p in parts if p)


def _ld_experience(ld: dict) -> tuple[str | None, float | None]:
    e = ld.get("experienceRequirements")
    if isinstance(e, dict) and e.get("monthsOfExperience") is not None:
        months = float(e["monthsOfExperience"])
        return f"{months / 12:g} years (structured data)", months / 12
    if isinstance(e, str):
        return e, None
    return None, None


@dataclass
class Candidate:
    posting: JobPosting
    site: str
    snippet: str
    page_status: str
    structured: bool
    company_url: str | None = None
    experience_text: str | None = None
    min_years: float | None = None
    valid_through: str | None = None
    extra: dict = field(default_factory=dict)


def from_json_ld(ld: dict, url: str, site: str, snippet: str) -> Candidate:
    org = _first(ld.get("hiringOrganization")) or {}
    org = org if isinstance(org, dict) else {"name": str(org)}
    exp_text, min_years = _ld_experience(ld)
    desc = html_to_text(html.unescape(ld.get("description") or ""))
    p = JobPosting(source=f"search:{site}", external_id=canonical_url(url), company=(org.get("name") or "").strip(),
                   title=html.unescape(ld.get("title") or "").strip(), location=_ld_location(ld), url=url,
                   description=desc, posted_at=ld.get("datePosted"))
    return Candidate(p, site, snippet, "fetched", True, org.get("sameAs") or org.get("url"), exp_text, min_years,
                     ld.get("validThrough"))


class ExtractedJob(BaseModel):
    is_single_job_posting: bool = Field(description="True only if this is ONE specific job opening (not a list/search page)")
    company: str = Field(description="Hiring company name exactly as stated; empty if unknown")
    title: str
    location: str
    experience_text: str = Field(description="Experience requirement as stated, or 'Not stated'")
    min_years: float | None = Field(description="Minimum years required, null if not stated")
    posted: str = Field(description="Posting date/age as stated (e.g. '3 days ago'), or empty")
    is_india: bool
    is_closed: bool = Field(description="True if the text says the job is closed/expired/no longer accepting")


EXTRACT_PROMPT = """Extract job details. Use ONLY the text below; never guess a company or details not stated.
If the hiring company is hidden (e.g. 'Top MNC', 'Confidential'), return company as stated.

URL: {url}
SEARCH RESULT TITLE: {title}
SEARCH SNIPPET: {snippet}
PAGE TEXT (may be empty):
{page}
"""


def extract_with_gemini(gemini: GeminiClient, r: SearchResult, page: Page, site: str) -> Candidate | None:
    page_text = page.text[:8000] if page.ok else ""
    x = gemini.generate_json(EXTRACT_PROMPT.format(url=r.url, title=r.title, snippet=r.snippet, page=page_text),
                             ExtractedJob, temperature=0, task="light")
    if not x.is_single_job_posting or not x.title:
        return None
    desc = page_text or f"{r.title}\n{r.snippet}"
    p = JobPosting(source=f"search:{site}", external_id=canonical_url(r.url), company=x.company.strip(),
                   title=x.title.strip(), location=x.location.strip(), url=r.url, description=desc,
                   posted_at=x.posted or None)
    c = Candidate(p, site, r.snippet, page.status, False, None,
                  None if x.experience_text == "Not stated" else x.experience_text, x.min_years)
    c.extra = {"closed": x.is_closed, "is_india": x.is_india}
    return c


def is_stale(posted: str | None, valid_through: str | None, max_age_days: int) -> str | None:
    now = datetime.now(timezone.utc)
    for label, val in (("posted", posted), ("valid through", valid_through)):
        if not val:
            continue
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if label == "posted" and now - dt > timedelta(days=max_age_days):
            return f"posted {dt.date()} (older than {max_age_days} days)"
        if label == "valid through" and dt < now:
            return f"expired on {dt.date()}"
    return None


def build_candidate(gemini: GeminiClient, fetcher: PageFetcher, r: SearchResult, site: str) -> Candidate | None:
    page = fetcher.fetch(r.url)
    if page.ok and page.job_postings:
        return from_json_ld(page.job_postings[0], r.url, site, r.snippet)
    return extract_with_gemini(gemini, r, page, site)
