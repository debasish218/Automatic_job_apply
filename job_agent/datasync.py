"""Laptop <-> private data repo sync for the dashboard (DATA_REPO_SYNC=1).

The daily cloud run commits the database to a private git repository. When the dashboard starts it pulls the
latest copy; when it stops it commits and pushes the user's changes. While a cloud run is in progress (the
workflow commits data/RUNNING) — or if laptop and cloud changes have diverged — the dashboard opens read-only,
because a SQLite file cannot be merged. Sync problems never stop the dashboard; they are shown as a warning.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MARKER = "RUNNING"


@dataclass
class SyncState:
    enabled: bool = False
    read_only: bool = False
    message: str = ""


class DataRepo:
    def __init__(self, path: Path, data_dir: Path):
        self.path = path
        self.data_dir = data_dir
        self.root: Path | None = None

    def _git(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root or self.path, capture_output=True, text=True,
                              timeout=timeout)

    def _ok(self, *args: str, timeout: int = 60) -> str:
        r = self._git(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout.strip()

    def marker(self) -> Path:
        return self.data_dir / MARKER

    def _rel(self) -> str:
        return str(self.data_dir.relative_to(self.root)) if self.root else "data"

    def _commit_local(self, message: str) -> bool:
        self._ok("add", "-A", "--", self._rel())
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return False
        self._ok("commit", "-q", "-m", message)
        return True

    def pull(self) -> SyncState:
        """Bring the laptop copy up to date before the dashboard opens the database."""
        try:
            self.root = Path(self._ok("rev-parse", "--show-toplevel"))
            self._commit_local("Dashboard changes (laptop)")       # leftovers from a session that couldn't push
            self._ok("fetch", "-q", "origin", timeout=90)
            branch = self._ok("rev-parse", "--abbrev-ref", "HEAD")
            ahead, behind = (int(x) for x in self._ok("rev-list", "--left-right", "--count",
                                                      f"HEAD...origin/{branch}").split())
            if ahead and behind:
                return SyncState(True, True,
                                 "Laptop and cloud both changed the database since the last sync, and a database "
                                 "file can't be merged. Opened READ-ONLY. To keep the cloud's version run "
                                 f"`git -C {self.root} reset --hard origin/{branch}`; to keep the laptop's, "
                                 f"`git -C {self.root} push --force`.")
            if behind:
                self._ok("merge", "-q", "--ff-only", f"origin/{branch}")
            if ahead:
                self._ok("push", "-q", "origin", branch, timeout=90)
            if self.marker().exists():
                return SyncState(True, True, "A daily cloud run is in progress right now. Opened READ-ONLY so "
                                             "nothing is overwritten — restart the dashboard after it finishes.")
            return SyncState(True, False, "Data synced with the private repo.")
        except Exception as e:
            log.warning("Data repo pull failed: %s", e)
            return SyncState(True, False, f"Could not sync with the private data repo ({e}). Working on the "
                                          f"local copy; changes are pushed when the dashboard closes.")

    def discard_local(self) -> str:
        """After a read-only session: drop incidental local writes (activity log, quota counters) so they don't
        collide with the cloud's copy at the next sync."""
        try:
            if self.root is None:
                return ""
            self._ok("checkout", "-q", "--", self._rel())
            return "Read-only session: local data left unchanged."
        except Exception as e:
            log.warning("Data repo discard failed: %s", e)
            return f"Could not reset local data ({e})."

    def push(self, message: str = "Dashboard changes (laptop)") -> str:
        """Commit and push the database after the dashboard closed it (call after a WAL checkpoint)."""
        try:
            if self.root is None:
                self.root = Path(self._ok("rev-parse", "--show-toplevel"))
            if not self._commit_local(message):
                return "No data changes to push."
            self._ok("push", "-q", "origin", "HEAD", timeout=90)
            return "Data changes pushed to the private repo."
        except Exception as e:
            log.warning("Data repo push failed: %s", e)
            return f"Could not push data changes ({e}); they are committed locally and retried next start."
