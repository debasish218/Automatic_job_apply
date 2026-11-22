"""UI-independent orchestration. A scheduler or web UI could drive the same methods later."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import db as dbm
from .config import Settings
from .email_writer import write_email
from .filters import extract_min_years, is_india, title_check
from .gemini_client import GeminiClient
from .gmail_client import GmailClient
from .matcher import MatchResult, analyse_job
from .recruiter import USER_PROVIDED, find_contacts, is_valid_email
from .resume import ResumeBundle
from .sources import JobSource

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    source: str
    company: str
    total: int = 0
    india: int = 0
    relevant: int = 0
    already_seen: int = 0
    new_candidates: list[int] = field(default_factory=list)
    filtered: list[tuple[str, str, str]] = field(default_factory=list)    # (title, location, reason)
    error: str | None = None


@dataclass
class SendResult:
    ok: bool
    message: str


class Pipeline:
    def __init__(self, settings: Settings, gemini: GeminiClient, resume: ResumeBundle, db: dbm.Database,
                 session, gmail: GmailClient | None):
        self.s = settings
        self.gemini = gemini
        self.resume = resume
        self.db = db
        self.session = session
        self.gmail = gmail
        self.gmail_ready = False

    # ------------------------------------------------------------- discovery
    def scan_source(self, src: JobSource) -> ScanReport:
        rep = ScanReport(source=repr(src), company=src.company)
        try:
            postings = src.fetch()
        except Exception as e:
            log.exception("Source %s failed", src)
            rep.error = f"{type(e).__name__}: {e}"
            return rep
        rep.total = len(postings)
        for p in postings:
            if not is_india(p.location):
                continue
            rep.india += 1
            keep, reason = title_check(p.title)
            if not keep and reason in ("non-engineering role", "title not in AI/ML target family"):
                continue
            rep.relevant += 1
            if not keep:
                rep.filtered.append((p.title, p.location, reason))
                continue
            try:
                src.fetch_description(p)
            except Exception as e:
                log.warning("Description fetch failed for %s: %s", p.url, e)
            job_id, is_new = self.db.upsert_job(p.to_dict())
            if not is_new:
                rep.already_seen += 1
                continue
            min_years, snippet = extract_min_years(p.description)
            self.db.set_job_experience(job_id, snippet, min_years)
            if min_years is not None and min_years > self.s.max_required_years:
                why = f"requires {min_years:g}+ years"
                self.db.set_job_status(job_id, dbm.FILTERED, why)
                rep.filtered.append((p.title, p.location, why))
                continue
            rep.new_candidates.append(job_id)
        return rep

    # ------------------------------------------------------------- analysis
    def analyse(self, job_id: int) -> dict:
        job = self.db.get_job(job_id)
        try:
            m: MatchResult = analyse_job(self.gemini, self.resume, job, self.s.candidate_experience,
                                         self.s.candidate_experience_months, self.s.target_roles)
        except Exception as e:
            self.db.set_job_status(job_id, dbm.ERROR, f"analysis failed: {e}")
            raise
        a = m.model_dump()
        self.db.save_analysis(job_id, a)
        self.db.set_job_experience(job_id, m.required_experience, m.required_min_years)
        if m.recommendation == "skip" or m.score < self.s.min_match_score:
            self.db.set_job_status(job_id, dbm.FILTERED,
                                   f"match {m.score}/100, Gemini recommends '{m.recommendation}'")
        else:
            self.db.set_job_status(job_id, dbm.PENDING, f"match {m.score}/100")
        return self.db.get_analysis(job_id)

    def pending_jobs(self) -> list[dict]:
        jobs = self.db.jobs_with_status(dbm.PENDING)
        return sorted(jobs, key=lambda j: -((self.db.get_analysis(j["id"]) or {}).get("score") or 0))

    # ------------------------------------------------------------- recruiters
    def discover_recruiters(self, job_id: int) -> list[dict]:
        if not self.db.get_recruiters(job_id):
            job = self.db.get_job(job_id)
            for c in find_contacts(job, self.session, self.s.http_user_agent):
                self.db.add_recruiter(job_id, email=c.email, verification=c.verification, source_url=c.source_url,
                                      designation={"recruiting_inbox": "Recruiting inbox",
                                                   "personal": "Contact named in posting"}.get(c.kind, "Contact"))
        return self.db.get_recruiters(job_id)

    def add_user_recruiter(self, job_id: int, email: str, name: str | None, source: str | None) -> None:
        if not is_valid_email(email):
            raise ValueError(f"Not a valid email address: {email}")
        self.db.add_recruiter(job_id, email=email, name=name or None, verification=USER_PROVIDED,
                              source_url=source or "entered manually", designation="Recruiter (user provided)")

    # ------------------------------------------------------------- email
    def draft_email(self, job_id: int, recruiter: dict) -> dict:
        job = self.db.get_job(job_id)
        a = self.db.get_analysis(job_id)
        m = MatchResult(**{k: a[k] for k in MatchResult.model_fields if k in a},
                        required_min_years=job.get("min_years"))
        d = write_email(self.gemini, self.resume, job, m, self.s.candidate_experience, recruiter.get("name"))
        attachment = str(self.resume.variants[m.best_resume].path)
        eid = self.db.save_email(job_id, recruiter["email"], recruiter.get("name"), d.subject, d.body, attachment)
        return self.db.get_email(eid)

    def send(self, email_id: int) -> SendResult:
        from pathlib import Path

        e = self.db.get_email(email_id)
        job = self.db.get_job(e["job_id"])
        if e["status"] == "sent":
            return SendResult(False, "This email was already sent.")
        if not self.gmail or not self.gmail_ready:
            return SendResult(False, "Gmail is not authenticated — draft saved, nothing sent. "
                                     "Run: python run.py --gmail-auth")
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
            self.db.update_email(email_id, status="failed", error=str(ex))
            return SendResult(False, f"Send failed: {ex}")
        self.db.update_email(email_id, status="sent", gmail_message_id=mid, sent_at=dbm.now())
        self.db.set_job_status(job["id"], dbm.EMAILED, f"emailed {e['recipient']}")
        return SendResult(True, f"Sent to {e['recipient']} (Gmail id {mid}).")

    def skip(self, job_id: int, reason: str = "skipped by user") -> None:
        self.db.set_job_status(job_id, dbm.SKIPPED, reason)
        e = self.db.latest_email_for_job(job_id)
        if e and e["status"] == "draft":
            self.db.update_email(e["id"], status="skipped")
