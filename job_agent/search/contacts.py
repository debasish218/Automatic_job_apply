"""Recruiter / HR contact discovery via web search + Gemini, with literal-evidence validation.

Rules enforced in code (not just in the prompt):
- An email is kept only if it literally appears in the text of the source it is attributed to.
- Data-broker sites (which publish inferred/guessed addresses) are excluded entirely.
- Masked or pattern addresses (e.g. j***@x.com, {first}.{last}@x.com) are rejected.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..gemini_client import GeminiClient
from ..recruiter import EMAIL_RE, classify
from .fetch import PageFetcher, host_of, is_data_broker, is_never_fetch
from .searx import SearxClient, SearxError

log = logging.getLogger(__name__)

FREE_MAIL = ("gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "rediffmail.com", "live.com")


@dataclass
class Source:
    sid: int
    url: str
    title: str
    snippet: str
    page_text: str = ""             # only emails' surrounding context is kept to bound prompt size
    fetched: bool = False

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.snippet}\n{self.page_text}"


@dataclass
class FoundContact:
    name: str | None
    designation: str | None
    email: str | None
    linkedin: str | None
    source_url: str
    evidence: str
    verification: str              # verified_on_page | search_snippet | linkedin_search_result
    kind: str                      # recruiter | hr_inbox | careers_inbox | job_post_contact | other
    domain_match: bool | None


class ContactOut(BaseModel):
    name: str = Field(description="Person name, or empty for a shared inbox")
    designation: str = Field(description="Role/title as stated, or empty")
    email: str = Field(description="Email exactly as written in the source, or empty")
    linkedin_url: str = Field(description="LinkedIn profile URL exactly as given in the sources, or empty")
    source_id: int
    evidence_quote: str = Field(description="Short verbatim quote from that source supporting this contact")
    kind: str = Field(description="recruiter | hr_inbox | careers_inbox | job_post_contact | other")
    works_at_company: bool = Field(description="True only if the source clearly ties this contact to the company")
    relevant_to_role: bool = Field(default=True, description="False if the source shows this person/inbox hires for a "
                                   "clearly different function (finance, accounting, sales, HR ops, non-tech "
                                   "trainees, a different country only). True for tech/engineering/AI hiring or "
                                   "general recruiting/careers inboxes")


class ContactsOut(BaseModel):
    company_domain: str = Field(description="Company's official web domain if evident from sources, else empty")
    contacts: list[ContactOut]


PROMPT = """Identify recruiting contacts for a job application. Company: "{company}". Job: "{title}" ({location}).
Known company website (may be empty): {domain_hint}

From the SOURCES below, list people or inboxes useful for applying: recruiters, talent acquisition,
HR/hiring inboxes, careers inboxes, and contacts named in the job post.
STRICT RULES:
- Use only what is written in the sources. Never construct or guess an email address.
- Copy emails exactly as written. Ignore masked addresses (with * or ...), privacy/legal/support/sales emails,
  and emails of other companies.
- A person without an email is still useful if the source shows their name + recruiting role at this company
  (e.g. a LinkedIn result titled "Name - Talent Acquisition - {company}"). Give their linkedin_url if present.
- works_at_company must be false unless the source clearly connects the contact to {company}.
- relevant_to_role must be false when the source shows the contact recruiting for an unrelated function
  (e.g. "hiring CA trainees for Accounting & Finance") — this job is a technical AI/ML role.
- Max 8 contacts, most relevant first (India-based recruiters first).

SOURCES:
{sources}
"""


def _email_contexts(text: str, width: int = 220, limit: int = 6) -> str:
    chunks = []
    for m in list(EMAIL_RE.finditer(text))[:limit]:
        chunks.append(text[max(0, m.start() - width): m.end() + 60].replace("\n", " "))
    return "\n...\n".join(chunks)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def company_domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    h = host_of(url if "://" in url else f"https://{url}")
    return h or None


class ContactFinder:
    def __init__(self, searx: SearxClient, fetcher: PageFetcher, gemini: GeminiClient, max_pages: int = 4):
        self.searx = searx
        self.fetcher = fetcher
        self.gemini = gemini
        self.max_pages = max_pages

    def _queries(self, company: str, domain: str | None, deep: bool = False) -> list[str]:
        q = [f"{company} HR recruiter email India careers",
             f"{company} talent acquisition India LinkedIn"]
        if deep:   # where recruiter emails actually get published: hiring posts and "send your CV" lines
            q += [f'"{company}" hiring "share your resume" OR "send your CV" OR "drop your CV" email',
                  f"{company} recruiter hiring India email site:linkedin.com/posts",
                  f"{company} careers email id HR recruitment contact" + (f" {domain}" if domain else ""),
                  f'"{company}" "@{domain}" hiring resume' if domain else f'"{company}" HR email id for jobs India']
        return q

    @staticmethod
    def _direct_pages(domain: str | None) -> list[str]:
        if not domain:
            return []
        base = f"https://{domain}"
        return [f"{base}/careers", f"{base}/contact", f"{base}/contact-us", f"{base}/careers/"]

    def find(self, company: str, title: str, location: str, job_url: str, job_text: str,
             domain_hint: str | None, deep: bool = False) -> tuple[list[FoundContact], str | None, list[str]]:
        """Returns (contacts, company_domain, queries_used). deep=True adds email-focused searches."""
        sources: list[Source] = []
        # Source 0: the job posting itself (emails printed in the JD are the most relevant)
        if job_text and EMAIL_RE.search(job_text):
            sources.append(Source(0, job_url, f"Job posting: {title}", "", _email_contexts(job_text), True))
        seen = {job_url}
        queries = self._queries(company, domain_hint, deep)
        per_query = 12 if deep else 8
        for q in queries:
            try:
                results = self.searx.search(q)
            except SearxError as e:
                log.warning("%s", e)
                continue
            # results whose snippet already shows an email are always kept
            results.sort(key=lambda r: not EMAIL_RE.search(r.snippet or ""))
            for i, r in enumerate(results):
                if i >= per_query and not EMAIL_RE.search(r.snippet or ""):
                    break
                if r.url in seen or is_data_broker(r.url):
                    continue
                seen.add(r.url)
                sources.append(Source(len(sources), r.url, r.title, r.snippet))
        # The company's own careers/contact pages (no search query needed; robots.txt respected)
        for u in self._direct_pages(domain_hint):
            if u in seen:
                continue
            seen.add(u)
            page = self.fetcher.fetch(u)
            if page.ok and EMAIL_RE.search(page.text):
                sources.append(Source(len(sources), u, f"{company} official page", "", _email_contexts(page.text), True))
        # Fetch promising, permitted pages from search results to look for published emails
        fetched, limit = 0, (self.max_pages + 3 if deep else self.max_pages)
        cname = _norm(company).split(" ")[0] if company else ""

        def promise(s: Source) -> int:
            blob = _norm(s.url + " " + s.title + " " + s.snippet)
            score = 0
            score += 3 if re.search(r"e-?mail|@|send (your )?(cv|resume)|share (your )?(cv|resume)", blob) else 0
            score += 2 if (cname and cname in blob) or (domain_hint and domain_hint in s.url) else 0
            score += 1 if re.search(r"career|job|contact|hr|recruit|hiring", blob) else 0
            return score

        for s in sorted(sources, key=promise, reverse=True):
            if fetched >= limit:
                break
            if s.fetched or is_never_fetch(s.url) or promise(s) < 3:
                continue
            page = self.fetcher.fetch(s.url)
            fetched += 1
            if page.ok:
                s.page_text = _email_contexts(page.text)
                s.fetched = True
        if not sources:
            return [], domain_hint, queries

        listing = "\n\n".join(f"[{s.sid}] URL: {s.url}\nTITLE: {s.title}\nSNIPPET: {s.snippet}"
                              + (f"\nPAGE EXCERPTS WITH EMAILS: {s.page_text}" if s.page_text else "")
                              for s in sources)[:30000]
        out = self.gemini.generate_json(PROMPT.format(company=company, title=title, location=location,
                                                      domain_hint=domain_hint or "unknown", sources=listing),
                                        ContactsOut, temperature=0, task="light")
        domain = (out.company_domain or domain_hint or "").lower().removeprefix("www.") or None
        return self._validate(out, sources, domain), domain, queries

    def _validate(self, out: ContactsOut, sources: list[Source], domain: str | None) -> list[FoundContact]:
        by_id = {s.sid: s for s in sources}
        result: list[FoundContact] = []
        seen_keys = set()
        for c in out.contacts:
            src = by_id.get(c.source_id)
            if not src or not c.works_at_company:
                continue
            text = src.text
            email = (c.email or "").strip().strip(".").lower() or None
            if email:
                if "*" in email or "{" in email or not EMAIL_RE.fullmatch(email):
                    email = None
                elif email not in text.lower():
                    log.info("Dropped email %s: not literally present in source %s", email, src.url)
                    email = None
                elif classify(email) is None:
                    email = None
            linkedin = (c.linkedin_url or "").strip() or None
            if linkedin and not any(linkedin.rstrip("/") == s.url.rstrip("/") for s in sources):
                linkedin = src.url if "linkedin.com/in/" in src.url else None
            if not email and not linkedin:
                continue
            if not c.name and not email:
                continue
            key = email or linkedin
            if key in seen_keys:
                continue
            seen_keys.add(key)
            evidence = c.evidence_quote.strip()
            if _norm(evidence) not in _norm(text):
                evidence = (src.snippet or src.title)[:300]
            if email:
                verification = "verified_on_page" if (src.fetched and email in src.page_text.lower()) \
                    else "search_snippet"
            else:
                verification = "linkedin_search_result" if "linkedin.com" in src.url else "search_snippet"
            dm = None
            if email:
                ed = email.split("@")[1]
                dm = bool(domain and (ed == domain or ed.endswith("." + domain) or domain.endswith(ed)))
            kind = c.kind or "other"
            if not c.relevant_to_role:
                kind = "other_function"
            if src.sid == 0 and email and src.url == sources[0].url and sources[0].title.startswith("Job posting"):
                kind = "job_post_contact"
            elif kind == "job_post_contact":      # only the posting itself can be a job-post contact
                kind = "careers_inbox" if email and classify(email) == "recruiting_inbox" else "recruiter"
            result.append(FoundContact(c.name or None, c.designation or None, email, linkedin, src.url, evidence,
                                       verification, kind, dm))
        rank = {"job_post_contact": 0, "recruiter": 1, "hr_inbox": 2, "careers_inbox": 3}
        result.sort(key=lambda f: (f.email is None, not f.domain_match if f.email else True,
                                   rank.get(f.kind, 4)))
        return result
