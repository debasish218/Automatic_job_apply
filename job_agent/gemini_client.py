"""Gemini API wrapper: connectivity test, throttling, retries, structured JSON output.

Never logs or prints the API key. Never falls back to another provider.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

RETRYABLE = {429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    """Raised when Gemini cannot produce a result after retries."""


@dataclass
class ConnectivityResult:
    ok: bool
    model: str
    detail: str
    latency_sec: float = 0.0


class GeminiClient:
    def __init__(self, api_key: str, model: str, min_interval_sec: float = 6.0, max_retries: int = 4):
        if not api_key:
            raise GeminiError("GEMINI_API_KEY is missing")
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.min_interval = min_interval_sec
        self.max_retries = max_retries
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.calls = 0

    # ------------------------------------------------------------------ internals
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _generate(self, contents: str, config: types.GenerateContentConfig | None):
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.models.generate_content(model=self.model, contents=contents, config=config)
                self.calls += 1
                return resp
            except errors.APIError as e:
                last_exc = e
                code = getattr(e, "code", None)
                if code not in RETRYABLE or attempt == self.max_retries:
                    break
                backoff = min(60, 5 * 2 ** (attempt - 1))
                log.warning("Gemini %s (attempt %d/%d), retrying in %ss", code, attempt, self.max_retries, backoff)
                time.sleep(backoff)
            except Exception as e:  # network errors etc.
                last_exc = e
                if attempt == self.max_retries:
                    break
                time.sleep(5 * attempt)
        raise GeminiError(f"Gemini request failed ({self.model}): {last_exc}") from last_exc

    # ------------------------------------------------------------------ public API
    def test_connectivity(self) -> ConnectivityResult:
        t0 = time.monotonic()
        try:
            resp = self._generate("Reply with exactly the word: OK", types.GenerateContentConfig(temperature=0))
            text = (resp.text or "").strip()
            ok = "OK" in text.upper()
            return ConnectivityResult(ok, self.model, f"response={text!r}" if ok else f"unexpected response: {text!r}",
                                      time.monotonic() - t0)
        except GeminiError as e:
            return ConnectivityResult(False, self.model, str(e), time.monotonic() - t0)

    def generate_text(self, prompt: str, temperature: float = 0.7) -> str:
        resp = self._generate(prompt, types.GenerateContentConfig(temperature=temperature))
        if not resp.text:
            raise GeminiError("Gemini returned an empty response")
        return resp.text

    def generate_json(self, prompt: str, schema: type[T], temperature: float = 0.2) -> T:
        cfg = types.GenerateContentConfig(
            temperature=temperature, response_mime_type="application/json", response_schema=schema)
        resp = self._generate(prompt, cfg)
        if isinstance(resp.parsed, schema):
            return resp.parsed
        try:
            return schema.model_validate_json(resp.text or "")
        except Exception as e:
            raise GeminiError(f"Gemini returned invalid JSON for {schema.__name__}: {e}") from e
