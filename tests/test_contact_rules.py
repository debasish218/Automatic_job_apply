import pytest

from job_agent import db as dbm
from job_agent.config import Settings
from job_agent.pipeline import Pipeline


def make(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    p = Pipeline(Settings(gemini_api_key=None), gemini=None, resume=None, db=d, searx=None, fetcher=None, gmail=None)
    jid, _ = d.upsert_job(dict(source="search:naukri", external_id="u1", company="Acme", title="ML Engineer",
                               location="Pune", url="https://x"))
    d.set_job_status(jid, dbm.PENDING)
    return p, d, jid


def test_manual_email_allowed_when_none_found(tmp_path):
    p, d, jid = make(tmp_path)
    p.add_user_recruiter(jid, "priya@acme.com", "Priya", "linkedin")
    p.add_user_recruiter(jid, "priya.r@acme.com", "Priya", "typo fix")        # can correct with another one
    assert {r["email"] for r in d.get_recruiters(jid)} == {"priya@acme.com", "priya.r@acme.com"}


def test_manual_email_blocked_when_search_found_one(tmp_path):
    p, d, jid = make(tmp_path)
    d.add_recruiter(jid, email="careers@acme.com", verification="verified_on_page", source_url="https://acme.com")
    with pytest.raises(ValueError, match="already has an email"):
        p.add_user_recruiter(jid, "me@acme.com", None, None)


def test_manual_email_blocked_after_sending(tmp_path):
    p, d, jid = make(tmp_path)
    d.set_job_status(jid, dbm.EMAILED)
    with pytest.raises(ValueError, match="already been emailed"):
        p.add_user_recruiter(jid, "x@acme.com", None, None)


def test_recipient_cannot_be_edited(tmp_path):
    p, d, jid = make(tmp_path)
    eid = d.save_email(jid, "careers@acme.com", None, "s", "b", "/nonexistent.pdf")
    with pytest.raises(ValueError, match="can't be changed"):
        p.edit_email(eid, recipient="other@evil.com", subject="s", body="b", attachment_key="AI_ML.pdf")
