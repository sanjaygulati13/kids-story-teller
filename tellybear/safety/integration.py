"""Glue between the pipeline and the verbatim safety module.

safety_check.py is used AS-IS. This module only:
  - builds the judge `llm_generate(system, user, json_mode)` callable bound to the judge
    model (Opus when enabled, else TB_LOCAL_JUDGE_MODEL — a DIFFERENT invocation than the
    generator), and
  - loads the household's banned terms in the [{"term","replacement"}] shape it expects.

The judge prompt the code reads is prompts/judge_system.txt.
"""
from __future__ import annotations

import contextlib

from sqlalchemy.orm import Session

from tellybear.core.config import PROMPTS_DIR, Settings, get_settings
from tellybear.core.llm_client import LLMClient, get_llm_client
from tellybear.db.models import BannedTerm
from tellybear.safety import safety_check
from tellybear.safety.safety_check import SafetyResult, run_safety

JUDGE_PROMPT_PATH = str(PROMPTS_DIR / "judge_system.txt")

# Override the verbatim module's structural word-count bounds from config (without editing
# safety_check.py) so story length is tunable — e.g. shorter early stories for young attention
# spans. These are the gross truncation/over-run guards; the judge handles fine fit.
def _apply_word_bounds(s: Settings | None = None) -> None:
    s = s or get_settings()
    safety_check.BAND_WORDS = {
        "early": (s.TB_WORDS_EARLY_MIN, s.TB_WORDS_EARLY_MAX),
        "older": (s.TB_WORDS_OLDER_MIN, s.TB_WORDS_OLDER_MAX),
    }


_apply_word_bounds()


@contextlib.contextmanager
def relaxed_word_bounds(min_words: int = 200, max_words: int = 6000):
    """Temporarily widen the structural word-count guard for long-form "epic" stories, which
    legitimately run ~2000 words and would otherwise trip the short-story max. Batch judging is
    sequential, so mutating this module global for one story's review is safe."""
    saved = dict(safety_check.BAND_WORDS)
    safety_check.BAND_WORDS = {band: (min_words, max_words) for band in saved}
    try:
        yield
    finally:
        safety_check.BAND_WORDS = saved


def judge_model_name(settings: Settings) -> str:
    if settings.TB_ENABLE_OPUS and settings.TB_OPUS_MODEL:
        return settings.TB_OPUS_MODEL
    return settings.TB_LOCAL_JUDGE_MODEL


def make_judge_llm(client: LLMClient, model: str):
    def llm_generate(system: str, user: str, json_mode: bool = True) -> str:
        return client.generate(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            json_mode=json_mode,
            temperature=0.2,  # judge stays cool/consistent regardless of hot generation
        )
    return llm_generate


def banned_terms_for(db: Session, household_id: str) -> list[dict]:
    rows = db.query(BannedTerm).filter_by(household_id=household_id).all()
    return [{"term": r.term, "replacement": r.replacement} for r in rows]


def review_story(
    story: dict,
    household_id: str,
    db: Session,
    *,
    client: LLMClient | None = None,
    settings: Settings | None = None,
    model: str | None = None,
    double_check: bool = False,
) -> tuple[SafetyResult, dict]:
    """Run the full layered safety check. Returns (SafetyResult, effective_story).
    effective_story has household substitutions applied (use it for TTS on pass).
    `model` overrides the judge model (resolved by the worker from app_setting)."""
    s = settings or get_settings()
    c = client or get_llm_client()
    judge = model or judge_model_name(s)
    llm = make_judge_llm(c, judge)
    banned = banned_terms_for(db, household_id)
    return run_safety(story, banned, llm, JUDGE_PROMPT_PATH, judge_model_name=judge,
                      double_check=double_check)
