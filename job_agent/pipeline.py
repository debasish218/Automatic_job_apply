"""UI-independent orchestration. The web dashboard (and later a scheduler) drive these methods."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import db as dbm
from .config import Settings
from .email_writer import write_email
from .filters import extract_min_years, is_india, title_check
from .gemini_client import GeminiBusy, GeminiClient, GeminiError
from .gmail_client import GmailClient
from .matcher import MatchResult, analyse_job
from .recruiter import USER_PROVIDED, emails_in_text, is_valid_email
from .resume import ResumeBundle
from .search.contacts import ContactFinder, company_domain_from_url
from .search.fetch import PageFetcher
from .search.job_search import build_candidate, build_queries, canonical_url, is_stale, site_of
from .search.searx import SearxClient, SearxError, SearchResult

log = logging.getLogger(__name__)


class QuotaExhausted(RuntimeError):
    """All Gemini models are out of free quota for today."""


@dataclass
class PassReport:
    queries: int = 0
    results: int = 0
    postings_seen: int = 0
    new_jobs: int = 0
    ready: int = 0
    filtered: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SendResult:
    ok: bool
    message: str


class Pipeline:
    def __init__(self, settings: Settings, gemini: GeminiClient, resume: ResumeBundle, db: dbm.Database,
                 searx: SearxClient, fetcher: PageFetcher, gmail: GmailClient | None,
                 on_event: Callable[[str, str, int | None], None] | None = None):
        self.s = settings
        self.gemini = gemini
        self.resume = resume
        self.db = db
        self.searx = searx
        self.fetcher = fetcher
        self.gmail = gmail
        self.gmail_ready = False
        self.contacts = ContactFinder(searx, fetcher, gemini)
        self.queries = build_queries(settings.target_roles)
        self._emit = on_event or (lambda msg, level, job_id: None)
        self.status_text = "idle"
        self._lock = threading.RLock()          # serialises Gemini-heavy actions between worker and UI

    def event(self, msg: str, level: str = "info", job_id: int | None = None) -> None:
        log.info(msg)
        self.db.log_event(msg, level, job_id)
        self._emit(msg, level, job_id)

    def _gemini(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except GeminiBusy:
            raise
        except GeminiError as e:
            if "No Gemini model available" in str(e):
                raise QuotaExhausted(str(e)) from e
            raise

    # ============================================================== discovery
    def resume_unfinished(self, stop: threading.Event) -> None:
        """Finish jobs interrupted by a Gemini outage: analyse, then contacts + draft."""
        for job in self.db.jobs_with_status(dbm.NEW):
            if stop.is_set():
                return
            a = self.analyse(job["id"])
            if self.db.get_job(job["id"])["status"] == dbm.PENDING:
                self.event(f"Match {a['score']}/100: {job['title']} @ {job['company']}", "good", job["id"])
                self.discover_contacts(job["id"])
                self.auto_draft(job["id"])

    def run_pass(self, stop: threading.Event) -> PassReport:
        rep = PassReport()
        self.resume_unfinished(stop)
        due = self.db.due_queries(self.queries, self.s.query_cooldown_hours, self.s.queries_per_pass)
        if not due:
            self.event("All search queries ran within the cooldown window; nothing new to search yet.")
            return rep
        for q in due:
            if stop.is_set():
                break
            self.status_text = f"Searching: {q}"
            try:
                results = self.searx.search(q, time_range="month")
            except SearxError as e:
                rep.errors.append(str(e))
                self.event(f"Search failed: {e}", "warn")
                continue
            rep.queries += 1
            rep.results += len(results)
            postings = [(r, site_of(r.url)) for r in results]
            postings = [(r, s) for r, s in postings if s]
            rep.postings_seen += len(postings)
            new = 0
            for r, site in postings:
                if stop.is_set():
                    break
                try:
                    outcome = self._process_result(r, site, rep)
                except (QuotaExhausted, GeminiBusy):
                    raise
                except Exception as e:      # one bad posting must not stop discovery
                    log.exception("Failed to process %s", r.url)
                    rep.errors.append(f"{r.url}: {e}")
                    self.event(f"Skipped a posting that could not be processed ({type(e).__name__}: {e}): {r.url}",
                               "warn")
                    continue
                new += outcome == "new"
            self.db.record_query(q, len(results), len(postings))
            self.event(f"Query «{q}» → {len(results)} results, {len(postings)} job postings, {new} new")
        self.status_text = "idle"
        return rep

    def _quick_reject(self, r: SearchResult) -> str | None:
        """Cheap checks on the search title/snippet before spending a page fetch or Gemini call."""
        title = r.title.split(" | ")[0]
        keep, reason = title_check(title)
        if not keep and reason.startswith("senior"):
            return reason
        yrs, _ = extract_min_years(f"{r.title}. {r.snippet}")
        if yrs is not None and yrs > self.s.max_required_years:
            return f"requires {yrs:g}+ years (search snippet)"
        return None

    def _process_result(self, r: SearchResult, site: str, rep: PassReport) -> str:
        ext_id = canonical_url(r.url)
        if self.db.job_exists(ext_id):
            return "seen"
        why = self._quick_reject(r)
        if why:
            rep.filtered += 1
            return "rejected"
        self.status_text = f"Reading posting: {r.title[:70]}"
        cand = self._gemini(build_candidate, self.gemini, self.fetcher, r, site)
        if cand is None:
            return "not_a_posting"
        p = cand.posting
        if not p.company or not p.title:
            return "incomplete"
        job = p.to_dict() | {"site": site, "snippet": r.snippet, "page_status": cand.page_status,
                             "company_domain": company_domain_from_url(cand.company_url)}
        job_id, is_new = self.db.upsert_job(job)
        if not is_new:
            return "duplicate"
        rep.new_jobs += 1
        # deterministic filters with recorded reasons
        reason = None
        if cand.extra.get("closed"):
            reason = "posting says it is closed"
        elif not (is_india(p.location) or cand.extra.get("is_india")):
            reason = f"not in India ({p.location or 'location unknown'})"
        else:
            keep, why = title_check(p.title)
            if not keep:
                reason = why
        if not reason:
            reason = is_stale(p.posted_at, cand.valid_through, self.s.max_posting_age_days)
        min_years = cand.min_years
        exp_text = cand.experience_text
        if min_years is None:
            min_years, snippet = extract_min_years(p.description)
            exp_text = exp_text or snippet
        self.db.set_job_experience(job_id, exp_text, min_years)
        if not reason and min_years is not None and min_years > self.s.max_required_years:
            reason = f"requires {min_years:g}+ years"
        if reason:
            self.db.set_job_status(job_id, dbm.FILTERED, reason)
            rep.filtered += 1
            self.event(f"Filtered: {p.title} @ {p.company} — {reason}", "muted", job_id)
            return "new"
        a = self.analyse(job_id)
        if self.db.get_job(job_id)["status"] != dbm.PENDING:
            rep.filtered += 1
            self.event(f"Low match {a['score']}/100: {p.title} @ {p.company}", "muted", job_id)
            return "new"
        self.event(f"Match {a['score']}/100: {p.title} @ {p.company} ({p.location})", "good", job_id)
        self.discover_contacts(job_id)
        self.auto_draft(job_id)
        rep.ready += 1
        return "new"

    # ============================================================== analysis
    def analyse(self, job_id: int) -> dict:
        with self._lock:
            job = self.db.get_job(job_id)
            self.status_text = f"Matching with resume: {job['title']} @ {job['company']}"
            try:
                m: MatchResult = self._gemini(analyse_job, self.gemini, self.resume, job, self.s.candidate_experience,
                                              self.s.candidate_experience_months, self.s.target_roles)
            except (QuotaExhausted, GeminiBusy):
                self.db.set_job_status(job_id, dbm.NEW, "waiting for Gemini")
                raise
            except Exception as e:
                self.db.set_job_status(job_id, dbm.ERROR, f"analysis failed: {e}")
                raise
            a = m.model_dump() | {"model": self.gemini.last_model_used}
            self.db.save_analysis(job_id, a)
            if m.required_experience and m.required_experience != "Not stated":
                self.db.set_job_experience(job_id, m.required_experience,
                                           m.required_min_years if m.required_min_years is not None
                                           else job.get("min_years"))
            if m.recommendation == "skip" or m.score < self.s.min_match_score:
                self.db.set_job_status(job_id, dbm.FILTERED,
                                       f"match {m.score}/100, Gemini recommends '{m.recommendation}'")
            else:
                self.db.set_job_status(job_id, dbm.PENDING, f"match {m.score}/100")
            return self.db.get_analysis(job_id)

    # ============================================================== contacts
    def discover_contacts(self, job_id: int) -> list[dict]:
        with self._lock:
            job = self.db.get_job(job_id)
            # 1) emails printed in the job description itself
            for c in emails_in_text(job.get("description") or "", job.get("url") or ""):
                self.db.add_recruiter(job_id, email=c.email, verification="verified_on_page", source_url=c.source_url,
                                      designation="Contact in job posting", kind="job_post_contact",
                                      evidence=c.context.strip()[:300])
            # 2) web search for recruiters / HR / careers contacts
            self.status_text = f"Searching recruiter contacts: {job['company']}"
            try:
                found, domain, _ = self._gemini(self.contacts.find, job["company"], job["title"],
                                                job.get("location") or "", job.get("url") or "",
                                                job.get("description") or "", job.get("company_domain"))
            except (QuotaExhausted, GeminiBusy):
                raise
            except Exception as e:
                self.event(f"Contact search failed for {job['company']}: {e}", "warn", job_id)
                return self.db.get_recruiters(job_id)
            if domain and not job.get("company_domain"):
                self.db.conn.execute("UPDATE jobs SET company_domain=? WHERE id=?", (domain, job_id))
                self.db.conn.commit()
            for c in found:
                self.db.add_recruiter(job_id, email=c.email, verification=c.verification, source_url=c.source_url,
                                      name=c.name, designation=c.designation, linkedin=c.linkedin,
                                      evidence=c.evidence, kind=c.kind, domain_match=c.domain_match)
            recs = self.db.get_recruiters(job_id)
            n_email = sum(1 for r in recs if r["email"])
            self.event(f"Contacts for {job['company']}: {len(recs)} found, {n_email} with email", "info", job_id)
            return recs

    def add_user_recruiter(self, job_id: int, email: str, name: str | None, source: str | None) -> int:
        if not is_valid_email(email):
            raise ValueError(f"Not a valid email address: {email}")
        return self.db.add_recruiter(job_id, email=email.strip(), name=name or None, verification=USER_PROVIDED,
                                     source_url=source or "entered manually", designation="Recruiter (user provided)",
                                     kind="user_provided", evidence="Entered by you")

    @staticmethod
    def best_recipient(recs: list[dict]) -> dict | None:
        usable = [r for r in recs if r["email"]]
        if not usable:
            return None
        order = {"user_provided": 0, "job_post_contact": 1, "recruiter": 2, "hr_inbox": 3, "careers_inbox": 4}
        usable.sort(key=lambda r: (order.get(r.get("kind") or "", 5), r.get("domain_match") == 0,
                                   r["verification"] != "verified_on_page"))
        return usable[0]

    # ============================================================== email
    def auto_draft(self, job_id: int) -> dict | None:
        if not self.s.auto_draft:
            return None
        e = self.db.latest_email_for_job(job_id)
        if e and e["status"] in ("draft", "sent"):
            return e
        rec = self.best_recipient(self.db.get_recruiters(job_id))
        return self.draft_email(job_id, rec) if rec else None

    def draft_email(self, job_id: int, recruiter: dict, attachment_key: str | None = None) -> dict:
        with self._lock:
            job = self.db.get_job(job_id)
            a = self.db.get_analysis(job_id)
            m = MatchResult(**{k: a[k] for k in MatchResult.model_fields if k in a},
                            required_min_years=job.get("min_years"))
            if attachment_key and attachment_key in self.resume.variants:
                m.best_resume = attachment_key
            self.status_text = f"Writing email: {job['title']} @ {job['company']}"
            d = self._gemini(write_email, self.gemini, self.resume, job, m, self.s.candidate_experience,
                             recruiter.get("name"))
            prev = self.db.latest_email_for_job(job_id)
            if prev and prev["status"] == "draft":
                self.db.update_email(prev["id"], status="replaced")
            attachment = str(self.resume.variants[m.best_resume].path)
            eid = self.db.save_email(job_id, recruiter["email"], recruiter.get("name"), d.subject, d.body, attachment,
                                     self.gemini.last_model_used)
            self.event(f"Email drafted for {job['title']} @ {job['company']} → {recruiter['email']}", "info", job_id)
            return self.db.get_email(eid)

    def edit_email(self, email_id: int, *, recipient: str, subject: str, body: str, attachment_key: str) -> dict:
        e = self.db.get_email(email_id)
        if not e or e["status"] != "draft":
            raise ValueError("Only drafts can be edited")
        if not is_valid_email(recipient):
            raise ValueError(f"Not a valid email address: {recipient}")
        if attachment_key not in self.resume.variants:
            raise ValueError(f"Unknown resume: {attachment_key}")
        if not subject.strip() or not body.strip():
            raise ValueError("Subject and body are required")
        self.db.update_email(email_id, recipient=recipient.strip().lower(), subject=subject.strip(), body=body.strip(),
                             attachment=str(self.resume.variants[attachment_key].path))
        return self.db.get_email(email_id)

    def send(self, email_id: int) -> SendResult:
        e = self.db.get_email(email_id)
        if not e:
            return SendResult(False, "Email not found")
        job = self.db.get_job(e["job_id"])
        if e["status"] == "sent":
            return SendResult(False, "This email was already sent.")
        if e["status"] != "draft":
            return SendResult(False, f"Email is '{e['status']}', not a draft.")
        if not self.gmail or not self.gmail_ready:
            return SendResult(False, "Gmail is not authenticated — nothing sent. Run: python run.py --gmail-auth")
        dup = self.db.duplicate_send_reason(job["id"], e["recipient"], job["company"], self.s.duplicate_window_days)
        if dup:
            return SendResult(False, f"Blocked duplicate: {dup}")
        if self.db.sent_today() >= self.s.daily_email_limit:
            return SendResult(False, f"Daily limit of {self.s.daily_email_limit} emails reached. Nothing sent.")
        att = Path(e["attachment"])
        if not att.exists():
            return SendResult(False, f"Attachment missing: {att}")
        self.db.update_email(email_id, status="approved")
        try:
            mid = self.gmail.send(e["recipient"], e["subject"], e["body"], att)
        except Exception as ex:
            log.exception("Send failed")
            self.db.update_email(email_id, status="draft", error=str(ex))
            return SendResult(False, f"Send failed: {ex}")
        self.db.update_email(email_id, status="sent", gmail_message_id=mid, sent_at=dbm.now(), error=None)
        self.db.set_job_status(job["id"], dbm.EMAILED, f"emailed {e['recipient']}")
        self.event(f"SENT: {job['title']} @ {job['company']} → {e['recipient']}", "good", job["id"])
        return SendResult(True, f"Sent to {e['recipient']} (Gmail id {mid}).")

    def skip(self, job_id: int, reason: str = "skipped by user") -> None:
        self.db.set_job_status(job_id, dbm.SKIPPED, reason)
        e = self.db.latest_email_for_job(job_id)
        if e and e["status"] == "draft":
            self.db.update_email(e["id"], status="skipped")

    def restore(self, job_id: int) -> None:
        self.db.set_job_status(job_id, dbm.PENDING, "restored by user")
