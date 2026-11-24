"""Local web dashboard (FastAPI). Binds to 127.0.0.1 only."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db as dbm
from ..config import PROJECT_ROOT, Settings
from ..gemini_client import GeminiBusy, GeminiClient
from ..gmail_client import GmailClient
from ..pipeline import Pipeline, QuotaExhausted
from ..resume import load_resume_bundle
from ..search.fetch import PageFetcher
from ..search.searx import SearxClient, ensure_running
from ..startups import StartupPipeline, best_startup_recipient

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Worker:
    """Runs discovery passes in a background thread until stopped (only while the app is open)."""

    def __init__(self, pipeline: Pipeline):
        self.p = pipeline
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "stopped"          # stopped | running | stopping | waiting | paused_quota | error
        self.message = ""
        self.last_report: dict | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.state, self.message = "running", "Starting discovery…"
        self.thread = threading.Thread(target=self._run, name="discovery", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread and self.thread.is_alive():
            self.state = "stopping"
            self.stop_event.set()
            self.p.event("STOP requested — finishing the current step and halting.", "warn")

    def _run(self) -> None:
        self.p.event("Discovery started.", "info")
        try:
            while not self.stop_event.is_set():
                self.state = "running"
                try:
                    rep = self.p.run_pass(self.stop_event)
                except GeminiBusy as e:
                    self.state, self.message = "waiting", "Gemini models overloaded — retrying in 5 min"
                    self.p.event(f"{e}. Waiting 5 minutes, then continuing.", "warn")
                    if self.stop_event.wait(300):
                        break
                    continue
                self.last_report = rep.__dict__
                self.p.event(f"Pass finished: {rep.queries} searches, {rep.postings_seen} postings, "
                             f"{rep.new_jobs} new, {rep.ready} ready for review, {rep.filtered} filtered.")
                if self.stop_event.is_set():
                    break
                if rep.queries == 0:
                    self.state, self.message = "waiting", "All queries searched recently — waiting 15 min"
                    if self.stop_event.wait(900):
                        break
                else:
                    self.stop_event.wait(5)
        except QuotaExhausted as e:
            self.state, self.message = "paused_quota", str(e)
            self.p.event(f"Paused: {e}", "warn")
            return
        except Exception as e:      # keep the server alive; show the error
            log.exception("Worker crashed")
            self.state, self.message = "error", f"{type(e).__name__}: {e}"
            self.p.event(f"Discovery error: {e}", "error")
            return
        finally:
            self.p.status_text = "idle"
        self.state, self.message = "stopped", "Stopped."
        self.p.event("Discovery stopped.", "info")


class EmailHunt:
    """Background deep email search over all matches lacking an email (user-triggered, stoppable)."""

    def __init__(self, pipeline: Pipeline):
        self.p = pipeline
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "idle"
        self.result: str = ""

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.state, self.result = "running", ""
        self.thread = threading.Thread(target=self._run, name="email-hunt", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        try:
            searched, found = self.p.find_missing_emails(self.stop_event)
            self.result = f"{searched} searched, emails found for {found}"
            self.state = "done"
        except (QuotaExhausted, GeminiBusy) as e:
            self.state, self.result = "paused", str(e)
            self.p.event(f"Email search paused: {e}", "warn")
        except Exception as e:
            log.exception("Email hunt crashed")
            self.state, self.result = "error", f"{type(e).__name__}: {e}"
            self.p.event(f"Email search error: {e}", "error")


class StartupScout:
    """One user-triggered, stoppable scan of funding news for recently funded AI startups."""

    def __init__(self, sp: StartupPipeline):
        self.sp = sp
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "idle"             # idle | running | done | paused | error
        self.result = ""
        self.task = "scan"              # scan | emails

    def start(self, task: str = "scan") -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.task = task
        self.state, self.result = "running", ""
        self.thread = threading.Thread(target=self._run, name="startup-scout", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        try:
            if self.task == "emails":
                searched, found = self.sp.find_missing_emails(self.stop_event)
                self.result = f"email search: {searched} startups searched, emails found for {found}"
                self.state = "done"
                return
            rep = self.sp.scan(self.stop_event)
            self.result = (f"{rep.articles} articles read, {rep.new_startups} new startups, "
                           f"{rep.matches} good fits")
            if rep.queries == 0 and not rep.new_startups:
                self.result = "all funding searches ran in the last 24 h — try again later"
            self.state = "done"
        except (QuotaExhausted, GeminiBusy) as e:
            self.state, self.result = "paused", str(e)
            self.sp.p.event(f"Startup scan paused: {e}", "warn")
        except Exception as e:
            log.exception("Startup scan crashed")
            self.state, self.result = "error", f"{type(e).__name__}: {e}"
            self.sp.p.event(f"Startup scan error: {e}", "error")
        finally:
            self.sp.p.status_text = "idle"


class AppState:
    pipeline: Pipeline
    startups: StartupPipeline
    scout: StartupScout
    worker: Worker
    hunt: EmailHunt
    gemini_info: dict
    gmail_info: dict
    searx_info: dict


def bootstrap(s: Settings) -> AppState:
    """Startup checks. Exits the process with a clear message if Gemini is unusable."""
    try:
        sys.stdout.reconfigure(line_buffering=True)    # show startup progress immediately
    except AttributeError:
        pass
    st = AppState()
    db = dbm.Database(s.db_path)
    gemini = GeminiClient(s.gemini_api_key, s.gemini_model, s.gemini_min_interval_sec,
                          state_file=s.cache_dir / "gemini_quota.json",
                          on_event=lambda m: db.log_event(m, "muted"))
    print("• Testing Gemini (key value hidden)…", flush=True)
    r = gemini.test_connectivity()
    for k, v in (r.per_model or {}).items():
        print(f"    {k:24} {v[:100]}")
    if not r.ok:
        print(f"\n✖ Gemini API test FAILED: {r.detail}\nExecution stopped. No other provider will be used.")
        sys.exit(1)
    print(f"✔ Gemini OK via {r.model} ({r.latency_sec:.1f}s)")
    st.gemini_info = {"ok": True, "model": r.model, "detail": r.detail}

    print("• Loading resume analysis…", flush=True)
    resume = load_resume_bundle(s.resumes_dir, gemini, s.cache_dir)
    print(f"✔ Resume: {resume.profile.name} · {len(resume.variants)} versions")

    gmail = GmailClient(s.gmail_credentials, s.gmail_token)
    g = gmail.check()
    st.gmail_info = {"ready": g.ready, "state": g.state, "detail": g.detail, "account": g.account}
    print(("✔ Gmail: " + (g.account or "")) if g.ready else f"⚠ Gmail not ready: {g.detail}")

    searx = SearxClient(s.searxng_url, s.search_min_interval_sec)
    ok, detail = ensure_running(PROJECT_ROOT, searx)
    st.searx_info = {"ok": ok, "detail": detail, "url": s.searxng_url}
    print(("✔ SearXNG: " if ok else "⚠ SearXNG: ") + detail)

    p = Pipeline(s, gemini, resume, db, searx, PageFetcher(s.page_user_agent), gmail)
    p.gmail_ready = g.ready
    st.pipeline = p
    st.worker = Worker(p)
    st.hunt = EmailHunt(p)
    st.startups = StartupPipeline(p)
    st.scout = StartupScout(st.startups)
    db.log_event("Dashboard started.")
    return st


def create_app(st: AppState) -> FastAPI:
    app = FastAPI(title="Job Assistant", docs_url=None, redoc_url=None)
    p, w, db = st.pipeline, st.worker, st.pipeline.db

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # State-changing calls must carry a custom header (blocks cross-site form/JS requests).
        if request.method in ("POST", "PUT", "DELETE") and request.headers.get("x-requested-with") != "job-assistant":
            return JSONResponse({"detail": "missing X-Requested-With header"}, status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        stats = db.stats()
        return {
            "worker": {"state": w.state, "message": w.message, "step": p.status_text, "last_report": w.last_report},
            "email_hunt": {"state": st.hunt.state, "result": st.hunt.result},
            "startup_scan": {"state": st.scout.state, "result": st.scout.result, "task": st.scout.task},
            "startup_stats": db.startup_stats(), "funding_window_days": p.s.funding_window_days,
            "gemini": st.gemini_info | {"models": p.gemini.model_status(), "calls": p.gemini.calls},
            "gmail": st.gmail_info, "searxng": st.searx_info | {"queries_run": p.searx.queries_run},
            "stats": stats, "daily_limit": p.s.daily_email_limit,
            "profile": {"name": p.resume.profile.name, "experience": p.s.candidate_experience,
                        "resumes": list(p.resume.variants)},
        }

    @app.post("/api/discovery/start")
    def start():
        if not st.searx_info["ok"]:
            ok, detail = ensure_running(PROJECT_ROOT, p.searx)
            st.searx_info.update(ok=ok, detail=detail)
            if not ok:
                raise HTTPException(503, f"SearXNG unavailable: {detail}")
        w.start()
        return {"ok": True}

    @app.post("/api/discovery/stop")
    def stop():
        w.stop()
        return {"ok": True}

    @app.post("/api/contacts/find-missing")
    def find_missing():
        st.hunt.start()
        return {"ok": True}

    @app.post("/api/contacts/find-missing/stop")
    def find_missing_stop():
        st.hunt.stop()
        return {"ok": True}

    @app.get("/api/events")
    def events(after: int = 0):
        return db.recent_events(after, 200)

    # ---------------------------------------------------------------- jobs
    VIEWS = {
        "ready": [dbm.PENDING], "matches": [dbm.PENDING], "sent": [dbm.EMAILED],
        "filtered": [dbm.FILTERED, dbm.DUPLICATE, dbm.ERROR], "skipped": [dbm.SKIPPED], "all": None,
    }

    @app.get("/api/jobs")
    def jobs(view: str = "matches"):
        rows = db.list_jobs(VIEWS.get(view, [dbm.PENDING]))
        if view == "ready":
            rows = [r for r in rows if r["email_status"] == "draft"]
        return rows

    def _job_detail(job_id: int) -> dict:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        email = db.latest_email_for_job(job_id)
        if email and email["status"] in ("replaced", "skipped"):
            email = None
        if email:
            att = Path(email["attachment"])
            email = email | {"attachment_name": att.name, "attachment_sent_as": email.get("attachment_filename") or att.name,
                             "attachment_kb": att.stat().st_size // 1024
                             if att.exists() else None}
        return {"job": job, "analysis": db.get_analysis(job_id), "contacts": db.get_recruiters(job_id),
                "email": email, "resumes": list(p.resume.variants)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: int):
        return _job_detail(job_id)

    @app.post("/api/jobs/{job_id}/skip")
    def skip(job_id: int):
        p.skip(job_id)
        return _job_detail(job_id)

    @app.post("/api/jobs/{job_id}/restore")
    def restore(job_id: int):
        p.restore(job_id)
        return _job_detail(job_id)

    @app.post("/api/jobs/{job_id}/reanalyse")
    def reanalyse(job_id: int):
        if not db.get_job(job_id):
            raise HTTPException(404, "job not found")
        try:
            p.analyse(job_id)
        except (QuotaExhausted, GeminiBusy) as e:
            raise HTTPException(429, str(e))
        return _job_detail(job_id)

    @app.post("/api/jobs/{job_id}/find-contacts")
    def find_contacts(job_id: int):
        try:
            p.discover_contacts(job_id, deep=True)
        except (QuotaExhausted, GeminiBusy) as e:
            raise HTTPException(429, str(e))
        return _job_detail(job_id)

    @app.post("/api/jobs/{job_id}/contacts")
    def add_contact(job_id: int, data: dict = Body(...)):
        try:
            p.add_user_recruiter(job_id, data.get("email", ""), data.get("name"), data.get("source"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _job_detail(job_id)

    @app.post("/api/jobs/{job_id}/draft")
    def draft(job_id: int, data: dict = Body(default={})):
        recs = db.get_recruiters(job_id)
        rec = next((r for r in recs if r["id"] == data.get("contact_id")), None) if data.get("contact_id") \
            else p.best_recipient(recs)
        if not rec or not rec.get("email"):
            raise HTTPException(400, "Choose a contact that has an email address (or add one).")
        if not db.get_analysis(job_id):
            raise HTTPException(400, "Job has not been analysed yet.")
        try:
            p.draft_email(job_id, rec, data.get("resume"))
        except (QuotaExhausted, GeminiBusy) as e:
            raise HTTPException(429, str(e))
        return _job_detail(job_id)

    # ---------------------------------------------------------------- emails
    @app.put("/api/emails/{email_id}")
    def edit_email(email_id: int, data: dict = Body(...)):
        try:
            e = p.edit_email(email_id, recipient=data.get("recipient", ""), subject=data.get("subject", ""),
                             body=data.get("body", ""), attachment_key=data.get("resume", ""))
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return _job_detail(e["job_id"])

    @app.post("/api/emails/{email_id}/send")
    def send(email_id: int, data: dict = Body(...)):
        # Explicit approval: the client must echo the exact recipient + subject the user confirmed.
        e = db.get_email(email_id)
        if not e:
            raise HTTPException(404, "email not found")
        if data.get("approved") is not True or data.get("recipient") != e["recipient"] \
                or data.get("subject") != e["subject"]:
            raise HTTPException(409, "Approval does not match the current draft — review it again.")
        res = p.send(email_id)
        if not res.ok:
            raise HTTPException(400, res.message)
        return {"message": res.message, **_job_detail(e["job_id"])}

    @app.get("/api/emails")
    def emails(status: str | None = None):
        return db.list_emails(status)

    @app.get("/api/companies")
    def companies():
        return db.companies()

    # ---------------------------------------------------------------- funded startups
    sp = st.startups
    S_VIEWS = {"matches": [dbm.PENDING], "sent": [dbm.EMAILED], "filtered": [dbm.FILTERED, dbm.NEW],
               "skipped": [dbm.SKIPPED], "all": None}

    def _gemini_call(fn, *a):
        try:
            return fn(*a)
        except (QuotaExhausted, GeminiBusy) as e:
            raise HTTPException(429, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/startups/scan")
    def startup_scan():
        if not st.searx_info["ok"]:
            ok, detail = ensure_running(PROJECT_ROOT, p.searx)
            st.searx_info.update(ok=ok, detail=detail)
            if not ok:
                raise HTTPException(503, f"SearXNG unavailable: {detail}")
        st.scout.start()
        return {"ok": True}

    @app.post("/api/startups/find-emails")
    def startup_find_emails():
        st.scout.start("emails")
        return {"ok": True}

    @app.post("/api/startups/scan/stop")
    def startup_scan_stop():
        st.scout.stop()
        return {"ok": True}

    @app.get("/api/startups")
    def startups(view: str = "matches"):
        return db.list_startups(S_VIEWS.get(view, [dbm.PENDING]))

    def _startup_detail(sid: int) -> dict:
        s_ = db.get_startup(sid)
        if not s_:
            raise HTTPException(404, "startup not found")
        email = db.latest_startup_email(sid)
        if email and email["status"] in ("replaced", "skipped"):
            email = None
        if email:
            att = Path(email["attachment"])
            email = email | {"attachment_name": att.name,
                             "attachment_sent_as": email.get("attachment_filename") or att.name,
                             "attachment_kb": att.stat().st_size // 1024 if att.exists() else None}
        return {"startup": s_, "contacts": db.get_startup_contacts(sid), "email": email,
                "resumes": list(p.resume.variants)}

    @app.get("/api/startups/{sid}")
    def startup_detail(sid: int):
        return _startup_detail(sid)

    @app.post("/api/startups/{sid}/skip")
    def startup_skip(sid: int):
        sp.skip(sid)
        return _startup_detail(sid)

    @app.post("/api/startups/{sid}/restore")
    def startup_restore(sid: int):
        sp.restore(sid)
        return _startup_detail(sid)

    @app.post("/api/startups/{sid}/rescore")
    def startup_rescore(sid: int):
        _startup_detail(sid)
        _gemini_call(sp.score, sid)
        return _startup_detail(sid)

    @app.post("/api/startups/{sid}/find-contacts")
    def startup_find_contacts(sid: int):
        _startup_detail(sid)
        _gemini_call(sp.discover_contacts, sid, True)
        if db.get_startup(sid)["status"] == dbm.PENDING:
            _gemini_call(sp.auto_draft, sid)
        return _startup_detail(sid)

    @app.post("/api/startups/{sid}/contacts")
    def startup_add_contact(sid: int, data: dict = Body(...)):
        _gemini_call(sp.add_user_contact, sid, data.get("email", ""), data.get("name"), data.get("source"))
        return _startup_detail(sid)

    @app.post("/api/startups/{sid}/draft")
    def startup_draft(sid: int, data: dict = Body(default={})):
        recs = db.get_startup_contacts(sid)
        rec = next((r for r in recs if r["id"] == data.get("contact_id")), None) if data.get("contact_id") \
            else best_startup_recipient(recs)
        if not rec or not rec.get("email"):
            raise HTTPException(400, "Choose a contact that has an email address (or add one).")
        _gemini_call(sp.draft_email, sid, rec, data.get("resume"))
        return _startup_detail(sid)

    @app.put("/api/startup-emails/{email_id}")
    def startup_edit_email(email_id: int, data: dict = Body(...)):
        try:
            e = sp.edit_email(email_id, recipient=data.get("recipient", ""), subject=data.get("subject", ""),
                              body=data.get("body", ""), attachment_key=data.get("resume", ""))
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return _startup_detail(e["startup_id"])

    @app.post("/api/startup-emails/{email_id}/send")
    def startup_send(email_id: int, data: dict = Body(...)):
        e = db.get_startup_email(email_id)
        if not e:
            raise HTTPException(404, "email not found")
        if data.get("approved") is not True or data.get("recipient") != e["recipient"] \
                or data.get("subject") != e["subject"]:
            raise HTTPException(409, "Approval does not match the current draft — review it again.")
        res = sp.send(email_id)
        if not res.ok:
            raise HTTPException(400, res.message)
        return {"message": res.message, **_startup_detail(e["startup_id"])}

    return app


def serve(s: Settings) -> None:
    import uvicorn
    import webbrowser

    st = bootstrap(s)
    app = create_app(st)
    url = f"http://127.0.0.1:{s.web_port}"
    print(f"\n➜ Dashboard: {url}   (Ctrl+C here to quit)\n")
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=s.web_port, log_level="warning")
    finally:
        st.worker.stop()
        st.scout.stop()
