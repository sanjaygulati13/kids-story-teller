"""Effective runtime settings = app_setting rows overlaid on TB_* env defaults.

The Admin page writes app_setting rows so the operator can switch the generation
model, judge model, and TTS engine/voice/exaggeration WITHOUT editing env or restarting. The
worker, judge and synth read the *effective* settings at job time via `effective()`.

Keys (all optional; missing → env default):
  active_gen_model   str   default TB_LOCAL_GEN_MODEL
  active_judge_model str   default TB_LOCAL_JUDGE_MODEL
  judge_use_opus     bool  default False (when True, judge on TB_OPUS_MODEL)
  gen_use_opus       bool  default False (when True, generate on TB_OPUS_MODEL)
  tts_engine         str   default TB_TTS_ENGINE
  tts_voice          str   default TB_CHATTERBOX_VOICE or TB_KOKORO_VOICE (by engine)
  tts_exaggeration   float default TB_CHATTERBOX_EXAGGERATION
  daily_story_limit  int   default 10
  chatterbox_url     str   default "" → TB_CHATTERBOX_URL (TTS server address)
  kokoro_url         str   default "" → TB_KOKORO_URL

TTS code resolves server addresses through tts_url(), which overlays these on the env defaults.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from tellybear.core.config import Settings, get_settings
from tellybear.db.base import utcnow_iso
from tellybear.db.models import AppSetting

KEYS = (
    "active_gen_model", "active_judge_model", "judge_use_opus", "gen_use_opus",
    "tts_engine", "tts_voice", "tts_exaggeration", "daily_story_limit",
    "chatterbox_url", "kokoro_url",
)
URL_KEYS = ("chatterbox_url", "kokoro_url")
# stored in app_setting but NOT a typed Effective field (managed via voice_pool/set_voice_pool)
_EXTRA_KEYS = ("tts_voice_pool",)
_BOOL_KEYS = {"judge_use_opus", "gen_use_opus"}
_FLOAT_KEYS = {"tts_exaggeration"}
_INT_KEYS = {"daily_story_limit"}


@dataclass
class Effective:
    active_gen_model: str
    active_judge_model: str
    judge_use_opus: bool
    gen_use_opus: bool
    tts_engine: str
    tts_voice: str
    tts_exaggeration: float
    daily_story_limit: int
    chatterbox_url: str     # "" = use TB_CHATTERBOX_URL
    kokoro_url: str         # "" = use TB_KOKORO_URL

    def gen_model(self, s: Settings) -> str:
        return (s.TB_OPUS_MODEL or "opus") if self.gen_use_opus and s.TB_ENABLE_OPUS else self.active_gen_model

    def judge_model(self, s: Settings) -> str:
        return (s.TB_OPUS_MODEL or "opus") if self.judge_use_opus and s.TB_ENABLE_OPUS else self.active_judge_model

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in KEYS}


def _coerce(key: str, raw: str):
    raw = str(raw).strip()
    if raw == "":
        return None
    if key in _BOOL_KEYS:
        return raw.lower() in ("1", "true", "yes", "on")
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if key in _INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return raw


def _raw_rows(db: Session) -> dict[str, str]:
    return {r.key: r.value for r in db.query(AppSetting).populate_existing().all()}


def effective(db: Session, settings: Settings | None = None) -> Effective:
    s = settings or get_settings()
    rows = _raw_rows(db)

    def val(key, default):
        if key in rows:
            c = _coerce(key, rows[key])
            if c is not None:
                return c
        return default

    engine = val("tts_engine", s.TB_TTS_ENGINE)
    default_voice = s.TB_CHATTERBOX_VOICE if engine == "chatterbox" else s.TB_KOKORO_VOICE
    return Effective(
        active_gen_model=val("active_gen_model", s.TB_LOCAL_GEN_MODEL),
        active_judge_model=val("active_judge_model", s.TB_LOCAL_JUDGE_MODEL),
        judge_use_opus=bool(val("judge_use_opus", False)),
        gen_use_opus=bool(val("gen_use_opus", False)),
        tts_engine=engine,
        tts_voice=val("tts_voice", default_voice),
        tts_exaggeration=float(val("tts_exaggeration", s.TB_CHATTERBOX_EXAGGERATION)),
        daily_story_limit=int(val("daily_story_limit", 10)),
        chatterbox_url=str(val("chatterbox_url", "")),
        kokoro_url=str(val("kokoro_url", "")),
    )


# TTS server addresses are read on every engine construction, from both the API and the worker
# process, so cache the DB lookup briefly. A change in the Admin tab reaches the worker within
# _URL_TTL seconds (immediately in the process that saved it).
_URL_TTL = 10.0
_url_cache: dict[str, tuple[str, float]] = {}


def tts_url(engine: str, settings: Settings | None = None) -> str:
    """Effective base URL for a TTS server: the Admin-tab value if set, else the env default."""
    s = settings or get_settings()
    key = f"{engine}_url"
    env_default = s.TB_CHATTERBOX_URL if engine == "chatterbox" else s.TB_KOKORO_URL
    hit = _url_cache.get(key)
    now = time.monotonic()
    if hit is None or now - hit[1] > _URL_TTL:
        try:
            from tellybear.db import base as _base
            db = _base.SessionLocal()
            try:
                row = db.get(AppSetting, key)
                value = (row.value or "").strip() if row else ""
            finally:
                db.close()
        except Exception:
            value = hit[0] if hit else ""  # DB unavailable: keep the last known value
        hit = (value, now)
        _url_cache[key] = hit
    return (hit[0] or env_default or "").rstrip("/")


def set_many(db: Session, updates: dict) -> None:
    """Upsert app_setting rows for any subset of KEYS. Values stored as text."""
    for key, value in updates.items():
        if key not in KEYS or value is None:
            continue
        if key in URL_KEYS:
            value = str(value).strip().rstrip("/")
            _url_cache.pop(key, None)
        text = "true" if (key in _BOOL_KEYS and value is True) else (
            "false" if key in _BOOL_KEYS else str(value))
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=text, updated_at=utcnow_iso()))
        else:
            row.value = text
            row.updated_at = utcnow_iso()
    db.commit()


def voice_pool(db: Session) -> list[str]:
    """Curated predefined-voice allowlist for random/per-child selection.
    Empty list = no restriction (use every voice). Stored as a JSON list in app_setting."""
    row = db.get(AppSetting, "tts_voice_pool")
    if not row or not row.value:
        return []
    try:
        v = json.loads(row.value)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def set_voice_pool(db: Session, voices: list[str]) -> None:
    text = json.dumps([str(v) for v in voices])
    row = db.get(AppSetting, "tts_voice_pool")
    if row is None:
        db.add(AppSetting(key="tts_voice_pool", value=text, updated_at=utcnow_iso()))
    else:
        row.value = text
        row.updated_at = utcnow_iso()
    db.commit()
