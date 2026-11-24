"""Headless daily run:  python run.py --daily   (scheduled on GitHub Actions, or a laptop timer).

  1. Gmail draft sync  — which drafts from earlier runs did the user send or delete in Gmail?
  2. Job discovery     — search passes (same rules as the dashboard)
  3. Funded startups   — one funding-news scan
  4. Missing emails    — contact search for good matches without an email
  5. Drafts            — up to the daily limit, put into the user's Gmail **Drafts** folder (never sent)
  6. Summary           — one email to the user's own address

Safety: nothing is ever sent to a recruiter or company here. The user reviews each draft in Gmail and presses
Send. A Gmail draft counts against the daily limit and in the duplicate checks, exactly like a sent email.
"""
from __future__ import annotations

import html
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import db as dbm
from .config import PROJECT_ROOT, Settings

log = logging.getLogger(__name__)

GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
WAITING, SENT, DISCARDED, UNKNOWN = "waiting", "sent", "discarded", "unknown"


# ============================================================== pure helpers (tested)
def decide_draft_state(draft_exists: bool | None, sent_times: list[int] | None) -> str:
    """What happened to a draft we put in Gmail?
    draft_exists: True (still in Drafts) / False (gone) / None (could not check).
    sent_times: internalDate (ms) of SENT messages in the draft's thread; None = could not check."""
    if draft_exists is None:
        return UNKNOWN
    if draft_exists:
        return WAITING
    if sent_times is None:
        return UNKNOWN
    return SENT if sent_times else DISCARDED


def draft_budget(daily_limit: int, sent_today: int, pending_gmail_drafts: int) -> int:
    """New Gmail drafts allowed this run: drafts waiting in Gmail + emails sent today never exceed the limit."""
    return max(0, daily_limit - sent_today - pending_gmail_drafts)


@dataclass
class Item:
    kind: str                 # job | startup
    name: str                 # company / startup
    role: str                 # job title, or "seed · $2M" for a startup
    score: int | None
    recipient: str
    link: str | None = None
    place: str | None = None


@dataclass
class DailyReport:
    date: str
    new_drafts: list[Item] = field(default_factory=list)
    sent_since_last: list[Item] = field(default_factory=list)
    discarded: list[Item] = field(default_factory=list)
    waiting: list[Item] = field(default_factory=list)
    new_jobs: int = 0
    job_matches: int = 0
    new_startups: int = 0
    startup_matches: int = 0
    held_back: int = 0        # drafts written but not put in Gmail (daily limit reached)
    daily_limit: int = 0
    sent_today: int = 0
    errors: list[str] = field(default_factory=list)
    minutes: float = 0.0


def _item_line(i: Item) -> str:
    score = f"[{i.score}] " if i.score is not None else ""
    kind = "Startup" if i.kind == "startup" else "Job"
    where = f" ({i.place})" if i.place else ""
    return f"{score}{kind}: {i.role} — {i.name}{where} → {i.recipient}"


def render_summary(r: DailyReport) -> tuple[str, str, str]:
    """(subject, plain text, html) of the daily summary email to the user."""
    n = len(r.new_drafts)
    subject = (f"Job agent {r.date}: {n} new draft{'s' if n != 1 else ''} ready in Gmail" if n
               else f"Job agent {r.date}: no new drafts today")
    if r.errors:
        subject += " (with warnings)"

    t: list[str] = [f"Daily job agent run — {r.date}", ""]
    if n:
        t += [f"NEW DRAFTS IN YOUR GMAIL ({n}) — review each one and press Send, or delete it:", GMAIL_DRAFTS_URL]
        t += [f"  • {_item_line(i)}" + (f"\n    {i.link}" if i.link else "") for i in r.new_drafts]
    else:
        t.append("No new drafts today.")
    if r.waiting:
        t += ["", f"STILL WAITING IN DRAFTS FROM EARLIER ({len(r.waiting)}):"]
        t += [f"  • {_item_line(i)}" for i in r.waiting]
    if r.sent_since_last:
        t += ["", f"YOU SENT SINCE THE LAST RUN ({len(r.sent_since_last)}):"]
        t += [f"  • {_item_line(i)}" for i in r.sent_since_last]
    if r.discarded:
        t += ["", f"Drafts you deleted (won't be drafted again): {len(r.discarded)}"]
    t += ["", "FOUND THIS RUN:",
          f"  Jobs: {r.new_jobs} new postings, {r.job_matches} good matches",
          f"  Funded startups: {r.new_startups} new, {r.startup_matches} good fits",
          f"  Daily limit: {r.sent_today} sent today + {len(r.waiting) + n} drafts waiting of {r.daily_limit}"]
    if r.held_back:
        t.append(f"  {r.held_back} more draft(s) are ready but held back by the daily limit (next run).")
    if r.errors:
        t += ["", "WARNINGS:"] + [f"  ! {e}" for e in r.errors]
    t += ["", f"Run time: {r.minutes:.0f} min. Open the dashboard on your laptop for full details."]
    text = "\n".join(t)

    e = html.escape

    def rows(items: list[Item], show_link: bool = True) -> str:
        out = []
        for i in items:
            score = f'<b style="color:#0a7d32">{i.score}</b>' if i.score is not None else "—"
            name = (f'<a href="{e(i.link)}">{e(i.name)}</a>' if show_link and i.link
                    and i.link.startswith(("http://", "https://")) else e(i.name))
            out.append(f"<tr><td>{score}</td><td>{'Startup' if i.kind == 'startup' else 'Job'}</td>"
                       f"<td>{e(i.role)}</td><td>{name}{(' · ' + e(i.place)) if i.place else ''}</td>"
                       f"<td>{e(i.recipient)}</td></tr>")
        return ('<table cellpadding="6" style="border-collapse:collapse;font-size:14px">'
                "<tr style='text-align:left;color:#666'><th>Score</th><th>Type</th><th>Role / round</th>"
                "<th>Company</th><th>To</th></tr>" + "".join(out) + "</table>")

    h = [f"<h2 style='margin:0 0 8px'>Daily job agent run — {e(r.date)}</h2>"]
    if n:
        h.append(f"<p><b>{n} new draft{'s' if n != 1 else ''} in your Gmail.</b> Review each one and press Send, "
                 f"or delete it. <a href='{GMAIL_DRAFTS_URL}'>Open Gmail Drafts</a></p>" + rows(r.new_drafts))
    else:
        h.append("<p>No new drafts today.</p>")
    if r.waiting:
        h.append(f"<h3>Still waiting in Drafts from earlier ({len(r.waiting)})</h3>" + rows(r.waiting, False))
    if r.sent_since_last:
        h.append(f"<h3>You sent since the last run ({len(r.sent_since_last)})</h3>" + rows(r.sent_since_last, False))
    if r.discarded:
        h.append(f"<p>Drafts you deleted (won't be drafted again): {len(r.discarded)}</p>")
    h.append("<h3>Found this run</h3><ul>"
             f"<li>Jobs: {r.new_jobs} new postings, {r.job_matches} good matches</li>"
             f"<li>Funded startups: {r.new_startups} new, {r.startup_matches} good fits</li>"
             f"<li>Daily limit: {r.sent_today} sent today + {len(r.waiting) + n} drafts waiting of "
             f"{r.daily_limit}</li>"
             + (f"<li>{r.held_back} more draft(s) held back by the daily limit (next run)</li>" if r.held_back else "")
             + "</ul>")
    if r.errors:
        h.append("<h3 style='color:#b54708'>Warnings</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in r.errors)
                 + "</ul>")
    h.append(f"<p style='color:#666;font-size:12px'>Run time: {r.minutes:.0f} min. Nothing is ever sent to a "
             "recruiter without you pressing Send in Gmail. Open the dashboard on your laptop for full details.</p>")
    body = "<div style='font-family:Arial,sans-serif'>" + "".join(h) + "</div>"
    return subject, text, body


def item_for(db: dbm.Database, table: str, e: dict) -> Item:
    if table == "emails":
        j = db.get_job(e["job_id"]) or {}
        a = db.get_analysis(e["job_id"]) or {}
        return Item("job", j.get("company", "?"), j.get("title", "?"), a.get("score"), e["recipient"], j.get("url"),
                    j.get("location"))
    s = db.get_startup(e["startup_id"]) or {}
    role = " · ".join(x for x in (s.get("funding_round"), s.get("amount")) if x) or "funded startup"
    return Item("startup", s.get("name", "?"), role, s.get("score"), e["recipient"],
                s.get("website") or s.get("source_url"), s.get("hq_city"))


# ============================================================== Gmail draft sync / push
def sync_gmail_drafts(db: dbm.Database, gmail, report: DailyReport, say=print) -> None:
    """Update the tracking DB from Gmail: drafts the user sent → sent; deleted → discarded (never re-drafted)."""
    for table, e in db.gmail_draft_rows():
        exists = gmail.draft_exists(e["gmail_draft_id"])
        times = None
        if exists is False and e.get("gmail_thread_id"):
            times = gmail.thread_sent_times(e["gmail_thread_id"])
        state = decide_draft_state(exists, times)
        it = item_for(db, table, e)
        if state == WAITING:
            report.waiting.append(it)
        elif state == SENT:
            sent_at = datetime.fromtimestamp(max(times) / 1000).isoformat(timespec="seconds")
            db.update_email_row(table, e["id"], status="sent", sent_at=sent_at)
            reason = f"sent from Gmail to {e['recipient']}"
            if table == "emails":
                db.set_job_status(e["job_id"], dbm.EMAILED, reason)
            else:
                db.update_startup(e["startup_id"], status=dbm.EMAILED, status_reason=reason)
            db.log_event(f"SENT (from Gmail): {it.role} @ {it.name} → {e['recipient']}", "good",
                         e.get("job_id"))
            report.sent_since_last.append(it)
        elif state == DISCARDED:
            db.update_email_row(table, e["id"], status=dbm.DISCARDED)
            reason = "Gmail draft deleted by you — not drafted again"
            if table == "emails":
                db.set_job_status(e["job_id"], dbm.SKIPPED, reason)
            else:
                db.update_startup(e["startup_id"], status=dbm.SKIPPED, status_reason=reason)
            report.discarded.append(it)
        else:
            report.waiting.append(it)
            report.errors.append(f"Could not check the Gmail draft for {it.name}; will retry next run.")
    say(f"✔ Gmail drafts: {len(report.sent_since_last)} sent by you, {len(report.discarded)} deleted, "
        f"{len(report.waiting)} still waiting")


def push_drafts(db: dbm.Database, p, sp, gmail, budget: int, report: DailyReport, say=print) -> None:
    """Put the best local drafts into Gmail Drafts, at most `budget`. Re-checks duplicates first."""
    from .pipeline import resolve_attachment
    failures = 0
    for d in db.local_drafts():
        if len(report.new_drafts) >= budget:
            report.held_back += 1
            continue
        if d["table"] == "emails":
            dup = p.duplicate_reason(db.get_job(d["job_id"]), d["recipient"])
        else:
            dup = sp.duplicate_reason(db.get_startup(d["startup_id"]), d["recipient"])
        if dup:
            say(f"  · not drafted ({d['name']}): {dup}")
            continue
        att = resolve_attachment(d["attachment"], p.s.resumes_dir)
        if not att.exists():
            report.errors.append(f"Resume file missing for {d['name']}: {att.name}")
            continue
        try:
            draft_id, mid, tid = gmail.create_draft(d["recipient"], d["subject"], d["body"], att,
                                                   d.get("attachment_filename"))
        except Exception as ex:
            failures += 1
            log.exception("Creating Gmail draft failed")
            report.errors.append(f"Could not create the Gmail draft for {d['name']}: {ex}")
            if failures >= 3:
                break
            continue
        db.update_email_row(d["table"], d["id"], status=dbm.GMAIL_DRAFT, gmail_draft_id=draft_id,
                            gmail_message_id=mid, gmail_thread_id=tid, error=None)
        reason = f"draft in your Gmail → {d['recipient']}"
        if d["table"] == "emails":
            db.set_job_status(d["job_id"], dbm.PENDING, reason)
        else:
            db.update_startup(d["startup_id"], status_reason=reason)
        it = item_for(db, d["table"], d)
        report.new_drafts.append(it)
        say(f"  ✉ Gmail draft: {_item_line(it)}")


# ============================================================== the run
def run_daily(s: Settings) -> int:
    """Returns the process exit code (0 = OK, 1 = run failed, 2 = setup problem)."""
    from .gemini_client import GeminiBusy, GeminiClient
    from .gmail_client import GmailClient
    from .pipeline import Pipeline, QuotaExhausted
    from .resume import load_resume_bundle
    from .search.fetch import PageFetcher
    from .search.searx import SearxClient, ensure_running
    from .startups import StartupPipeline

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    t0 = time.monotonic()
    budget_sec = max(5, s.daily_run_minutes) * 60

    def say(msg: str) -> None:
        print(msg, flush=True)

    def until(frac: float) -> threading.Event:
        """An event that is set when `frac` of the time budget has passed (stops a phase on time)."""
        ev = threading.Event()
        left = t0 + budget_sec * frac - time.monotonic()
        if left <= 0:
            ev.set()
        else:
            timer = threading.Timer(left, ev.set)
            timer.daemon = True
            timer.start()
        return ev

    db = dbm.Database(s.db_path)
    report = DailyReport(date=datetime.now().strftime("%d %b %Y"), daily_limit=s.daily_email_limit)
    say(f"• Daily run started (budget {s.daily_run_minutes} min, daily limit {s.daily_email_limit})")

    gmail = GmailClient(s.gmail_credentials, s.gmail_token)
    g = gmail.check()
    if not g.ready:
        say(f"✖ Gmail not ready: {g.detail}")
        return 2
    missing = gmail.missing_draft_scopes()
    if missing:
        say("✖ The saved Gmail login lacks: " + ", ".join(missing)
            + ". On your laptop run: python run.py --gmail-auth (tick all boxes), then update the GMAIL_TOKEN_JSON "
              "secret.")
        return 2
    say(f"✔ Gmail: {g.account}")

    def finish(code: int) -> int:
        report.minutes = (time.monotonic() - t0) / 60
        report.sent_today = db.sent_today()
        subject, text, body = render_summary(report)
        try:
            gmail.send_summary_to_self(subject, text, body)
            say(f"✔ Summary emailed to {g.account}: {subject}")
        except Exception as ex:
            log.exception("Summary email failed")
            say(f"✖ Could not send the summary email: {ex}")
            code = code or 1
        db.log_event(f"Daily run finished: {len(report.new_drafts)} new Gmail drafts, "
                     f"{len(report.sent_since_last)} sent from Gmail, {len(report.errors)} warnings.")
        db.checkpoint()
        db.close()
        return code

    # 1) sync drafts the user handled in Gmail since the last run
    try:
        sync_gmail_drafts(db, gmail, report, say)
    except Exception as ex:
        log.exception("Draft sync failed")
        report.errors.append(f"Gmail draft sync failed: {ex}")

    gemini = GeminiClient(s.gemini_api_key, s.gemini_model, s.gemini_min_interval_sec,
                          state_file=s.cache_dir / "gemini_quota.json",
                          on_event=lambda m: (db.log_event(m, "muted"), say(f"    {m}")))
    say("• Testing Gemini (key value hidden)…")
    r = gemini.test_connectivity()
    if not r.ok:
        say(f"✖ Gemini API test FAILED: {r.detail}")
        report.errors.append(f"Gemini unavailable — nothing searched today: {r.detail}")
        return finish(1)
    say(f"✔ Gemini OK via {r.model}")
    resume = load_resume_bundle(s.resumes_dir, gemini, s.cache_dir)
    say(f"✔ Resume: {resume.profile.name} · {len(resume.variants)} versions")

    searx = SearxClient(s.searxng_url, s.search_min_interval_sec)
    search_ok, detail = ensure_running(PROJECT_ROOT, searx)
    say(("✔ SearXNG: " if search_ok else "⚠ SearXNG: ") + detail)
    if not search_ok:
        report.errors.append(f"Search engine unavailable — no new search today: {detail}")

    p = Pipeline(s, gemini, resume, db, searx, PageFetcher(s.page_user_agent), gmail,
                 on_event=lambda m, lvl, j: say(f"  [{lvl}] {m}"))
    p.gmail_ready = False            # belt and braces: the pipeline's send() can never send in a daily run
    sp = StartupPipeline(p)
    gemini_out = False

    def phase(name: str, fn) -> None:
        nonlocal gemini_out
        if gemini_out:
            return
        say(f"• {name}")
        try:
            fn()
        except QuotaExhausted as ex:
            gemini_out = True
            report.errors.append(f"Gemini free quota used up during '{name}' — continues next run ({ex})")
        except GeminiBusy as ex:
            report.errors.append(f"Gemini overloaded during '{name}': {ex}")
        except Exception as ex:
            log.exception("Daily phase %s failed", name)
            report.errors.append(f"{name} failed: {type(ex).__name__}: {ex}")

    def jobs() -> None:
        stop = until(0.5)
        while not stop.is_set():
            rep = p.run_pass(stop)
            report.new_jobs += rep.new_jobs
            report.job_matches += rep.ready
            if rep.queries == 0:            # everything searched recently (or every search failed)
                break

    def startups() -> None:
        rep = sp.scan(until(0.75))
        report.new_startups += rep.new_startups
        report.startup_matches += rep.matches

    def missing_emails() -> None:
        stop = until(0.92)
        sp.find_missing_emails(stop)
        p.find_missing_emails(stop)

    def fill_drafts() -> None:
        """Write drafts for good matches that have an email but no draft yet (only as many as can be used)."""
        stop = until(0.97)
        need = draft_budget(s.daily_email_limit, db.sent_today(), db.pending_gmail_drafts()) - len(db.local_drafts())
        no_draft = (None, "replaced")
        cands = [("job", r) for r in db.list_jobs([dbm.PENDING]) if r["emails_found"] and r["email_status"] in no_draft]
        cands += [("startup", r) for r in db.list_startups([dbm.PENDING])
                  if r["emails_found"] and r["email_status"] in no_draft]
        cands.sort(key=lambda x: -(x[1]["score"] or 0))
        for kind, r in cands:
            if need <= 0 or stop.is_set():
                break
            e = p.auto_draft(r["id"]) if kind == "job" else sp.auto_draft(r["id"])
            if e and e["status"] == "draft":
                need -= 1

    if search_ok:
        phase("Job discovery", jobs)
        phase("Funded startups scan", startups)
        phase("Finding missing recruiter emails", missing_emails)
    phase("Writing drafts", fill_drafts)

    budget = draft_budget(s.daily_email_limit, db.sent_today(), db.pending_gmail_drafts())
    say(f"• Putting up to {budget} drafts into Gmail")
    try:
        push_drafts(db, p, sp, gmail, budget, report, say)
    except Exception as ex:
        log.exception("Pushing drafts failed")
        report.errors.append(f"Creating Gmail drafts failed: {ex}")
    return finish(0)
