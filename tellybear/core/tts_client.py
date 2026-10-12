"""Thin shared HTTP helpers for the TTS engine adapters.

Engine-specific endpoints (Kokoro /v1/audio/speech + /dev/phonemize, Qwen3-TTS) live in the
adapters under tellybear/tts/, NOT here, so swapping engines never touches core. These are
just transport helpers with the same timeout + single-retry policy as the LLM client.
"""
from __future__ import annotations

import time

import httpx

from tellybear.core.logging import get_logger

log = get_logger("tts_http")

_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class TTSHttpError(RuntimeError):
    pass


def _with_retry(fn):
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            return fn()
        except (httpx.TimeoutException, httpx.TransportError, TTSHttpError) as e:
            last_exc = e
            log.warning("TTS HTTP call failed (attempt %d/2): %s", attempt, e)
            if attempt == 1:
                time.sleep(1.0)
    raise TTSHttpError(f"TTS HTTP call failed after retry: {last_exc}")


def post_bytes(url: str, json_body: dict) -> bytes:
    def _do() -> bytes:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(url, json=json_body)
            if r.status_code >= 500:
                raise TTSHttpError(f"{url} -> {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.content
    return _with_retry(_do)


def post_json(url: str, json_body: dict) -> dict:
    def _do() -> dict:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(url, json=json_body)
            if r.status_code >= 500:
                raise TTSHttpError(f"{url} -> {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
    return _with_retry(_do)


def get_ok(url: str, timeout: float = 5.0) -> bool:
    """Cheap health probe: True if the URL returns < 500 within `timeout`. No retry."""
    try:
        with httpx.Client(timeout=timeout) as c:
            return c.get(url).status_code < 500
    except Exception:
        return False


def get_json(url: str) -> dict:
    def _do() -> dict:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(url)
            if r.status_code >= 500:
                raise TTSHttpError(f"{url} -> {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
    return _with_retry(_do)
