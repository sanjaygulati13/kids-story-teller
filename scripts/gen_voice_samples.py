#!/usr/bin/env python3
"""Generate a ~15s audition sample for every predefined TTS voice, so the operator can listen in
the Admin portal and curate which voices to keep (kills the random-voice roulette).

Saves <data>/voice_samples/<voice>.mp3, rendered through the real transcode (atempo + warmth +
loudnorm) so the audition matches what stories actually sound like. Needs Chatterbox running.
Stop the worker first so it doesn't swap the GPU out mid-run.

  .venv/bin/python scripts/gen_voice_samples.py [--force]
"""
from __future__ import annotations

import sys
from pathlib import Path

from tellybear.core import settings_store
from tellybear.core.config import get_settings
from tellybear.db.base import SessionLocal
from tellybear.tts import synth
from tellybear.tts.engine import get_engine

TEXT = ("Once the lanterns dimmed, the little house grew hush and warm. Outside, a soft wind hummed "
        "a sleepy tune through the trees. Close your eyes now, little one — the night is gentle, "
        "and morning will come with the sun.")


def _safe(v: str) -> str:
    return "".join(c for c in v if c.isalnum() or c in "._-")


def main() -> int:
    force = "--force" in sys.argv
    s = get_settings()
    db = SessionLocal()
    eff = settings_store.effective(db, s)
    db.close()
    engine = get_engine(s, name=eff.tts_engine)
    try:
        from tellybear.worker import gpu
        gpu.start_tts_service(s)
        gpu.ensure_tts_loaded(s)   # /restart_server triggers the model load; wait for model-info.loaded
    except Exception as e:
        print("warn: could not ensure TTS model loaded:", e)
    voices = engine.list_voices()
    if not voices:
        print("no voices from engine — is Chatterbox up?"); return 1
    out = Path(s.data_dir) / "voice_samples"
    out.mkdir(parents=True, exist_ok=True)
    done = 0
    for v in voices:
        mp3 = out / f"{_safe(v)}.mp3"
        if mp3.is_file() and not force:
            print("skip (exists):", v); continue
        try:
            wav = engine.speak(TEXT, voice=v, exaggeration=0.45,
                               cfg_weight=s.TB_CHATTERBOX_CFG_WEIGHT, fmt="wav")
        except Exception as e:
            print("FAIL synth:", v, e); continue
        wavp = out / f"{_safe(v)}.wav"
        wavp.write_bytes(wav)
        ok = synth._transcode_to_mp3(wavp, mp3, atempo=s.TB_TTS_ATEMPO,
                                     loudnorm=s.TB_TTS_LOUDNORM, warmth=s.TB_TTS_WARMTH_EQ)
        wavp.unlink(missing_ok=True)
        print("ok:" if ok else "transcode-FAIL:", v)
        done += ok
    print(f"done — {done}/{len(voices)} samples in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
