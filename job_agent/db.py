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
"""

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
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
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
                company_highlights, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, a["score"], json.dumps(a["matching_skills"]), json.dumps(a["missing_skills"]),
             a["required_experience"], a["experience_comparison"], int(a["experience_gap"]), a["relevance"],
             a["recommendation"], a["reasoning"], a["best_resume"], json.dumps(a.get("company_highlights", [])),
             now()))
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
                      name: str | None = None, designation: str | None = None, linkedin: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO recruiters (job_id, name, designation, email, linkedin, source_url,
               verification, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (job_id, name, designation, email.lower() if email else None, linkedin, source_url, verification, now()))
        self.conn.commit()

    def get_recruiters(self, job_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM recruiters WHERE job_id=? ORDER BY id", (job_id,))]

    # ---------------------------------------------------------------- emails
    def save_email(self, job_id: int, recipient: str, recruiter_name: str | None, subject: str, body: str,
                   attachment: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO emails (job_id, recipient, recruiter_name, subject, body, attachment, status, created_at)
               VALUES (?,?,?,?,?,?,'draft',?)""",
            (job_id, recipient.lower(), recruiter_name, subject, body, attachment, now()))
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

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        d = {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        d["total"] = sum(d.values())
        d["emails_sent"] = self.conn.execute("SELECT COUNT(*) FROM emails WHERE status='sent'").fetchone()[0]
        return d
