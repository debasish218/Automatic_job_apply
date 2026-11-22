"""Client for a local SearXNG instance (JSON API)."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    engines: list[str]
    published: str | None = None


class SearxError(RuntimeError):
    pass


class SearxClient:
    def __init__(self, base_url: str, min_interval_sec: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval_sec
        self._last = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.queries_run = 0

    def health(self) -> tuple[bool, str]:
        try:
            r = self.session.get(f"{self.base_url}/search", params={"q": "test", "format": "json"}, timeout=20)
            if r.status_code == 403:
                return False, "SearXNG JSON output is disabled (enable 'json' in settings.yml formats)"
            r.raise_for_status()
            return True, f"{len(r.json().get('results', []))} results for test query"
        except Exception as e:
            return False, f"SearXNG not reachable at {self.base_url}: {e}"

    def search(self, query: str, time_range: str | None = None, pages: int = 1) -> list[SearchResult]:
        out: dict[str, SearchResult] = {}
        for page in range(1, pages + 1):
            with self._lock:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            params = {"q": query, "format": "json", "pageno": page}
            if time_range:
                params["time_range"] = time_range
            try:
                r = self.session.get(f"{self.base_url}/search", params=params, timeout=45)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                raise SearxError(f"search failed for {query!r}: {e}") from e
            self.queries_run += 1
            for x in data.get("results", []):
                url = x.get("url") or ""
                if not url.startswith(("http://", "https://")) or url.startswith("http:///"):
                    continue          # broken redirect URLs (some engines)
                if url not in out:
                    out[url] = SearchResult(url, x.get("title") or "", x.get("content") or "",
                                            x.get("engines") or [], x.get("publishedDate"))
        return list(out.values())


def ensure_running(project_root: Path, client: SearxClient, timeout_sec: int = 60) -> tuple[bool, str]:
    """Start the local SearXNG container on demand (docker compose), then wait until it answers."""
    ok, detail = client.health()
    if ok:
        return ok, detail
    try:
        subprocess.run(["docker", "compose", "up", "-d", "searxng"], cwd=project_root, check=True,
                       capture_output=True, timeout=120)
    except Exception as e:
        return False, f"{detail}. Could not start it with docker compose: {e}"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        time.sleep(2)
        ok, detail = client.health()
        if ok:
            return ok, detail
    return False, detail
