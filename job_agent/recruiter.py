"""Recruiter/contact discovery from public, permitted sources only.

Only email addresses that literally appear on a fetched page are recorded (verification='verified_on_page').
Addresses typed in by the user are recorded as 'user_provided'. Nothing is ever guessed or pattern-generated.
"""
from __future__ import annotations

import logging
import re
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .sources.base import html_to_text

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IRRELEVANT_LOCAL = re.compile(
    r"^(no-?reply|donotreply|privacy|dpo|gdpr|legal|security|abuse|support|help|info|press|media|"
    r"accommodations?|accessibility|disability|ada|investor|ir|billing|sales|compliance|ethics|"
    r"accounts?|accounting|finance|payroll|invoices?|payments?|procurement|vendors?|purchase|marketing|"
    r"applicant-?privacy|data-?protection|.*accommodation.*|.*privacy.*)$", re.I)
RECRUITING_LOCAL = re.compile(r"(career|job|hiring|hire|talent|recruit|hr|people|ta\b|campus)", re.I)

# Shared inboxes that are fine for contacting a small startup when no recruiting address exists.
GENERAL_LOCAL = re.compile(
    r"^(info|hello|hi|hey|contact|contactus|contact-us|team|connect|office|admin|enquiry|enquiries|inquiry|"
    r"inquiries|mail|reach|reachus|talk|founders?|general|business|partner|partners)$", re.I)


def classify_startup(email: str) -> str | None:
    """Startup contacts: recruiting inboxes first, general inboxes allowed. Returns kind or None (unusable)."""
    local = email.split("@")[0]
    if GENERAL_LOCAL.match(local):
        return "general_inbox"
    kind = classify(email)
    if kind == "recruiting_inbox":
        return "careers_inbox"
    return kind          # "personal" | "other" | None


VERIFIED = "verified_on_page"
USER_PROVIDED = "user_provided"


@dataclass
class Contact:
    email: str
    kind: str            # "recruiting_inbox" | "personal" | "other"
    source_url: str
    verification: str
    context: str = ""


def classify(email: str) -> str | None:
    local = email.split("@")[0]
    if IRRELEVANT_LOCAL.match(local):
        return None
    if RECRUITING_LOCAL.search(local):
        return "recruiting_inbox"
    if re.match(r"^[a-z]+[._][a-z]+$", local, re.I):
        return "personal"
    return "other"


def emails_in_text(text: str, source_url: str) -> list[Contact]:
    out: dict[str, Contact] = {}
    for m in EMAIL_RE.finditer(text or ""):
        email = m.group(0).strip(".").lower()
        if email.endswith((".png", ".jpg", ".gif", ".svg")):
            continue
        kind = classify(email)
        if not kind or email in out:
            continue
        ctx = text[max(0, m.start() - 120): m.end() + 40].replace("\n", " ")
        out[email] = Contact(email, kind, source_url, VERIFIED, ctx)
    return list(out.values())


def robots_allows(url: str, user_agent: str) -> bool:
    p = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
    try:
        rp.read()
    except Exception:
        return False
    return rp.can_fetch(user_agent, url)


def find_contacts(job: dict, session: requests.Session, user_agent: str, fetch_page: bool = True) -> list[Contact]:
    """Search the job description, then (if robots.txt allows) the public job page itself."""
    contacts = emails_in_text(job.get("description", ""), job.get("url") or "job description (API)")
    url = job.get("url")
    if fetch_page and url and not contacts:
        try:
            if robots_allows(url, user_agent):
                r = session.get(url, timeout=20, headers={"Accept": "text/html"})
                if r.ok:
                    contacts = emails_in_text(html_to_text(r.text), url)
            else:
                log.info("robots.txt disallows fetching %s", url)
        except requests.RequestException as e:
            log.warning("Could not fetch job page %s: %s", url, e)
    order = {"personal": 0, "recruiting_inbox": 1, "other": 2}
    return sorted(contacts, key=lambda c: order[c.kind])


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email.strip()))
