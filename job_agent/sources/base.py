"""Common job posting type and HTML helpers."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import requests
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
