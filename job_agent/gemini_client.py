"""Gemini API wrapper: model pools with per-model daily-quota tracking, throttling, retries, structured JSON.

Gemini's free tier gives each model its own small daily request quota. With the user's consent, work is
spread over an ordered pool of Gemini models per task type. Every switch is logged and reported through
`on_event` (shown in the dashboard); nothing is hidden. Only Gemini is ever used — no other provider.
Never logs or prints the API key.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_POOLS: dict[str, list[str]] = {
    # quality matters: resume analysis, job matching, email writing
    "heavy": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-4-hydrogen",
              "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    # extraction / classification
    "light": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.7-flash", "gemini-3.8-flash",
              "gemini-3.5-flash", "gemini-4-hydrogen"],
}


class GeminiError(RuntimeError):
    """Raised when no Gemini model in the pool can produce a result."""


class GeminiBusy(GeminiError):
    """Every model is temporarily overloaded (not out of quota) — retry in a few minutes."""


@dataclass
class ConnectivityResult:
    ok: bool
    model: str
    detail: str
    latency_sec: float = 0.0
    per_model: dict[str, str] | None = None


def _parse_quota(msg: str) -> tuple[str | None, int | None, float | None]:
    """From a 429 message return (quota_model, limit, retry_seconds)."""
    m = re.search(r"limit: (\d+), model: ([\w.-]+)", msg)
    model, limit = (m.group(2), int(m.group(1))) if m else (None, None)
    retry = None
    r = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", msg)
    if r:
        retry = float(r.group(1))
    else:
        r = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", msg)
        if r and any(r.groups()):
            h, mi, s = (float(x) if x else 0 for x in r.groups())
            retry = h * 3600 + mi * 60 + s
    return model, limit, retry


class GeminiClient:
    def __init__(self, api_key: str, model: str, min_interval_sec: float = 4.0, max_retries: int = 3,
                 pools: dict[str, list[str]] | None = None, state_file: Path | None = None,
                 on_event: Callable[[str], None] | None = None):
        if not api_key:
            raise GeminiError("GEMINI_API_KEY is missing")
        # SDK-level retries disabled: this class handles retries/model switching itself. Timeout in ms.
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=45_000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.model = model                      # primary/preferred model (shown at startup)
        self.pools = {k: list(v) for k, v in (pools or DEFAULT_POOLS).items()}
        if model:                                # configured model goes first in the heavy pool
            if model in self.pools["heavy"]:
                self.pools["heavy"].remove(model)
            self.pools["heavy"].insert(0, model)
        self.min_interval = min_interval_sec
        self.max_retries = max_retries
        self.state_file = state_file
        self.on_event = on_event or (lambda m: None)
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.calls = 0
        self.last_model_used: str | None = None
        self.usage_today: dict[str, int] = {}
        self._unavailable: dict[str, dict] = {}   # model -> {"until": iso, "reason": str}
        self._load_state()

    # ------------------------------------------------------------------ quota state
    def _load_state(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self._unavailable = data.get("unavailable", {})
                if data.get("date") == datetime.now().date().isoformat():
                    self.usage_today = data.get("usage", {})
            except (json.JSONDecodeError, OSError):
                pass

    def _save_state(self) -> None:
        if self.state_file:
            self.state_file.write_text(json.dumps({"date": datetime.now().date().isoformat(),
                                                   "usage": self.usage_today,
                                                   "unavailable": self._unavailable}, indent=1))

    def _is_available(self, model: str) -> bool:
        u = self._unavailable.get(model)
        if not u:
            return True
        if datetime.fromisoformat(u["until"]) <= datetime.now():
            self._unavailable.pop(model, None)
            return True
        return False

    def _mark_unavailable(self, model: str, seconds: float, reason: str) -> None:
        until = datetime.now() + timedelta(seconds=seconds)
        self._unavailable[model] = {"until": until.isoformat(timespec="seconds"), "reason": reason}
        self._save_state()

    def model_status(self) -> list[dict]:
        models = list(dict.fromkeys(m for p in self.pools.values() for m in p))
        out = []
        for m in models:
            avail = self._is_available(m)
            u = self._unavailable.get(m, {})
            out.append({"model": m, "available": avail, "used_today": self.usage_today.get(m, 0),
                        "until": None if avail else u.get("until"), "reason": None if avail else u.get("reason")})
        return out

    # ------------------------------------------------------------------ core call
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _call_model(self, model: str, contents: str, config) -> object:
        """Try one model with retries for transient errors. Raises the last APIError/Exception."""
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.models.generate_content(model=model, contents=contents, config=config)
                self.calls += 1
                self.usage_today[model] = self.usage_today.get(model, 0) + 1
                self.last_model_used = model
                self._save_state()
                return resp
            except errors.APIError as e:
                last_exc = e
                code = getattr(e, "code", None)
                if code == 429:
                    qmodel, limit, retry = _parse_quota(str(e))
                    if limit is not None or (retry and retry > 120):   # daily / hard quota -> leave this model
                        raise
                    if attempt < self.max_retries:                     # per-minute limit -> wait and retry
                        time.sleep(min(60, retry or 15 * attempt))
                        continue
                    raise
                if code in (500, 502, 503) and attempt < 2:      # one quick retry, then next model
                    time.sleep(3)
                    continue
                raise
            except Exception as e:   # network
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(4 * attempt)
                    continue
                raise
        raise last_exc  # pragma: no cover

    def _generate(self, contents: str, config, task: str = "heavy"):
        pool = self.pools.get(task) or self.pools["heavy"]
        errors_seen: list[str] = []
        for model in pool:
            if not self._is_available(model):
                continue
            try:
                resp = self._call_model(model, contents, config)
                if errors_seen:
                    self.on_event(f"Gemini: used {model} for {task} task ({'; '.join(errors_seen)})")
                return resp
            except errors.APIError as e:
                code = getattr(e, "code", None)
                msg = str(e)
                if code == 429:
                    qmodel, limit, retry = _parse_quota(msg)
                    secs = 7 * 86400 if limit == 0 else (retry or 3600)
                    why = f"no free quota" if limit == 0 else f"daily quota {limit} reached" if limit else "quota exhausted"
                    for m in {model, qmodel} - {None}:
                        self._mark_unavailable(m, secs, why)
                    # models sharing the same quota bucket (e.g. 3.5-flash -> 3.6-flash quota)
                    errors_seen.append(f"{model}: {why}")
                    self.on_event(f"Gemini model {model} unavailable: {why}; trying next model")
                elif code == 404:
                    self._mark_unavailable(model, 30 * 86400, "model not available to this key")
                    errors_seen.append(f"{model}: not available")
                elif code in (500, 502, 503, 504):
                    self._mark_unavailable(model, 300, f"overloaded ({code})")
                    errors_seen.append(f"{model}: busy")
                    self.on_event(f"Gemini model {model} busy ({code}); trying next model")
                else:   # 400/401/403: key or request problem -> do not mask it
                    raise GeminiError(f"Gemini request failed ({model}): {msg}") from e
            except Exception as e:
                errors_seen.append(f"{model}: {type(e).__name__}")
                log.warning("Gemini %s network error: %s", model, e)
        all_quota = all("quota" in (self._unavailable.get(m, {}).get("reason") or "") for m in pool)
        if not all_quota:
            raise GeminiBusy("All Gemini models are temporarily overloaded: " + "; ".join(errors_seen))
        raise GeminiError("No Gemini model available for this request. "
                          + ("; ".join(errors_seen) if errors_seen else
                             "All models in the pool are over today's free quota.")
                          + " Quotas reset daily (see dashboard › Gemini models).")

    # ------------------------------------------------------------------ public API
    def test_connectivity(self) -> ConnectivityResult:
        """Find at least one working model. Reports every model tried."""
        t0 = time.monotonic()
        per: dict[str, str] = {}
        cfg = types.GenerateContentConfig(temperature=0)
        order = list(dict.fromkeys(self.pools["light"] + self.pools["heavy"]))   # fastest, most reliable first
        for model in order:
            if not self._is_available(model):
                per[model] = "skipped: " + self._unavailable[model]["reason"]
                continue
            try:
                resp = self._call_model(model, "Reply with exactly the word: OK", cfg)
                text = (resp.text or "").strip()
                per[model] = f"OK ({text!r})"
                return ConnectivityResult("OK" in text.upper(), model, f"response={text!r}",
                                          time.monotonic() - t0, per)
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code in (400, 401, 403):
                    return ConnectivityResult(False, model, str(e), time.monotonic() - t0, per)
                if code == 429:
                    _, limit, retry = _parse_quota(str(e))
                    self._mark_unavailable(model, 7 * 86400 if limit == 0 else (retry or 3600),
                                           "no free quota" if limit == 0 else f"daily quota {limit} reached")
                elif code == 404:
                    self._mark_unavailable(model, 30 * 86400, "model not available to this key")
                elif code in (500, 502, 503, 504):
                    self._mark_unavailable(model, 300, f"overloaded ({code})")
                per[model] = f"{code}: {str(e)[:120]}"
            except Exception as e:
                per[model] = f"error: {e}"
        return ConnectivityResult(False, self.model, "No Gemini model responded. " +
                                  " | ".join(f"{k}: {v}" for k, v in per.items()), time.monotonic() - t0, per)

    def generate_text(self, prompt: str, temperature: float = 0.7, task: str = "heavy") -> str:
        resp = self._generate(prompt, types.GenerateContentConfig(temperature=temperature), task)
        if not resp.text:
            raise GeminiError("Gemini returned an empty response")
        return resp.text

    def generate_json(self, prompt: str, schema: type[T], temperature: float = 0.2, task: str = "heavy") -> T:
        cfg = types.GenerateContentConfig(
            temperature=temperature, response_mime_type="application/json", response_schema=schema)
        resp = self._generate(prompt, cfg, task)
        if isinstance(resp.parsed, schema):
            return resp.parsed
        try:
            return schema.model_validate_json(resp.text or "")
        except Exception as e:
            raise GeminiError(f"Gemini returned invalid JSON for {schema.__name__}: {e}") from e
