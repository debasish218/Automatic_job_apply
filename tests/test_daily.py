"""Daily run: Gmail draft sync, daily-limit budget, duplicate checks with Gmail drafts, summary (no network)."""
from email import message_from_bytes, policy
from types import SimpleNamespace

from job_agent import db as dbm
from job_agent.daily import (DISCARDED, SENT, UNKNOWN, WAITING, DailyReport, Item, decide_draft_state, draft_budget,
                             push_drafts, render_summary, sync_gmail_drafts)
from job_agent.gmail_client import build_summary_message
from job_agent.pipeline import resolve_attachment


def job(d, n, company="Acme", title="ML Engineer", score=80):
    jid, _ = d.upsert_job(dict(source="naukri", external_id=f"x:{n}", company=company, title=title,
                               location="Pune", url=f"https://jobs.example/{n}", description="desc"))
    d.save_analysis(jid, dict(score=score, matching_skills=[], missing_skills=[], required_experience="1-3",
                              experience_comparison="ok", experience_gap=False, relevance="high",
                              recommendation="apply", reasoning="r", best_resume="AI_ML.pdf"))
    d.set_job_status(jid, dbm.PENDING, "match")
    return jid


class FakeGmail:
    def __init__(self, drafts=(), sent_threads=None):
        self.drafts = set(drafts)                 # draft ids still in Gmail
        self.sent_threads = sent_threads or {}    # thread id -> [internalDate ms of SENT messages]
        self.created = []

    def draft_exists(self, draft_id):
        return None if draft_id == "flaky" else draft_id in self.drafts

    def thread_sent_times(self, thread_id):
        return self.sent_threads.get(thread_id, [])

    def create_draft(self, to, subject, body, attachment, filename=None):
        n = len(self.created) + 1
        self.created.append(to)
        return f"d{n}", f"m{n}", f"t{n}"


def test_draft_state_decision():
    assert decide_draft_state(True, None) == WAITING
    assert decide_draft_state(False, [1_700_000_000_000]) == SENT
    assert decide_draft_state(False, []) == DISCARDED
    assert decide_draft_state(None, None) == UNKNOWN          # Gmail error: leave it alone
    assert decide_draft_state(False, None) == UNKNOWN


def test_draft_budget_never_exceeds_daily_limit():
    assert draft_budget(30, 0, 0) == 30
    assert draft_budget(30, 5, 20) == 5
    assert draft_budget(30, 12, 25) == 0
    assert draft_budget(20, 3, 0) == 17


def test_gmail_draft_counts_as_contacted(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    j1, j2 = job(d, 1), job(d, 2, title="CV Engineer")
    eid = d.save_email(j1, "hr@acme.example", None, "s", "b", "/x/AI_ML.pdf")
    assert d.duplicate_send_reason(j1, "hr@acme.example", "Acme", 30) is None       # local draft: not yet
    d.update_email(eid, status=dbm.GMAIL_DRAFT, gmail_draft_id="d1")
    assert "Gmail drafts" in d.duplicate_send_reason(j1, "hr@acme.example", "Acme", 30)
    assert "already emailed" in d.duplicate_send_reason(j2, "hr@acme.example", "Acme", 30)
    assert "Acme" in d.recipient_recently_emailed("hr@acme.example", 30)              # startups see it too
    assert d.pending_gmail_drafts() == 1 and d.sent_today() == 0


def test_sync_marks_sent_and_deleted(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    ids = []
    for n, (draft, thread) in enumerate([("d1", "t1"), ("d2", "t2"), ("d3", "t3"), ("flaky", "t4")], 1):
        jid = job(d, n, company=f"Co{n}")
        eid = d.save_email(jid, f"hr@co{n}.example", None, "s", "b", "/x/AI_ML.pdf")
        d.update_email(eid, status=dbm.GMAIL_DRAFT, gmail_draft_id=draft, gmail_thread_id=thread)
        ids.append((jid, eid))
    sid = d.insert_startup({"name": "NovaMind", "domain": "novamind.example", "source_url": "https://n",
                            "status": dbm.PENDING})
    seid = d.save_startup_email(sid, "careers@novamind.example", None, "s", "b", "/x/AI_ML.pdf", None, None)
    d.update_startup_email(seid, status=dbm.GMAIL_DRAFT, gmail_draft_id="d5", gmail_thread_id="t5")

    gm = FakeGmail(drafts={"d1"}, sent_threads={"t2": [1_763_900_000_000], "t5": [1_763_900_000_000]})
    rep = DailyReport(date="x")
    sync_gmail_drafts(d, gm, rep, say=lambda m: None)

    (j1, e1), (j2, e2), (j3, e3), (j4, e4) = ids
    assert d.get_email(e1)["status"] == dbm.GMAIL_DRAFT                     # still waiting
    assert d.get_email(e2)["status"] == "sent" and d.get_email(e2)["sent_at"]
    assert d.get_job(j2)["status"] == dbm.EMAILED
    assert d.get_email(e3)["status"] == dbm.DISCARDED                       # deleted -> never re-drafted
    assert d.get_job(j3)["status"] == dbm.SKIPPED
    assert d.get_email(e4)["status"] == dbm.GMAIL_DRAFT                     # could not check -> unchanged
    assert d.get_startup_email(seid)["status"] == "sent" and d.get_startup(sid)["status"] == dbm.EMAILED
    assert len(rep.sent_since_last) == 2 and len(rep.discarded) == 1 and len(rep.waiting) == 2
    assert rep.errors                                                        # the flaky check is reported


def test_push_respects_budget_and_duplicates(tmp_path):
    d = dbm.Database(tmp_path / "t.db")
    (tmp_path / "resumes").mkdir()
    (tmp_path / "resumes" / "AI_ML.pdf").write_bytes(b"%PDF-1.4 test")
    j1, j2, j3, j4 = job(d, 1, "Alpha", score=90), job(d, 2, "Alpha", "CV Engineer", 85), \
        job(d, 3, "Beta", score=70), job(d, 4, "Gamma", score=60)
    other_machine = "/home/runner/work/x/resumes/AI_ML.pdf"                 # drafted on another machine
    d.save_email(j1, "hr@alpha.example", None, "s1", "b1", other_machine)
    d.save_email(j2, "hr@alpha.example", None, "s2", "b2", other_machine)   # same recruiter: duplicate
    d.save_email(j3, "jobs@beta.example", None, "s3", "b3", other_machine)
    d.save_email(j4, "hr@gamma.example", None, "s4", "b4", other_machine)

    class P:
        s = SimpleNamespace(resumes_dir=tmp_path / "resumes", duplicate_window_days=30)

        def duplicate_reason(self, j, recipient):
            return d.duplicate_send_reason(j["id"], recipient, j["company"], 30)

    gm, rep = FakeGmail(), DailyReport(date="x")
    push_drafts(d, P(), None, gm, budget=2, report=rep, say=lambda m: None)
    assert gm.created == ["hr@alpha.example", "jobs@beta.example"]           # best scores first, dup skipped
    assert rep.held_back == 1 and len(rep.new_drafts) == 2
    assert d.pending_gmail_drafts() == 2
    assert d.latest_email_for_job(j2)["status"] == "draft"                   # left as a local draft
    assert d.latest_email_for_job(j1)["gmail_thread_id"] == "t1"


def test_resolve_attachment_across_machines(tmp_path):
    (tmp_path / "CV.pdf").write_bytes(b"x")
    assert resolve_attachment("/elsewhere/CV.pdf", tmp_path) == tmp_path / "CV.pdf"
    assert resolve_attachment(str(tmp_path / "CV.pdf"), tmp_path / "none") == tmp_path / "CV.pdf"


def test_summary_rendering():
    rep = DailyReport(date="25 Nov 2026", daily_limit=30, sent_today=2, new_jobs=14, job_matches=3,
                      new_startups=4, startup_matches=1, held_back=1,
                      new_drafts=[Item("job", "Acme <Labs>", "ML Engineer", 82, "hr@acme.example",
                                       "https://jobs.example/1", "Pune"),
                                  Item("startup", "NovaMind", "seed · $2M", 74, "careers@novamind.example")],
                      sent_since_last=[Item("job", "Beta", "CV Engineer", 70, "jobs@beta.example")],
                      errors=["Gemini free quota used up"])
    subject, text, body = render_summary(rep)
    assert subject.startswith("Job agent 25 Nov 2026: 2 new drafts ready in Gmail") and "warnings" in subject
    assert "[82] Job: ML Engineer — Acme <Labs> (Pune) → hr@acme.example" in text
    assert "YOU SENT SINCE THE LAST RUN (1)" in text and "held back" in text and "Gemini free quota" in text
    assert "Acme &lt;Labs&gt;" in body and "<Labs>" not in body                # HTML-escaped
    assert "mail.google.com" in body
    assert render_summary(DailyReport(date="d"))[0] == "Job agent d: no new drafts today"


def test_summary_message_is_multipart_to_self():
    m = build_summary_message("me@example.com", "Subj", "plain", "<b>html</b>")
    parsed = message_from_bytes(m.as_bytes(), policy=policy.default)
    assert parsed["To"] == parsed["From"] == "me@example.com"
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in types and "text/html" in types


def test_run_daily_gemini_down_still_syncs_and_reports(tmp_path, monkeypatch):
    """Gemini unavailable: no search, but drafts are synced and the summary goes to the user only."""
    from job_agent import daily, gemini_client, gmail_client
    from job_agent.config import Settings

    sent = []

    class G(FakeGmail):
        account = "me@example.com"

        def __init__(self, *a):
            super().__init__()

        def check(self):
            return gmail_client.GmailStatus(True, "ready", "ok", self.account)

        def missing_draft_scopes(self):
            return []

        def send_summary_to_self(self, subject, text, html):
            sent.append(subject)
            return "id"

    class Down:
        def __init__(self, *a, **k):
            pass

        def test_connectivity(self):
            return gemini_client.ConnectivityResult(False, "", "all models 503", 0.0, {})

    monkeypatch.setattr(gmail_client, "GmailClient", G)
    monkeypatch.setattr(gemini_client, "GeminiClient", Down)
    s = Settings(gemini_api_key="x", db_path=tmp_path / "jobs.db", cache_dir=tmp_path)
    assert daily.run_daily(s) == 1
    assert sent and "warnings" in sent[0]
