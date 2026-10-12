#!/usr/bin/env python3
"""Run the safety judge red-team eval using the project's .env config.

`tellybear/safety/eval_judge.py` is the verbatim eval harness; it reads TB_JUDGE_URL /
TB_JUDGE_MODEL from the environment (independent of the app's .env). This wrapper fills those
from the project settings (TB_LLM_URL / TB_LOCAL_JUDGE_MODEL / TB_LLM_API_KEY, or the Opus judge when
TB_ENABLE_OPUS=true) so you can just run:

    .venv/bin/python scripts/run_judge_eval.py

Exits non-zero if ANY unsafe red-team story slips through (same contract as eval_judge.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tellybear.core.config import get_settings  # loads .env

s = get_settings()
judge_url = s.TB_LLM_URL
if s.TB_LLM_API_KEY and not os.environ.get("TB_LLM_API_KEY"):
    os.environ["TB_LLM_API_KEY"] = s.TB_LLM_API_KEY
# the judge model actually in use: the Admin-tab choice if one is saved, else the .env default
judge_model = s.TB_LOCAL_JUDGE_MODEL
try:
    from tellybear.core import settings_store
    from tellybear.db.base import SessionLocal
    _db = SessionLocal()
    try:
        judge_model = settings_store.effective(_db, s).active_judge_model or judge_model
    finally:
        _db.close()
except Exception as e:  # no database yet — fall back to the .env default
    print(f"(could not read the Admin judge setting: {e}; using {judge_model})")

# Only fill if unset/empty so an explicit override on the command line still wins.
if not os.environ.get("TB_JUDGE_URL"):
    os.environ["TB_JUDGE_URL"] = judge_url
if not os.environ.get("TB_JUDGE_MODEL"):
    os.environ["TB_JUDGE_MODEL"] = judge_model

print(f"judge: {os.environ['TB_JUDGE_URL']}  model: {os.environ['TB_JUDGE_MODEL']}\n")

# eval_judge.py uses `from safety_check import ...`, so its own dir must be importable.
sys.path.insert(0, str(ROOT / "tellybear" / "safety"))
import eval_judge  # noqa: E402  (env must be set before this import)

eval_judge.main()
