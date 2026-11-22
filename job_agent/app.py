"""Entry point: web dashboard by default, plus a few maintenance commands."""
from __future__ import annotations

import argparse

from .config import load_settings
from .log import setup_logging


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="AI job search & application assistant (web dashboard)")
    ap.add_argument("--gmail-auth", action="store_true", help="one-time Gmail OAuth login, then exit")
    ap.add_argument("--check-search", action="store_true", help="check SearXNG is running and returns results")
    ap.add_argument("--stats", action="store_true", help="print tracking database statistics")
    args = ap.parse_args(argv)
    s = load_settings()
    setup_logging(s.log_dir)

    if args.gmail_auth:
        from .gmail_client import GmailClient
        st = GmailClient(s.gmail_credentials, s.gmail_token).authenticate()
        print(("✔ Gmail authenticated as " + (st.account or "?")) if st.ready else f"✖ {st.detail}")
        return
    if args.check_search:
        from .config import PROJECT_ROOT
        from .search.searx import SearxClient, ensure_running
        ok, detail = ensure_running(PROJECT_ROOT, SearxClient(s.searxng_url, 0))
        print(("✔ SearXNG: " if ok else "✖ SearXNG: ") + detail)
        return
    if args.stats:
        from .db import Database
        for k, v in sorted(Database(s.db_path).stats().items()):
            print(f"{k:>16}: {v}")
        return
    if not s.gemini_api_key:
        print("✖ GEMINI_API_KEY not found in .env / environment. Execution stopped.")
        raise SystemExit(1)
    from .web.server import serve
    serve(s)
