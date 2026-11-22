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


class AppState:
    pipeline: Pipeline
    worker: Worker
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
            email = email | {"attachment_name": att.name, "attachment_kb": att.stat().st_size // 1024
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

    @app.post("/api/jobs/{job_id}/find-contacts")
    def find_contacts(job_id: int):
        try:
            p.discover_contacts(job_id)
        except QuotaExhausted as e:
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
        except QuotaExhausted as e:
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
