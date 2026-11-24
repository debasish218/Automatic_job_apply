"""Funded-startup outreach: discover recently funded AI startups, score them, find contacts, draft and send emails.

Kept separate from job postings (own tables and dashboard tab). Shares the Gemini pool, SearXNG, Gmail, the
daily send limit and the duplicate protections with the job pipeline.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbm
from .gemini_client import GeminiBusy
from .pipeline import Pipeline, QuotaExhausted, SendResult
from .recruiter import USER_PROVIDED, is_valid_email
from .search.funding import (FUNDING_QUERIES, DealCandidate, check_deal, domain_from_website, extract_deals,
                             norm_round, verified_evidence)
from .search.fetch import host_of
from .search.searx import SearxError
from .search.site_emails import crawl_site_emails
from .startup_fit import StartupFit, score_startup, write_outreach

log = logging.getLogger(__name__)

FUNDING_WORDS = re.compile(r"rais|fund|seed|series a|pre-seed|secur|bags|invest|round|backed|nets|lands", re.I)


# Hosts that are never a startup's own website (news, social, databases, job boards).
NOT_COMPANY_SITE = re.compile(
    r"linkedin|facebook|twitter|x\.com|instagram|youtube|crunchbase|tracxn|wikipedia|inc42|yourstory|entrackr|"
    r"economictimes|indiatimes|techcrunch|medium\.com|glassdoor|ambitionbox|naukri|zaubacorp|tofler|pitchbook|"
    r"cbinsights|google\.|vccircle|moneycontrol|business-standard|livemint|thehindu|prnewswire|github\.com|"
    r"wellfound|angel\.co|ycombinator|f6s|apollo|rocketreach|zoominfo|owler|startupindia|dealroom", re.I)

RECRUITER_KINDS = ("user_provided", "recruiter", "careers_inbox", "hr_inbox", "job_post_contact")
SPECIFIC_KINDS = RECRUITER_KINDS + ("founder",)
# The user's priority for startups: recruiter mail first, then (small company) a founder, then a general inbox.
STARTUP_RECIPIENT_ORDER = {"user_provided": 0, "recruiter": 1, "careers_inbox": 1, "hr_inbox": 2,
                           "job_post_contact": 2, "founder": 3, "team_member": 4, "general_inbox": 6}


def has_recruiter_email(recs: list[dict]) -> bool:
    return any(r["email"] and r.get("kind") in RECRUITER_KINDS for r in recs)


def has_specific_email(recs: list[dict]) -> bool:
    """A recruiter / careers / founder address (anything better than a general inbox)."""
    return any(r["email"] and r.get("kind") in SPECIFIC_KINDS for r in recs)


def best_startup_recipient(recs: list[dict]) -> dict | None:
    usable = [r for r in recs if r["email"] and r.get("kind") != "other_function"]
    if not usable:
        return None
    usable.sort(key=lambda r: (STARTUP_RECIPIENT_ORDER.get(r.get("kind") or "", 5), r.get("domain_match") == 0,
                               r["verification"] != "verified_on_page"))
    return usable[0]


def pick_company_site(name: str, results) -> str | None:
    """Choose the startup's own domain from search results: host must contain the company name."""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    first = re.sub(r"[^a-z0-9]", "", name.lower().split()[0]) if name.split() else ""
    for r in results:
        h = host_of(r.url)
        if not h or NOT_COMPANY_SITE.search(h):
            continue
        label = re.sub(r"[^a-z0-9]", "", h.split(".")[0])
        if key and (key in label or (len(label) >= 4 and label in key)):
            return h
        if len(first) >= 4 and first == label:
            return h
    return None


def mentions_known(title: str, known: list[str]) -> bool:
    """Headline is about a startup we already have (same story from another outlet): skip, saves Gemini quota."""
    t = f" {re.sub(r'[^a-z0-9]+', ' ', title.lower())} "
    return any(len(n) >= 4 and f" {n} " in t for n in known)


@dataclass
class ScanReport:
    queries: int = 0
    articles: int = 0
    deals: int = 0
    new_startups: int = 0
    matches: int = 0
    filtered: int = 0
    errors: list[str] = field(default_factory=list)


class StartupPipeline:
    def __init__(self, p: Pipeline):
        self.p = p
        self.db = p.db
        self.s = p.s

    # ============================================================== discovery
    def scan(self, stop: threading.Event) -> ScanReport:
        rep = ScanReport()
        self._resume_unfinished(stop)
        due = self.db.due_queries(FUNDING_QUERIES, self.s.query_cooldown_hours, self.s.funding_queries_per_scan)
        if not due:
            self.p.event("Funding search: all queries ran within the last 24 h; nothing new to search yet.")
            return rep
        for q in due:
            if stop.is_set() or rep.articles >= self.s.funding_max_articles:
                break
            self.p.status_text = f"Funding news search: {q}"
            try:
                results = self.p.searx.search(q, time_range="month")
            except SearxError as e:
                rep.errors.append(str(e))
                self.p.event(f"Funding search failed: {e}", "warn")
                continue
            rep.queries += 1
            known = self.db.startup_names()
            news = [r for r in results if FUNDING_WORDS.search(f"{r.title} {r.snippet}")
                    and not self.db.article_read(r.url) and not mentions_known(r.title, known)]
            for r in news[:6]:
                if stop.is_set() or rep.articles >= self.s.funding_max_articles:
                    break
                self.p.status_text = f"Reading funding news: {r.title[:70]}"
                try:
                    with self.p._lock:
                        cands, _ = self.p._gemini(extract_deals, self.p.gemini, self.p.fetcher, r)
                except (QuotaExhausted, GeminiBusy):
                    raise
                except Exception as e:
                    log.exception("Funding extraction failed for %s", r.url)
                    rep.errors.append(f"{r.url}: {e}")
                    continue
                rep.articles += 1
                self.db.mark_article(r.url, len(cands))
                for c in cands:
                    if stop.is_set():
                        break
                    rep.deals += 1
                    try:
                        self._handle_deal(c, rep)
                    except (QuotaExhausted, GeminiBusy):
                        raise
                    except Exception as e:
                        log.exception("Failed to handle deal %s", c.deal.company)
                        rep.errors.append(f"{c.deal.company}: {e}")
            self.db.record_query(q, len(results), len(news))
        self.p.status_text = "idle"
        self.p.event(f"Funding scan finished: {rep.queries} searches, {rep.articles} articles, {rep.deals} deals, "
                     f"{rep.new_startups} new startups, {rep.matches} good fits, {rep.filtered} filtered.",
                     "good" if rep.matches else "info")
        return rep

    def _resume_unfinished(self, stop: threading.Event) -> None:
        for st in self.db.startups_with_status(dbm.NEW):
            if stop.is_set():
                return
            if self.score(st["id"])["status"] == dbm.PENDING:
                self.discover_contacts(st["id"])
                self.auto_draft(st["id"])

    def _handle_deal(self, c: DealCandidate, rep: ScanReport) -> None:
        d = c.deal
        name = d.company.strip()
        if not name or name.lower() not in c.source_text.lower():
            return                                             # junk extraction: not stored
        domain = domain_from_website(d.website)
        existing = self.db.find_startup(name, domain)
        if existing:
            self.db.add_startup_source(existing["id"], c.source_url)
            return
        reason = check_deal(c, self.s.funding_window_days)
        sid = self.db.insert_startup({
            "name": name, "website": d.website or None, "domain": domain, "description": d.description,
            "sector": d.sector, "hq_city": d.hq_city, "hq_country": d.hq_country, "region": c.region,
            "founded_year": d.founded_year, "founders": d.founders, "funding_round": norm_round(d.funding_round),
            "amount": d.amount, "funding_date": c.funding_date.isoformat() if c.funding_date else None,
            "investors": d.investors, "india_presence": d.india_presence,
            "india_hiring_evidence": d.hires_from_india_evidence, "use_of_funds": d.use_of_funds,
            "source_url": c.source_url, "source_title": c.source_title, "evidence": verified_evidence(c),
            "status": dbm.FILTERED if reason else dbm.NEW, "status_reason": reason})
        rep.new_startups += 1
        if reason:
            rep.filtered += 1
            self.p.event(f"Startup filtered: {name} — {reason}", "muted")
            return
        st = self.score(sid)
        if st["status"] != dbm.PENDING:
            rep.filtered += 1
            return
        rep.matches += 1
        self.discover_contacts(sid)
        self.auto_draft(sid)

    # ============================================================== scoring
    def score(self, startup_id: int) -> dict:
        with self.p._lock:
            st = self.db.get_startup(startup_id)
            self.p.status_text = f"Scoring startup fit: {st['name']}"
            try:
                fit: StartupFit = self.p._gemini(score_startup, self.p.gemini, self.p.resume, st,
                                                 self.s.candidate_experience, self.s.target_roles)
            except (QuotaExhausted, GeminiBusy):
                self.db.update_startup(startup_id, status=dbm.NEW, status_reason="waiting for Gemini")
                raise
            data = fit.model_dump() | {"model": self.p.gemini.last_model_used}
            if fit.recommendation == "skip" or fit.score < self.s.min_match_score:
                status, reason = dbm.FILTERED, f"fit {fit.score}/100 — not suitable enough"
                self.p.event(f"Startup low fit {fit.score}/100: {st['name']}", "muted")
            else:
                status, reason = dbm.PENDING, f"fit {fit.score}/100"
                self.p.event(f"Startup match {fit.score}/100: {st['name']} ({st.get('funding_round')}, "
                             f"{st.get('amount') or 'amount n/a'})", "good")
            if st["status"] in (dbm.SKIPPED, dbm.EMAILED):
                status, reason = st["status"], st["status_reason"]     # re-scoring keeps the user's decision
            self.db.update_startup(startup_id, score=fit.score, fit=data, status=status, status_reason=reason)
            return self.db.get_startup(startup_id)

    # ============================================================== contacts
    def discover_contacts(self, startup_id: int, deep: bool | None = None) -> list[dict]:
        """1) the company's own website; 2) web search for recruiters/founders; 3) deeper search.
        Stops as soon as a recruiter/careers/founder address exists. General inboxes (hello@, info@…) found on the
        way are kept and used only when nothing better turns up."""
        crawled = set()
        st = self.db.get_startup(startup_id)
        if not st.get("domain"):
            self._find_website(startup_id)
            st = self.db.get_startup(startup_id)
        if st.get("domain"):
            crawled.add(st["domain"])
            if not self._website_contacts(startup_id):          # the article's website was not theirs
                self.db.update_startup(startup_id, domain=None)
                self._find_website(startup_id)
                st = self.db.get_startup(startup_id)
                if st.get("domain") and st["domain"] not in crawled:
                    crawled.add(st["domain"])
                    self._website_contacts(startup_id)
        recs = self.db.get_startup_contacts(startup_id)
        if has_recruiter_email(recs) and not deep:
            return recs
        recs = self._discover(startup_id, deep=bool(deep))
        st = self.db.get_startup(startup_id)
        if st.get("domain") and st["domain"] not in crawled:          # web search revealed the website
            self._website_contacts(startup_id)
            recs = self.db.get_startup_contacts(startup_id)
        if deep is None and self.s.deep_email_search and not has_recruiter_email(recs):
            recs = self._discover(startup_id, deep=True)
        return recs

    def _find_website(self, startup_id: int) -> None:
        st = self.db.get_startup(startup_id)
        self.p.status_text = f"Looking up website: {st['name']}"
        try:
            results = self.p.searx.search(f'"{st["name"]}" official website {st.get("sector") or "AI startup"}')
        except SearxError as e:
            self.p.event(f"Website search failed for {st['name']}: {e}", "warn")
            return
        dom = pick_company_site(st["name"], results)
        if dom:
            self.db.update_startup(startup_id, domain=dom, website=st.get("website") or f"https://{dom}")
            self.p.event(f"Website for {st['name']}: {dom}", "muted")

    def _website_contacts(self, startup_id: int) -> bool:
        """Crawl the website for emails. Returns False if the site turned out not to be this company's."""
        st = self.db.get_startup(startup_id)
        self.p.status_text = f"Reading website for emails: {st['domain']}"
        people = list(st.get("founders") or [])
        people += [(c["name"], c.get("designation") or "") for c in self.db.get_startup_contacts(startup_id)
                   if c.get("name")]
        try:
            emails, msg = crawl_site_emails(self.p.fetcher, st["domain"], st["name"], people=people)
        except Exception as e:                       # network trouble must not stop the scan
            log.exception("Website crawl failed")
            emails, msg = [], f"website crawl failed: {e}"
        labels = {"careers_inbox": "Careers / HR inbox", "recruiter": "Hiring contact",
                  "founder": "Founder", "team_member": "Team member", "general_inbox": "General inbox"}
        for e in emails:
            label = (e.role or labels[e.kind]) + " (company website)"
            self.db.upsert_site_contact(startup_id, email=e.email, kind=e.kind, designation=label, name=e.name,
                                        source_url=e.page_url, evidence=e.context[:300])
        best = emails[0].kind.replace("_", " ") if emails else None
        self.p.event(f"Website {st['domain']} for {st['name']}: {msg}" + (f" (best: {best})" if best else ""),
                     "good" if emails else "info")
        return "does not mention" not in msg

    def find_missing_emails(self, stop: threading.Event) -> tuple[int, int]:
        """Website + web search for every good-fit startup without an email. Returns (searched, found)."""
        retry_before = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
        todo = []
        for r in self.db.list_startups([dbm.PENDING]):
            if r["email_status"] == "sent":
                continue
            recs = self.db.get_startup_contacts(r["id"])
            if not r["emails_found"]:
                todo.append(r)                       # nothing yet
            elif not has_recruiter_email(recs):      # only founder / general inbox: retry for a recruiter address
                searched = (self.db.get_startup(r["id"]).get("contacts_searched") or "").split(" ")[-1]
                if not searched or searched < retry_before:
                    todo.append(r)
        self.p.event(f"Startup email search started for {len(todo)} startups without a recruiter email.")
        searched = found = 0
        for r in todo:
            if stop.is_set():
                break
            recs = self.discover_contacts(r["id"])
            searched += 1
            if any(x["email"] for x in recs):
                found += 1
                self.auto_draft(r["id"])
        self.p.event(f"Startup email search finished: {searched} searched, emails found for {found}.",
                     "good" if found else "info")
        self.p.status_text = "idle"
        return searched, found

    def _discover(self, startup_id: int, deep: bool) -> list[dict]:
        with self.p._lock:
            st = self.db.get_startup(startup_id)
            self.p.status_text = f"{'Deep email' if deep else 'Contact'} search: {st['name']}"
            try:
                found, domain, _ = self.p._gemini(
                    self.p.contacts.find, st["name"], "AI/ML Engineer (startup outreach)",
                    ", ".join(x for x in (st.get("hq_city"), st.get("hq_country")) if x), st.get("source_url") or "",
                    "", st.get("domain"), deep, True)
            except (QuotaExhausted, GeminiBusy):
                raise
            except Exception as e:
                self.p.event(f"Contact search failed for {st['name']}: {e}", "warn")
                return self.db.get_startup_contacts(startup_id)
            if domain and not st.get("domain"):
                self.db.update_startup(startup_id, domain=domain)
            for c in found:
                self.db.add_startup_contact(startup_id, email=c.email, verification=c.verification,
                                            source_url=c.source_url, name=c.name, designation=c.designation,
                                            linkedin=c.linkedin, evidence=c.evidence, kind=c.kind,
                                            domain_match=c.domain_match)
            recs = self.db.get_startup_contacts(startup_id)
            n_email = sum(1 for r in recs if r["email"])
            self.db.update_startup(startup_id, contacts_searched=f"{'deep' if deep else 'quick'} {dbm.now()}")
            self.p.event(f"{'Deep email search' if deep else 'Contacts'} for startup {st['name']}: {len(recs)} "
                         f"contacts, {n_email} with email", "good" if n_email else "info")
            return recs

    def add_user_contact(self, startup_id: int, email: str, name: str | None, source: str | None) -> int:
        st = self.db.get_startup(startup_id)
        if not st:
            raise ValueError("Startup not found")
        if st["status"] == dbm.EMAILED:
            raise ValueError("This startup has already been emailed.")
        if any(r["email"] and r["verification"] != USER_PROVIDED for r in self.db.get_startup_contacts(startup_id)):
            raise ValueError("This startup already has an email found by search; manual emails are only for "
                             "startups without one.")
        if not is_valid_email(email):
            raise ValueError(f"Not a valid email address: {email}")
        return self.db.add_startup_contact(startup_id, email=email.strip(), name=name or None,
                                           verification=USER_PROVIDED, source_url=source or "entered manually",
                                           designation="Contact (user provided)", kind="user_provided",
                                           evidence="Entered by you")

    # ============================================================== email
    def auto_draft(self, startup_id: int) -> dict | None:
        if not self.s.auto_draft:
            return None
        e = self.db.latest_startup_email(startup_id)
        recs = self.db.get_startup_contacts(startup_id)
        rec = best_startup_recipient(recs)
        if e and e["status"] == "sent":
            return e
        if e and e["status"] == "draft":
            current = next((r for r in recs if r["email"] == e["recipient"]), None)
            upgrade = (current and rec and rec["email"] != current["email"] and current.get("kind") != "user_provided"
                       and STARTUP_RECIPIENT_ORDER.get(rec.get("kind"), 5)
                       < STARTUP_RECIPIENT_ORDER.get(current.get("kind"), 5))
            if not upgrade:
                return e
            self.p.event(f"Better contact found for {self.db.get_startup(startup_id)['name']}: re-drafting to "
                         f"{rec['email']} ({rec.get('kind', '').replace('_', ' ')}) instead of {e['recipient']}")
        return self.draft_email(startup_id, rec) if rec else None

    def draft_email(self, startup_id: int, contact: dict, attachment_key: str | None = None) -> dict:
        with self.p._lock:
            st = self.db.get_startup(startup_id)
            if not st.get("fit"):
                raise ValueError("This startup has not been scored yet.")
            fit = StartupFit(**{k: v for k, v in st["fit"].items() if k in StartupFit.model_fields})
            if attachment_key and attachment_key in self.p.resume.variants:
                fit.best_resume = attachment_key
            self.p.status_text = f"Writing email to {st['name']}"
            d = self.p._gemini(write_outreach, self.p.gemini, self.p.resume, st, fit, self.s.candidate_experience,
                               contact.get("name"))
            prev = self.db.latest_startup_email(startup_id)
            if prev and prev["status"] == "draft":
                self.db.update_startup_email(prev["id"], status="replaced")
            eid = self.db.save_startup_email(startup_id, contact["email"], contact.get("name"), d.subject, d.body,
                                             str(self.p.resume.variants[fit.best_resume].path),
                                             self.p.gemini.last_model_used, d.attachment_filename)
            self.p.event(f"Email drafted for startup {st['name']} → {contact['email']}")
            return self.db.get_startup_email(eid)

    def edit_email(self, email_id: int, *, recipient: str, subject: str, body: str, attachment_key: str) -> dict:
        e = self.db.get_startup_email(email_id)
        if not e or e["status"] != "draft":
            raise ValueError("Only drafts can be edited")
        if recipient.strip().lower() != e["recipient"]:
            raise ValueError("The recipient can't be changed. Write a new email from another contact instead.")
        if attachment_key not in self.p.resume.variants:
            raise ValueError(f"Unknown resume: {attachment_key}")
        if not subject.strip() or not body.strip():
            raise ValueError("Subject and body are required")
        self.db.update_startup_email(email_id, subject=subject.strip(), body=body.strip(),
                                     attachment=str(self.p.resume.variants[attachment_key].path))
        return self.db.get_startup_email(email_id)

    def duplicate_reason(self, st: dict, recipient: str) -> str | None:
        r = self.db.conn.execute("SELECT id FROM startup_emails WHERE startup_id=? AND status='sent'",
                                 (st["id"],)).fetchone()
        if r:
            return f"{st['name']} was already emailed (startup email #{r['id']})"
        dup = self.db.recipient_recently_emailed(recipient, self.s.duplicate_window_days)
        if dup:
            return dup
        key = dbm.company_key(st["name"])
        for j in self.db.conn.execute(
                "SELECT j.company, e.id FROM emails e JOIN jobs j ON j.id=e.job_id WHERE e.status='sent' "
                "AND e.sent_at>=date('now', ?)", (f"-{self.s.duplicate_window_days} days",)):
            if dbm.company_key(j["company"]) == key:
                return f"you already applied to a job at {j['company']} recently (email #{j['id']})"
        return None

    def send(self, email_id: int) -> SendResult:
        e = self.db.get_startup_email(email_id)
        if not e:
            return SendResult(False, "Email not found")
        st = self.db.get_startup(e["startup_id"])
        if e["status"] == "sent":
            return SendResult(False, "This email was already sent.")
        if e["status"] != "draft":
            return SendResult(False, f"Email is '{e['status']}', not a draft.")
        if not self.p.gmail or not self.p.gmail_ready:
            return SendResult(False, "Gmail is not authenticated — nothing sent. Run: python run.py --gmail-auth")
        dup = self.duplicate_reason(st, e["recipient"])
        if dup:
            return SendResult(False, f"Blocked duplicate: {dup}")
        if self.db.sent_today() >= self.s.daily_email_limit:
            return SendResult(False, f"Daily limit of {self.s.daily_email_limit} emails reached. Nothing sent.")
        att = Path(e["attachment"])
        if not att.exists():
            return SendResult(False, f"Attachment missing: {att}")
        self.db.update_startup_email(email_id, status="approved")
        try:
            mid = self.p.gmail.send(e["recipient"], e["subject"], e["body"], att, e.get("attachment_filename"))
        except Exception as ex:
            log.exception("Send failed")
            self.db.update_startup_email(email_id, status="draft", error=str(ex))
            return SendResult(False, f"Send failed: {ex}")
        self.db.update_startup_email(email_id, status="sent", gmail_message_id=mid, sent_at=dbm.now(), error=None)
        self.db.update_startup(st["id"], status=dbm.EMAILED, status_reason=f"emailed {e['recipient']}")
        self.p.event(f"SENT: startup outreach to {st['name']} → {e['recipient']}", "good")
        return SendResult(True, f"Sent to {e['recipient']} (Gmail id {mid}).")

    def skip(self, startup_id: int) -> None:
        self.db.update_startup(startup_id, status=dbm.SKIPPED, status_reason="skipped by user")
        e = self.db.latest_startup_email(startup_id)
        if e and e["status"] == "draft":
            self.db.update_startup_email(e["id"], status="skipped")

    def restore(self, startup_id: int) -> None:
        self.db.update_startup(startup_id, status=dbm.PENDING, status_reason="restored by user")
