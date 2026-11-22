"""Rich terminal dashboard and interactive command loop."""
from __future__ import annotations

import logging
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.markup import escape as esc
from rich.text import Text

from . import db as dbm
from .config import Settings
from .gemini_client import ConnectivityResult
from .gmail_client import GmailStatus
from .pipeline import Pipeline, ScanReport
from .resume import ResumeBundle

log = logging.getLogger(__name__)
console = Console()

COMMANDS = "[bold]NEXT[/] continue · [bold]DETAILS[/] full details · [bold]APPROVE[/] send email · " \
           "[bold]SKIP[/] skip job · [bold]EMAIL[/] add recruiter email · [bold]REGEN[/] rewrite email · " \
           "[bold]STOP[/] quit"


class StopRequested(Exception):
    pass


def score_style(score: int | None) -> str:
    if score is None:
        return "dim"
    return "bold green" if score >= 75 else "yellow" if score >= 55 else "red"


# ------------------------------------------------------------------ startup panels
def show_banner() -> None:
    console.print(Panel.fit("[bold cyan]AI Job Search & Application Assistant[/]\n"
                            "Manual mode · every email requires your explicit APPROVE", box=box.DOUBLE))


def show_gemini(r: ConnectivityResult) -> None:
    if r.ok:
        console.print(f"[green]✔ Gemini API[/]  model=[bold]{r.model}[/]  status=OK  ({r.latency_sec:.1f}s, {r.detail})")
    else:
        console.print(Panel(f"[bold red]Gemini API test FAILED[/]\nModel: {r.model}\nError: {esc(r.detail)}\n\n"
                            "Execution stopped. No other AI provider will be used.", border_style="red"))


def show_resume(b: ResumeBundle, s: Settings) -> None:
    p = b.profile
    t = Table(title="Resume analysis (source of truth)", box=box.SIMPLE_HEAVY, show_header=False)
    t.add_column(style="bold cyan", width=24)
    t.add_column()
    t.add_row("Candidate", esc(f"{p.name} · {p.location} · {p.email}"))
    t.add_row("Experience", esc(f"{s.candidate_experience} (used in emails) · resume text says: "
                            f"'{p.experience_stated_in_resume}'"))
    t.add_row("Employment", esc("\n".join(f"{e.role} @ {e.company} ({e.start} – {e.end})" for e in p.employment_history)))
    t.add_row("Projects", esc("\n".join(x.name for x in p.projects)))
    t.add_row("Education", esc("\n".join(f"{e.degree}, {e.institution} ({e.period})" for e in p.education)))
    t.add_row("Skills", esc(", ".join(p.technical_skills)))
    t.add_row("Deployment", esc("\n".join(f"• {d}" for d in p.deployment_and_production_experience[:5])))
    t.add_row("Resume versions", ", ".join(b.variants))
    console.print(t)


def show_gmail(st: GmailStatus) -> None:
    if st.ready:
        console.print(f"[green]✔ Gmail[/]  authenticated as [bold]{st.account or 'unknown'}[/]")
    else:
        console.print(f"[yellow]⚠ Gmail not ready ({st.state})[/]: {esc(st.detail)}")
        if st.state == "no_credentials":
            console.print("   [dim]See README.md → 'Gmail setup'. You can still review jobs and drafts.[/]")


def show_stats(stats: dict) -> None:
    parts = [f"{k}={v}" for k, v in sorted(stats.items()) if k not in ("total", "emails_sent")]
    console.print(f"[cyan]Database:[/] {stats.get('total', 0)} jobs tracked · {stats.get('emails_sent', 0)} emails sent"
                  + (f" · {', '.join(parts)}" if parts else ""))


# ------------------------------------------------------------------ discovery
def show_scan(rep: ScanReport) -> None:
    if rep.error:
        console.print(f"[red]✖ {esc(rep.company):<14}[/] [dim]{rep.source}[/] error: {esc(rep.error)}")
        return
    style = "bold green" if rep.new_candidates else "white"
    console.print(f"[{style}]● {esc(rep.company):<14}[/] [dim]{rep.source:<28}[/] {rep.total:4} jobs · "
                  f"{rep.india:3} in India · {rep.relevant:2} AI/ML · {rep.already_seen} seen before · "
                  f"[bold]{len(rep.new_candidates)} new to analyse[/]")
    for title, loc, reason in rep.filtered:
        console.print(f"     [dim]✗ {esc(title)} ({esc(loc)}) — {esc(reason)}[/]")


def show_analysis_line(job: dict, a: dict) -> None:
    st = job["status"]
    mark = "[green]→ review[/]" if st == dbm.PENDING else f"[dim]✗ {esc(job['status_reason'] or '')}[/]"
    console.print(f"     [{score_style(a['score'])}]{a['score']:3}/100[/] {esc(job['title'])} ({esc(job['location'] or '')}) "
                  f"· exp: {esc(a['required_experience'][:50])} · {mark}")


# ------------------------------------------------------------------ opportunity card
def _recruiter_table(recs: list[dict], job: dict) -> Table | Text:
    if not recs:
        return Text.assemble(("No recruiter email published on the posting or job page. ", "yellow"),
                             "Nothing is guessed.\nApply on the official page: ", (job["url"] or "-", "underline"),
                             "\nIf you find a recruiter's address yourself, type EMAIL to add it.")
    t = Table(box=box.MINIMAL, show_edge=False)
    for c in ("Name", "Designation", "Email", "Verification", "Source"):
        t.add_column(c)
    for r in recs:
        ver = "[green]verified (on page)[/]" if r["verification"] == "verified_on_page" else "[yellow]user provided[/]"
        t.add_row(esc(r["name"] or "-"), esc(r["designation"] or "-"), esc(r["email"] or "-"), ver, esc(r["source_url"] or "-"))
    return t


def show_opportunity(job: dict, a: dict, recs: list[dict], email: dict | None, idx: str = "") -> None:
    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold cyan")
    head.add_column()
    head.add_row("Company", esc(job["company"]))
    head.add_row("Job title", f"[bold]{esc(job['title'])}[/]")
    head.add_row("Location", esc(job["location"] or "-"))
    head.add_row("Source", esc(f"{job['source']} · {job['url']}"))
    head.add_row("Experience req.", esc(a["required_experience"]))
    head.add_row("Your experience", esc(a["experience_comparison"]))
    head.add_row("Match score", f"[{score_style(a['score'])}]{a['score']}/100[/]  relevance={a['relevance']}  "
                                f"recommendation=[bold]{a['recommendation']}[/]")
    head.add_row("Matching skills", "[green]" + esc(", ".join(a["matching_skills"]) or "-") + "[/]")
    head.add_row("Missing skills", "[red]" + esc(", ".join(a["missing_skills"]) or "-") + "[/]")
    head.add_row("Why", esc(a["reasoning"]))
    head.add_row("Resume to attach", a["best_resume"])
    parts = [head, Text(""), Text("Recruiter / contact", style="bold underline"), _recruiter_table(recs, job)]
    if email:
        att = Path(email["attachment"])
        size = f"{att.stat().st_size // 1024} KB" if att.exists() else "MISSING"
        parts += [Text(""), Panel(
            Group(Text.assemble(("To: ", "bold"), email["recipient"], ("   Recruiter: ", "bold"),
                                email["recruiter_name"] or "(unknown — 'Hiring Team')"),
                  Text.assemble(("Subject: ", "bold"), email["subject"]),
                  Text.assemble(("Attachment: ", "bold"), f"{att.name} ({size}) — {att}"),
                  Text(""), Text(email["body"])),
            title=f"Email draft #{email['id']} · status: {email['status']}", border_style="magenta")]
    console.print(Panel(Group(*parts), title=f"Opportunity {idx} · job #{job['id']}", border_style="cyan"))


def show_details(job: dict, a: dict, recs: list[dict]) -> None:
    console.print(Panel(Text(job["description"] or "(no description)"), title=esc(f"Full job description — {job['title']}"),
                        border_style="blue"))
    console.print(f"[bold]Experience snippet:[/] {esc(job['experience_req'] or '-')}")
    console.print("[bold]Company/team facts from JD:[/] " + esc("; ".join(a["company_highlights"]) or "-"))
    console.print(f"[bold]Discovered:[/] {job['discovered_at']} · [bold]posted/updated:[/] {job['posted_at'] or '-'}"
                  f" · [bold]status:[/] {job['status']} ({esc(job['status_reason'] or '')})")
    for r in recs:
        console.print(esc(f"Contact: {r['email']} · {r['verification']} · source: {r['source_url']}"))


def ask(prompt: str = "Command") -> str:
    try:
        return console.input(f"\n[bold cyan]{prompt}[/] ({COMMANDS})\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        raise StopRequested()


# ------------------------------------------------------------------ review loop
def review(p: Pipeline, job_id: int, idx: str = "") -> None:
    """Interactive review of one opportunity. Raises StopRequested on STOP."""
    job = p.db.get_job(job_id)
    a = p.db.get_analysis(job_id)
    with console.status("Looking for published recruiter contacts…"):
        recs = p.discover_recruiters(job_id)
    email = p.db.latest_email_for_job(job_id)
    if email and email["status"] not in ("draft", "failed"):
        email = None if email["status"] == "skipped" else email
    if email is None and any(r["email"] for r in recs):
        email = _draft(p, job_id, next(r for r in recs if r["email"]))

    while True:
        show_opportunity(job, a, recs, email, idx)
        cmd = ask().upper()
        if cmd in ("NEXT", "N"):
            console.print("[dim]Left as pending — it will be shown again next run.[/]")
            return
        if cmd in ("DETAILS", "D"):
            show_details(job, a, recs)
            ask("Press Enter to return to the opportunity card")
            continue
        if cmd in ("SKIP", "S"):
            p.skip(job_id)
            console.print("[yellow]Skipped. It will not be shown again.[/]")
            return
        if cmd == "STOP":
            raise StopRequested()
        if cmd == "EMAIL":
            addr = console.input("Recruiter email: ").strip()
            name = console.input("Recruiter name (optional): ").strip()
            src = console.input("Where did you find it (URL, optional): ").strip()
            try:
                p.add_user_recruiter(job_id, addr, name, src)
            except ValueError as e:
                console.print(f"[red]{esc(str(e))}[/]")
                continue
            recs = p.db.get_recruiters(job_id)
            email = _draft(p, job_id, next(r for r in recs if r["email"] == addr.lower()))
            continue
        if cmd == "REGEN":
            if not email:
                console.print("[yellow]No recipient yet — use EMAIL first.[/]")
                continue
            p.db.update_email(email["id"], status="skipped")
            rec = next((r for r in recs if r["email"] == email["recipient"]), {"email": email["recipient"],
                                                                                "name": email["recruiter_name"]})
            email = _draft(p, job_id, rec)
            continue
        if cmd == "APPROVE":
            if not email:
                console.print("[yellow]There is no email to approve (no recipient). Use EMAIL to add one, "
                              "or apply via the official page shown above.[/]")
                continue
            with console.status(f"Sending to {email['recipient']}…"):
                res = p.send(email["id"])
            console.print(f"[{'green' if res.ok else 'red'}]{'✔' if res.ok else '✖'} {esc(res.message)}[/]")
            if res.ok:
                return
            email = p.db.get_email(email["id"])
            continue
        console.print("[red]Unknown command.[/] Type NEXT, DETAILS, APPROVE, SKIP, EMAIL, REGEN or STOP.")


def _draft(p: Pipeline, job_id: int, recruiter: dict) -> dict | None:
    with console.status(f"Gemini is writing a personalised email to {recruiter['email']}…"):
        try:
            return p.draft_email(job_id, recruiter)
        except Exception as e:
            console.print(f"[red]Email generation failed: {esc(str(e))}[/]")
            return None
