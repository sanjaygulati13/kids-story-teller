#!/usr/bin/env python3
"""Backfill story_embedding for existing stories (variety guard).

Embeds every story that doesn't yet have a fingerprint so the guard can compare new
generations against the existing corpus from day one. CPU-only (fastembed/ONNX) — safe to
run while the GPU is busy. Idempotent: skips stories that already have an embedding.

Usage:  .venv/bin/python scripts/backfill_embeddings.py [--all]
        --all   also re-embed stories whose stored vector used a different model
"""
from __future__ import annotations

import json
import sys

from tellybear.core.config import get_settings
from tellybear.db.base import SessionLocal
from tellybear.db.models import Story, StoryEmbedding
from tellybear.variety import embed


def main() -> int:
    redo_all = "--all" in sys.argv
    s = get_settings()
    if not embed.available(s.TB_EMBED_MODEL):
        print("fastembed unavailable — install it in this venv first (pip install fastembed)")
        return 2
    db = SessionLocal()
    try:
        stories = db.query(Story).all()
        done = skipped = failed = 0
        for st in stories:
            existing = db.get(StoryEmbedding, st.id)
            if existing is not None and not (redo_all and existing.model != s.TB_EMBED_MODEL):
                skipped += 1
                continue
            try:
                story_dict = json.loads(st.story_json)
            except Exception:
                failed += 1
                continue
            text = embed.story_text(story_dict)
            if not text.strip():
                skipped += 1
                continue
            vec = embed.embed_text(text, s)
            if vec is None:
                failed += 1
                continue
            embed.store(db, st, vec, s, commit=False)
            done += 1
            if done % 25 == 0:
                db.commit()
                print(f"  …{done} embedded")
        db.commit()
        print(f"done: {done} embedded, {skipped} skipped, {failed} failed "
              f"({len(stories)} total) — model={s.TB_EMBED_MODEL}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
