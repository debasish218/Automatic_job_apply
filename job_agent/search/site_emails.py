"""Find contact emails on a company's own website (no Gemini needed).

Priority (used when choosing the recipient):
  1. recruiting addresses  — careers@ / jobs@ / hr@ / talent@ …, or a person's address on a careers/jobs page
  2. people at the company — personal addresses elsewhere on the site (e.g. a founder)
  3. general inboxes       — hello@ / info@ / contact@ / team@ … (fine for a small startup)

Only addresses that literally appear on the site (text, mailto links, Cloudflare-protected links) are kept,
and only on the company's own domain — except a free-mail address printed on a careers page.
robots.txt is respected by the fetcher.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..recruiter import EMAIL_RE, classify_startup
from .fetch import PageFetcher, host_of

log = logging.getLogger(__name__)

FREE_MAIL = ("gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "rediffmail.com", "live.com",
             "proton.me", "protonmail.com", "icloud.com")
CAREERS_PAGE_RE = re.compile(r"career|jobs?\b|join|hiring|work[-_ ]?with[-_ ]?us|openings|vacanc", re.I)
CONTACT_PAGE_RE = re.compile(r"contact|about|team|company|reach|connect|people", re.I)
HIRING_CONTEXT_RE = re.compile(r"resume|\bcv\b|apply|hiring|careers?|jobs?|opening|join (our|the) team", re.I)
COMMON_PATHS = ["/careers", "/jobs", "/contact", "/contact-us", "/about", "/about-us", "/team", "/join-us"]
KIND_RANK = {"careers_inbox": 0, "recruiter": 0, "founder": 1, "team_member": 2, "general_inbox": 3}
# Function/department inboxes: company inboxes, not a person and not recruiting -> treated as general inboxes.
FUNCTIONAL_LOCAL = re.compile(
    r"^(solutions?|services?|ops|operations|demo|products?|tech|dev|engineering|it|feedback|orders?|bookings?|"
    r"events?|community|growth|bd|partnerships?|enterprise|customers?|clients?|care|query|queries|ask|website|web|"
    r"social|design|studio|labs?|ai|official|corporate|management|office|team|desk)$", re.I)
FOUNDER_LOCAL = re.compile(r"^(ceo|cto|coo|founders?|co-?founders?)$", re.I)
FOUNDER_ROLE_RE = re.compile(r"founder|ceo|cto|coo|chief|director|owner|president", re.I)


@dataclass
class SiteEmail:
    email: str
    kind: str             # careers_inbox | recruiter | founder | team_member | general_inbox
    page_url: str
    context: str
    name: str | None = None
    role: str | None = None


def _person(text: str) -> tuple[str, str]:
    """'Harsha Vardhan (Founder & CEO)' -> ('Harsha Vardhan', 'Founder & CEO')"""
    m = re.match(r"\s*([^(,–-]+?)\s*(?:[(,–-]\s*(.*?)\)?\s*)?$", text or "")
    return ((m.group(1) or "").strip(), (m.group(2) or "").strip()) if m else ((text or "").strip(), "")


def match_person(local: str, people: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Is this address clearly a known person's (founder/team from the news or LinkedIn)? Name match only."""
    tokens = [t for t in re.split(r"[._+-]", local.lower()) if t]
    compact = re.sub(r"[^a-z]", "", local.lower())
    for name, role in people:
        parts = [re.sub(r"[^a-z]", "", w) for w in name.lower().split()]
        parts = [w for w in parts if len(w) >= 3]
        if not parts:
            continue
        first, last = parts[0], parts[-1]
        if (any(t in parts or (len(t) >= 4 and any(w.startswith(t) for w in parts)) for t in tokens)
                or compact in (first + last, first[0] + last, first + last[0])
                or (len(parts) == 1 and compact == first)):
            return name, role
    return None


def _domain_ok(email: str, domain: str) -> bool:
    ed = email.split("@")[1]
    return ed == domain or ed.endswith("." + domain) or domain.endswith("." + ed)


def _context(text: str, email: str, width: int = 140) -> str:
    i = text.lower().find(email)
    if i < 0:
        return ""
    return " ".join(text[max(0, i - width): i + len(email) + 60].split())


def site_mentions(text: str, company: str) -> bool:
    """Does the page mention the company? (guards against a wrong website from the news article)"""
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    name = re.sub(r"[^a-z0-9]", "", company.lower())
    first = re.sub(r"[^a-z0-9]", "", company.lower().split()[0]) if company.split() else ""
    return bool(name) and (name in compact or (len(first) >= 4 and first in compact))


def classify_site_email(email: str, domain: str, page_url: str, context: str,
                        people: list[tuple[str, str]] = ()) -> tuple[str, str | None, str | None] | None:
    """Returns (kind, person_name, role) or None if the address is unusable."""
    if not _domain_ok(email, domain):
        on_careers = CAREERS_PAGE_RE.search(urlparse(page_url).path or "")
        if not (email.split("@")[1] in FREE_MAIL and on_careers and HIRING_CONTEXT_RE.search(context)):
            return None          # another company's address (agency, website builder, investor…)
    kind = classify_startup(email)
    if kind is None:
        return None
    local = email.split("@")[0]
    if kind == "careers_inbox":
        return "careers_inbox", None, None
    if FOUNDER_LOCAL.match(local):
        return "founder", None, local.upper() if len(local) <= 3 else "Founders"
    person = match_person(local, list(people))
    if person:
        name, role = person
        return ("founder" if FOUNDER_ROLE_RE.search(role or "") else "team_member"), name, role or None
    if kind == "general_inbox" or FUNCTIONAL_LOCAL.match(local):
        return "general_inbox", None, None
    # an individual's address: a recruiter only if the text right around it is about hiring
    # (the page path alone is not enough — footers repeat the same address on every page)
    if HIRING_CONTEXT_RE.search(context or ""):
        return "recruiter", None, None
    return "team_member", None, None


def crawl_site_emails(fetcher: PageFetcher, domain: str, company: str, max_pages: int = 8,
                      people: list[str] | list[tuple[str, str]] = ()) -> tuple[list[SiteEmail], str]:
    """people: known founders / team, e.g. ['Harsha Vardhan (Founder & CEO)'] or [(name, role)]."""
    people = [p if isinstance(p, tuple) else _person(p) for p in people]
    """Returns (emails sorted by priority, status message)."""
    home = f"https://{domain}"
    page = fetcher.fetch(home)
    if not page.ok:
        page = fetcher.fetch(f"https://www.{domain}")
    if not page.ok:
        return [], f"website not readable ({page.status})"
    if not site_mentions(page.text, company):
        return [], f"{domain} does not mention {company} — website may be wrong, not crawled"
    site_host = host_of(page.final_url or home)
    queue: list[str] = []
    for url, label in page.links:            # careers / contact pages linked from the homepage first
        h = host_of(url)
        if (h == site_host or h.endswith("." + domain)) and \
                (CAREERS_PAGE_RE.search(url + " " + label) or CONTACT_PAGE_RE.search(url + " " + label)):
            queue.append(url.split("#")[0])
    queue += [f"https://{site_host}{p}" for p in COMMON_PATHS]
    queue.sort(key=lambda u: 0 if CAREERS_PAGE_RE.search(u) else 1)
    pages = [page]
    tried = {home.rstrip("/")}
    got = {(page.final_url or home).rstrip("/")}            # pages already read (after redirects)
    for url in queue:
        if len(pages) >= max_pages:
            break
        if url.rstrip("/") in tried | got:
            continue
        tried.add(url.rstrip("/"))
        p = fetcher.fetch(url)
        final = (p.final_url or url).rstrip("/")
        if p.ok and final not in got:                        # e.g. /careers redirecting to the homepage
            pages.append(p)
            got.add(final)
    found: dict[str, SiteEmail] = {}
    for p in pages:
        purl = p.final_url or p.url
        cands = [(m.group(0), _context(p.text, m.group(0).lower())) for m in EMAIL_RE.finditer(p.text)]
        cands += [(e, label or _context(p.text, e.lower())) for e, label in p.mailtos]
        for raw, ctx in cands:
            email = raw.strip().strip(".").lower()
            if not EMAIL_RE.fullmatch(email) or email.endswith((".png", ".jpg", ".svg", ".gif", ".webp")):
                continue
            res = classify_site_email(email, domain, purl, ctx, people)
            if not res:
                continue
            kind, name, role = res
            ctx = "" if "email protected" in (ctx or "").lower() else ctx
            prev = found.get(email)
            if prev is None or KIND_RANK[kind] < KIND_RANK[prev.kind] or (len(ctx) > len(prev.context)
                                                                         and kind == prev.kind):
                found[email] = SiteEmail(email, kind, purl, ctx or f"Listed on {purl}", name, role)
    out = sorted(found.values(), key=lambda e: KIND_RANK[e.kind])
    return out, f"{len(pages)} pages read on {site_host}, {len(out)} emails"
