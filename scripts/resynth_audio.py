#!/usr/bin/env python3
"""Re-synthesize ready stories, e.g. after changing voices or TTS settings. By default it skips
the stories whose (deterministic) per-story voice would come out the same as the one they already
have; --all re-synthesizes everything.

It does NOT synthesize inline; it flips the chosen stories to `approved` and lets the always-on
worker render them ONE AT A TIME (the safe GPU path). Schedule THIS script for the quiet hours;
the worker drains the queue overnight.

  python scripts/resynth_audio.py            # all ready stories, all children
  python scripts/resynth_audio.py --child <id>
  python scripts/resynth_audio.py --dry-run  # show what would be re-synthesized, change nothing
  python scripts/resynth_audio.py --all       # ignore the n-1/n skip; re-synth everything
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tellybear.core import settings_store
from tellybear.core.config import get_settings
from tellybear.db.base import SessionLocal
from tellybear.db.models import Story, StoryAsset
from tellybear.tts import synth
from tellybear.tts.engine import get_engine


def _current_voice(label: str | None) -> str | None:
    # asset.voice looks like "chatterbox:Alice.wav+multi" → "Alice.wav"
    if not label:
        return None
    return label.split(":", 1)[-1].split("+", 1)[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="ignore the n-1/n same-voice skip")
    args = ap.parse_args()

    s = get_settings()
    db = SessionLocal()
    try:
        eff = settings_store.effective(db, s)
        engine = get_engine(s, name=eff.tts_engine)
        # the voice pool needs the TTS service reachable (model load not required to LIST voices)
        pool = engine.list_voices() if hasattr(engine, "list_voices") else []
        if not pool and engine.name == "chatterbox":
            try:
                from tellybear.worker import gpu
                gpu.start_tts_service(s)
                pool = engine.list_voices()
            except Exception as e:
                print(f"could not reach Chatterbox to list voices: {e}", file=sys.stderr)
        if not pool:
            print("no voice pool available; aborting", file=sys.stderr)
            sys.exit(1)
        n = len(pool)
        mode = (s.TB_TTS_VOICE_MODE or "random").lower()
        print(f"engine={engine.name} voices={n} mode={mode}")

        q = db.query(Story).filter_by(status="ready")
        if args.child:
            q = q.filter_by(child_id=args.child)
        stories = q.all()

        marked = skipped = 0
        for st in stories:
            if mode == "random":
                target = synth._pick(pool, f"narrator:{st.id}")
            elif mode == "per_child":
                target = synth._pick(pool, f"child:{st.child_id}")
            else:
                target = pool[0]
            asset = db.query(StoryAsset).filter_by(story_id=st.id, kind="mp3").first()
            current = _current_voice(asset.voice if asset else None)
            if not args.all and current == target:
                skipped += 1
                continue
            marked += 1
            print(f"  re-synth {st.id}  {current or '?'} -> {target}  {st.title!r}")
            if not args.dry_run:
                st.status = "approved"
                st.stage = "Recording the voice…"
        if not args.dry_run:
            db.commit()
        print(f"\n{'DRY-RUN: would mark' if args.dry_run else 'marked'} {marked} for re-synth, "
              f"skipped {skipped} (already on their target voice). The worker will render them "
              f"one at a time.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
