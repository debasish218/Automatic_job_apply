"""Common types and HTTP handling for public job-board APIs (no scraping of restricted sites)."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


@dataclass
class JobPosting:
    source: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    posted_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4"]):
        br.append("\n")
    lines = (ln.strip() for ln in soup.get_text().splitlines())
    return "\n".join(ln for ln in lines if ln)


def make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class JobSource(ABC):
    """One company's public ATS job board."""

    kind: str = ""
    request_delay_sec = 1.0  # polite gap between requests to the same provider

    def __init__(self, company: str, board: str, session: requests.Session):
        self.company = company
        self.board = board
        self.session = session

    def __repr__(self) -> str:
        return f"{self.kind}:{self.board}"

    def _get_json(self, url: str, **params):
        time.sleep(self.request_delay_sec)
        r = self.session.get(url, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    @abstractmethod
    def fetch(self) -> list[JobPosting]:
        """Return all postings (descriptions may be filled lazily via fetch_description)."""

    def fetch_description(self, posting: JobPosting) -> str:
        return posting.description


def load_sources(companies_file: Path, session: requests.Session) -> list[JobSource]:
    from .ashby import AshbySource
    from .greenhouse import GreenhouseSource
    from .lever import LeverSource
    from .smartrecruiters import SmartRecruitersSource

    kinds = {c.kind: c for c in (GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource)}
    data = yaml.safe_load(companies_file.read_text()) or {}
    sources: list[JobSource] = []
    for entry in data.get("companies", []):
        if not entry.get("enabled", True):
            continue
        cls = kinds.get(entry.get("ats"))
        if not cls:
            log.warning("Unknown ATS %r for %s", entry.get("ats"), entry.get("name"))
            continue
        sources.append(cls(entry["name"], entry["board"], session))
    return sources
