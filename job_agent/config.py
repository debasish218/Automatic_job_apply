"""Central configuration. Secrets come from .env and are never printed."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    gemini_api_key: str | None
    gemini_model: str = "gemini-3.5-flash"
    gemini_min_interval_sec: float = 4.0       # client-side throttle (free-tier friendly)
    min_match_score: int = 50                   # below this, no email is drafted
    daily_email_limit: int = 20                 # hard cap on emails sent per day
    duplicate_window_days: int = 30            # don't email same recipient+company again within N days

    candidate_experience: str = "1 year and 8 months"
    candidate_experience_months: int = 20
    max_required_years: float = 2.0             # jobs whose *minimum* exceeds this are filtered out

    target_roles: list[str] = field(default_factory=lambda: [
        "AI/ML Engineer", "Machine Learning Engineer", "MLOps Engineer",
        "Computer Vision Engineer", "Generative AI Engineer", "Agentic AI Engineer",
        "RAG Engineer", "Applied AI Engineer",
    ])
    preferred_locations: list[str] = field(default_factory=lambda: [
        "Hyderabad", "Bangalore", "Bengaluru", "Pune", "Noida", "Kolkata", "Chennai",
        "Mumbai", "Gurgaon", "Gurugram", "Delhi", "New Delhi", "Ahmedabad",
        "Thiruvananthapuram", "Kochi", "Coimbatore", "India",
    ])

    resumes_dir: Path = PROJECT_ROOT / "resumes"
    db_path: Path = PROJECT_ROOT / "data" / "jobs.db"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    log_dir: Path = PROJECT_ROOT / "logs"
    gmail_credentials: Path = PROJECT_ROOT / "credentials" / "credentials.json"
    gmail_token: Path = PROJECT_ROOT / "credentials" / "token.json"

    http_user_agent: str = "JobSearchAssistant/1.0 (personal, manual-run job search tool)"
    page_user_agent: str = "Mozilla/5.0 (compatible; JobSearchAssistant/1.0; personal job search)"

    searxng_url: str = "http://localhost:8888"
    search_min_interval_sec: float = 20.0       # engines block fast querying; keep this slow
    queries_per_pass: int = 12
    query_cooldown_hours: float = 24.0          # don't repeat the same query within this window
    max_posting_age_days: int = 45
    deep_email_search: bool = True              # extra email-focused searches when none found
    auto_draft: bool = True                     # draft an email automatically when a contact email is found
    web_port: int = 8765


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    s = Settings(gemini_api_key=os.getenv("GEMINI_API_KEY") or None)
    s.gemini_model = os.getenv("GEMINI_MODEL", s.gemini_model)
    s.gemini_min_interval_sec = _float("GEMINI_MIN_INTERVAL_SEC", s.gemini_min_interval_sec)
    s.min_match_score = _int("MIN_MATCH_SCORE", s.min_match_score)
    s.daily_email_limit = _int("DAILY_EMAIL_LIMIT", s.daily_email_limit)
    s.searxng_url = os.getenv("SEARXNG_URL", s.searxng_url)
    s.search_min_interval_sec = _float("SEARCH_MIN_INTERVAL_SEC", s.search_min_interval_sec)
    s.queries_per_pass = _int("QUERIES_PER_PASS", s.queries_per_pass)
    s.web_port = _int("WEB_PORT", s.web_port)
    if os.getenv("JOB_AGENT_DB"):
        s.db_path = Path(os.environ["JOB_AGENT_DB"])
    for d in (s.db_path.parent, s.cache_dir, s.log_dir, s.gmail_credentials.parent):
        d.mkdir(parents=True, exist_ok=True)
    return s
