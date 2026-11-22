"""Polite page fetcher: robots.txt respected, per-host blocklist, JSON-LD JobPosting extraction."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Never fetched: terms forbid automated access, or the site sells inferred/guessed contact data.
NEVER_FETCH = (
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "glassdoor.",
    "rocketreach.co", "zoominfo.com", "signalhire.com", "contactout.com", "apollo.io", "lusha.com",
    "leadiq.com", "adapt.io", "hunter.io", "snov.io", "getprospect.com", "kaspr.io", "datanyze.com",
    "clearbit.com", "aeroleads.com", "salesql.com", "seamless.ai", "uplead.com", "rocketreach",
)
DATA_BROKERS = NEVER_FETCH[8:]


@dataclass
class Page:
    url: str
    ok: bool
    status: str                 # "fetched" | "robots_disallowed" | "blocked_site" | "http_<code>" | "error"
    text: str = ""
    job_postings: list[dict] = field(default_factory=list)


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:          # malformed URLs (e.g. stray '[' parsed as IPv6) from page metadata
        return ""


def is_never_fetch(url: str) -> bool:
    h = host_of(url)
    return any(b in h for b in NEVER_FETCH)


def is_data_broker(url: str) -> bool:
    h = host_of(url)
    return any(b in h for b in DATA_BROKERS)


class PageFetcher:
    def __init__(self, user_agent: str, per_host_interval: float = 3.0, max_bytes: int = 3_000_000):
        self.ua = user_agent
        self.per_host_interval = per_host_interval
        self.max_bytes = max_bytes
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml",
                                     "Accept-Language": "en-IN,en;q=0.8"})
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _robots_for(self, url: str):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(f"{base}/robots.txt", timeout=15)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except requests.RequestException:
                rp = None          # unknown -> don't fetch
            self._robots[base] = rp
        return self._robots[base]

    def allowed(self, url: str) -> tuple[bool, str]:
        if is_never_fetch(url):
            return False, "blocked_site"
        rp = self._robots_for(url)
        if rp is None or not rp.can_fetch(self.ua, url):
            return False, "robots_disallowed"
        return True, "ok"

    def fetch(self, url: str) -> Page:
        ok, why = self.allowed(url)
        if not ok:
            return Page(url, False, why)
        host = host_of(url)
        with self._lock:
            wait = self._last_hit.get(host, 0) + self.per_host_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()
        try:
            r = self.session.get(url, timeout=25, stream=True)
            content = r.raw.read(self.max_bytes, decode_content=True)
            r.close()
        except requests.RequestException as e:
            return Page(url, False, "error", str(e))
        if r.status_code != 200:
            return Page(url, False, f"http_{r.status_code}")
        html = content.decode(r.encoding or "utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        postings = []
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or s.get_text() or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for item in _walk_ld(data):
                if item.get("@type") == "JobPosting" or (isinstance(item.get("@type"), list)
                                                          and "JobPosting" in item["@type"]):
                    postings.append(item)
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        text = "\n".join(ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip())
        return Page(url, True, "fetched", text, postings)


def _walk_ld(data):
    if isinstance(data, list):
        for d in data:
            yield from _walk_ld(d)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _walk_ld(data["@graph"])
