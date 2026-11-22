"""Application entry: startup checks, discovery loop, interactive review."""
from __future__ import annotations

import argparse
import logging
import sys

from . import ui
from .config import load_settings
from .db import PENDING, Database
from .gemini_client import GeminiClient, GeminiError
from .gmail_client import GmailClient
from .pipeline import Pipeline
from .log import setup_logging
from .resume import load_resume_bundle
from .sources import load_sources
from .sources.base import make_session

log = logging.getLogger(__name__)
console = ui.console


def startup_gemini(s) -> GeminiClient:
    if not s.gemini_api_key:
        ui.show_gemini(type("R", (), {"ok": False, "model": s.gemini_model,
                                      "detail": "GEMINI_API_KEY not found in .env / environment"})())
        sys.exit(1)
    console.print("[green]✔ GEMINI_API_KEY found[/] [dim](value hidden)[/]")
    g = GeminiClient(s.gemini_api_key, s.gemini_model, s.gemini_min_interval_sec)
    with console.status(f"Testing Gemini ({s.gemini_model})…"):
        r = g.test_connectivity()
    ui.show_gemini(r)
    if not r.ok:
        sys.exit(1)
    return g


def run_interactive(s) -> None:
    ui.show_banner()
    gemini = startup_gemini(s)
    with console.status("Loading and analysing resume(s)…"):
        try:
            resume = load_resume_bundle(s.resumes_dir, gemini, s.cache_dir)
        except (FileNotFoundError, ValueError, GeminiError) as e:
            console.print(f"[red]Resume analysis failed: {ui.esc(str(e))}[/]")
            sys.exit(1)
    ui.show_resume(resume, s)

    gmail = GmailClient(s.gmail_credentials, s.gmail_token)
    gst = gmail.check()
    ui.show_gmail(gst)

    db = Database(s.db_path)
    ui.show_stats(db.stats())
    session = make_session(s.http_user_agent)
    p = Pipeline(s, gemini, resume, db, session, gmail)
    p.gmail_ready = gst.ready
    sources = load_sources(s.companies_file, session)
    console.print(f"[cyan]Job sources:[/] {len(sources)} official company job boards from companies.yaml")

    try:
        backlog = p.pending_jobs()
        if backlog:
            console.rule(f"[bold]{len(backlog)} opportunities pending from previous runs")
            for i, j in enumerate(backlog, 1):
                ui.review(p, j["id"], f"{i}/{len(backlog)} (backlog)")
        pass_no = 0
        while True:
            pass_no += 1
            console.rule(f"[bold]Discovery pass {pass_no}")
            found = 0
            for src in sources:
                with console.status(f"Fetching {src.company} ({src})…"):
                    rep = p.scan_source(src)
                ui.show_scan(rep)
                for jid in rep.new_candidates:
                    with console.status(f"Gemini is analysing '{db.get_job(jid)['title']}'…"):
                        try:
                            a = p.analyse(jid)
                        except GeminiError as e:
                            console.print(f"     [red]Analysis failed: {ui.esc(str(e))}[/]")
                            continue
                    job = db.get_job(jid)
                    ui.show_analysis_line(job, a)
                    if job["status"] == PENDING:
                        found += 1
                        ui.review(p, jid, f"#{found} (pass {pass_no})")
            console.rule(f"Pass {pass_no} complete · {found} new opportunities · Gemini calls so far: {gemini.calls}")
            ui.show_stats(db.stats())
            while True:
                cmd = ui.ask("Pass finished. NEXT = scan all sources again, STOP = quit").upper()
                if cmd in ("NEXT", "N"):
                    break
                if cmd == "STOP":
                    raise ui.StopRequested()
    except ui.StopRequested:
        console.print("\n[bold yellow]STOP received — discovery and sending halted. Progress is saved.[/]")
    finally:
        ui.show_stats(db.stats())
        db.close()


def check_sources(s) -> None:
    session = make_session(s.http_user_agent)
    for src in load_sources(s.companies_file, session):
        try:
            n = len(src.fetch())
            console.print(f"[green]✔[/] {src.company:<16} {src!r:<30} {n} postings")
        except Exception as e:
            console.print(f"[red]✖[/] {src.company:<16} {src!r:<30} {type(e).__name__}: {ui.esc(str(e))}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Interactive AI job search & application assistant")
    ap.add_argument("--gmail-auth", action="store_true", help="one-time Gmail OAuth login, then exit")
    ap.add_argument("--check-sources", action="store_true", help="validate every board in companies.yaml")
    ap.add_argument("--stats", action="store_true", help="show tracking database statistics")
    args = ap.parse_args(argv)
    s = load_settings()
    setup_logging(s.log_dir)
    if args.gmail_auth:
        g = GmailClient(s.gmail_credentials, s.gmail_token)
        ui.show_gmail(g.authenticate())
        return
    if args.check_sources:
        check_sources(s)
        return
    if args.stats:
        ui.show_stats(Database(s.db_path).stats())
        return
    run_interactive(s)
