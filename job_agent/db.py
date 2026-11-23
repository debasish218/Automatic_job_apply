"""SQLite persistence for jobs, analyses, recruiter contacts and emails."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    description TEXT,
    experience_req TEXT,
    min_years REAL,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    status_reason TEXT,
    duplicate_of INTEGER,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS analyses (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    score INTEGER,
    matching_skills TEXT,
    missing_skills TEXT,
    required_experience TEXT,
    experience_comparison TEXT,
    experience_gap INTEGER,
    relevance TEXT,
    recommendation TEXT,
    reasoning TEXT,
    best_resume TEXT,
    company_highlights TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruiters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    name TEXT,
    designation TEXT,
    email TEXT,
    linkedin TEXT,
    source_url TEXT,
    verification TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, email)
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    recipient TEXT NOT NULL,
    recruiter_name TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attachment TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    gmail_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);

CREATE TABLE IF NOT EXISTS search_queries (
    query TEXT PRIMARY KEY,
    last_run TEXT,
    runs INTEGER DEFAULT 0,
    results INTEGER DEFAULT 0,
    postings INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    job_id INTEGER
);
"""

# Columns added after the first release (applied with ALTER TABLE if missing).
MIGRATIONS = {
    "jobs": [("site", "TEXT"), ("snippet", "TEXT"), ("company_domain", "TEXT"), ("page_status", "TEXT"),
             ("contacts_searched", "TEXT")],
    "recruiters": [("evidence", "TEXT"), ("kind", "TEXT"), ("domain_match", "INTEGER")],
    "analyses": [("model", "TEXT")],
    "emails": [("model", "TEXT"), ("attachment_filename", "TEXT")],
}

# Job statuses
NEW, FILTERED, ANALYZED, PENDING, SKIPPED, EMAILED, DUPLICATE, ERROR = (
    "new", "filtered_out", "analyzed", "pending_review", "skipped", "emailed", "duplicate", "error")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def fingerprint(company: str, title: str, location: str) -> str:
    key = "|".join(normalize(x) for x in (company, title, location))
    return hashlib.sha1(key.encode()).hexdigest()


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- jobs
    def upsert_job(self, job: dict[str, Any]) -> tuple[int, bool]:
        """Insert a job. Returns (job_id, is_new). Cross-source duplicates are flagged."""
        cur = self.conn.execute("SELECT id FROM jobs WHERE source=? AND external_id=?",
                                (job["source"], job["external_id"]))
        row = cur.fetchone()
        if row:
            self.conn.execute("UPDATE jobs SET last_seen_at=? WHERE id=?", (now(), row["id"]))
            self.conn.commit()
            return row["id"], False
        fp = fingerprint(job["company"], job["title"], job.get("location", ""))
        dup = self.conn.execute("SELECT id FROM jobs WHERE fingerprint=? ORDER BY id LIMIT 1", (fp,)).fetchone()
        ts = now()
        cur = self.conn.execute(
            """INSERT INTO jobs (source, external_id, fingerprint, company, title, location, url, description,
                                 posted_at, discovered_at, last_seen_at, status, status_reason, duplicate_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["source"], job["external_id"], fp, job["company"], job["title"], job.get("location"),
             job.get("url"), job.get("description"), job.get("posted_at"), ts, ts,
             DUPLICATE if dup else NEW, f"same company/title/location as job #{dup['id']}" if dup else None,
             dup["id"] if dup else None))
        extra = {k: job[k] for k in ("site", "snippet", "company_domain", "page_status") if job.get(k)}
        if extra:
            self.conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in extra)} WHERE id=?",
                              (*extra.values(), cur.lastrowid))
        self.conn.commit()
        return cur.lastrowid, dup is None

    def set_job_status(self, job_id: int, status: str, reason: str | None = None) -> None:
        self.conn.execute("UPDATE jobs SET status=?, status_reason=? WHERE id=?", (status, reason, job_id))
        self.conn.commit()

    def set_job_experience(self, job_id: int, text: str | None, min_years: float | None) -> None:
        self.conn.execute("UPDATE jobs SET experience_req=?, min_years=? WHERE id=?", (text, min_years, job_id))
        self.conn.commit()

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def jobs_with_status(self, *statuses: str) -> list[dict]:
        q = f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id"
        return [dict(r) for r in self.conn.execute(q, statuses)]

    # ---------------------------------------------------------------- analyses
    def save_analysis(self, job_id: int, a: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO analyses (job_id, score, matching_skills, missing_skills, required_experience,
                experience_comparison, experience_gap, relevance, recommendation, reasoning, best_resume,
                company_highlights, created_at, model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, a["score"], json.dumps(a["matching_skills"]), json.dumps(a["missing_skills"]),
             a["required_experience"], a["experience_comparison"], int(a["experience_gap"]), a["relevance"],
             a["recommendation"], a["reasoning"], a["best_resume"], json.dumps(a.get("company_highlights", [])),
             now(), a.get("model")))
        self.conn.commit()

    def get_analysis(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM analyses WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("matching_skills", "missing_skills", "company_highlights"):
            d[k] = json.loads(d[k] or "[]")
        d["experience_gap"] = bool(d["experience_gap"])
        return d

    # ---------------------------------------------------------------- recruiters
    def add_recruiter(self, job_id: int, *, email: str | None, verification: str, source_url: str | None,
                      name: str | None = None, designation: str | None = None, linkedin: str | None = None,
                      evidence: str | None = None, kind: str | None = None, domain_match: bool | None = None) -> int:
        if not email and linkedin:
            r = self.conn.execute("SELECT id FROM recruiters WHERE job_id=? AND linkedin=?", (job_id, linkedin)).fetchone()
            if r:
                return r["id"]
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO recruiters (job_id, name, designation, email, linkedin, source_url,
               verification, created_at, evidence, kind, domain_match) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, name, designation, email.lower() if email else None, linkedin, source_url, verification, now(),
             evidence, kind, None if domain_match is None else int(domain_match)))
        self.conn.commit()
        return cur.lastrowid

    def get_recruiter(self, rid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM recruiters WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def get_recruiters(self, job_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM recruiters WHERE job_id=? ORDER BY id", (job_id,))]

    # ---------------------------------------------------------------- emails
    def save_email(self, job_id: int, recipient: str, recruiter_name: str | None, subject: str, body: str,
                   attachment: str, model: str | None = None, attachment_filename: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO emails (job_id, recipient, recruiter_name, subject, body, attachment, status, created_at,
               model, attachment_filename) VALUES (?,?,?,?,?,?,'draft',?,?,?)""",
            (job_id, recipient.lower(), recruiter_name, subject, body, attachment, now(), model, attachment_filename))
        self.conn.commit()
        return cur.lastrowid

    def update_email(self, email_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE emails SET {cols} WHERE id=?", (*fields.values(), email_id))
        self.conn.commit()

    def get_email(self, email_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        return dict(row) if row else None

    def latest_email_for_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM emails WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        return dict(row) if row else None

    def duplicate_send_reason(self, job_id: int, recipient: str, company: str, window_days: int) -> str | None:
        """Return a reason string if sending would be a duplicate, else None."""
        r = self.conn.execute("SELECT id FROM emails WHERE job_id=? AND status='sent'", (job_id,)).fetchone()
        if r:
            return f"an email for this job was already sent (email #{r['id']})"
        since = (datetime.now() - timedelta(days=window_days)).isoformat(timespec="seconds")
        r = self.conn.execute(
            """SELECT e.id, j.title FROM emails e JOIN jobs j ON j.id=e.job_id
               WHERE e.status='sent' AND e.recipient=? AND lower(j.company)=lower(?) AND e.sent_at>=?""",
            (recipient.lower(), company, since)).fetchone()
        if r:
            return (f"{recipient} was already emailed about '{r['title']}' at {company} in the last "
                    f"{window_days} days (email #{r['id']})")
        return None

    def sent_today(self) -> int:
        today = datetime.now().date().isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM emails WHERE status='sent' AND sent_at>=?",
                                 (today,)).fetchone()[0]

    def job_exists(self, external_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE external_id=?", (external_id,)).fetchone() is not None

    # ---------------------------------------------------------------- search queries
    def due_queries(self, queries: list[str], cooldown_hours: float, limit: int) -> list[str]:
        cutoff = (datetime.now() - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
        last = {r["query"]: r["last_run"] for r in self.conn.execute("SELECT query, last_run FROM search_queries")}
        due = [q for q in queries if not last.get(q) or last[q] < cutoff]
        due.sort(key=lambda q: last.get(q) or "")
        return due[:limit]

    def record_query(self, query: str, results: int, postings: int) -> None:
        self.conn.execute(
            """INSERT INTO search_queries (query, last_run, runs, results, postings) VALUES (?,?,1,?,?)
               ON CONFLICT(query) DO UPDATE SET last_run=excluded.last_run, runs=runs+1,
               results=results+excluded.results, postings=postings+excluded.postings""",
            (query, now(), results, postings))
        self.conn.commit()

    # ---------------------------------------------------------------- events
    def log_event(self, message: str, level: str = "info", job_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO events (ts, level, message, job_id) VALUES (?,?,?,?)",
                          (now(), level, message, job_id))
        self.conn.commit()

    def recent_events(self, after_id: int = 0, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (after_id, limit))][::-1]

    # ---------------------------------------------------------------- dashboard queries
    def list_jobs(self, statuses: list[str] | None = None, limit: int = 500) -> list[dict]:
        q = """SELECT j.id, j.company, j.title, j.location, j.url, j.site, j.source, j.status, j.status_reason,
                      j.experience_req, j.discovered_at, a.score, a.recommendation, a.best_resume,
                      (SELECT COUNT(*) FROM recruiters r WHERE r.job_id=j.id) AS contacts,
                      (SELECT COUNT(*) FROM recruiters r WHERE r.job_id=j.id AND r.email IS NOT NULL) AS emails_found,
                      (SELECT e.status FROM emails e WHERE e.job_id=j.id ORDER BY e.id DESC LIMIT 1) AS email_status
               FROM jobs j LEFT JOIN analyses a ON a.job_id=j.id"""
        args: tuple = ()
        if statuses:
            q += f" WHERE j.status IN ({','.join('?' * len(statuses))})"
            args = tuple(statuses)
        q += " ORDER BY COALESCE(a.score,-1) DESC, j.id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, (*args, limit))]

    def list_emails(self, status: str | None = None) -> list[dict]:
        q = "SELECT e.*, j.company, j.title FROM emails e JOIN jobs j ON j.id=e.job_id"
        args: tuple = ()
        if status:
            q += " WHERE e.status=?"
            args = (status,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY e.id DESC", args)]

    def companies(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            """SELECT j.company, COUNT(*) jobs, MAX(a.score) best_score, MAX(j.company_domain) domain,
                      SUM(CASE WHEN j.status='pending_review' THEN 1 ELSE 0 END) pending,
                      SUM(CASE WHEN j.status='emailed' THEN 1 ELSE 0 END) emailed
               FROM jobs j LEFT JOIN analyses a ON a.job_id=j.id GROUP BY lower(j.company)
               ORDER BY best_score DESC NULLS LAST, jobs DESC""")]

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        d = {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        d["total"] = sum(d.values())
        d["emails_sent"] = self.conn.execute("SELECT COUNT(*) FROM emails WHERE status='sent'").fetchone()[0]
        d["sent_today"] = self.sent_today()
        d["companies"] = self.conn.execute("SELECT COUNT(DISTINCT lower(company)) FROM jobs").fetchone()[0]
        d["queries_run"] = self.conn.execute("SELECT COALESCE(SUM(runs),0) FROM search_queries").fetchone()[0]
        return d
