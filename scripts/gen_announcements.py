#!/usr/bin/env python3
"""Generate each child's "your story is ready" clip for the Tellybear device.

The device plays <data>/announcements/<slug>.mp3 when a new story lands (GET
/api/child/announcement/<slug>); with no clip it simply skips the announcement. Run this once per
child (and again after renaming a child or rotating their player link). Needs the TTS server.

By default the clip uses the configured narrator voice. Pass --voice with the label of a cloned
parent voice (Parent app -> Kids -> parent voices, e.g. "Mom") to have a parent announce it.

  .venv/bin/python scripts/gen_announcements.py                    # every active child
  .venv/bin/python scripts/gen_announcements.py --child Sam --voice Mom
  .venv/bin/python scripts/gen_announcements.py --text "{name}, a new story is here!"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tellybear.core import settings_store
from tellybear.core.config import get_settings
from tellybear.db.base import SessionLocal
from tellybear.db.models import Child, VoiceProfile
from tellybear.pron import inject as pron_inject
from tellybear.tts import synth
from tellybear.tts.engine import get_engine
from tellybear.tts.synth import prepare_text

DEFAULT_TEXT = "{name}, Tellybear has a new story for you. Come and listen!"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--child", help="child name or id (default: every active child)")
    ap.add_argument("--voice", help="label of a cloned parent voice to use (default: narrator voice)")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="announcement text; {name} = child's name")
    a = ap.parse_args()

    s = get_settings()
    out_dir = s.data_dir / "announcements"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        q = db.query(Child)
        if a.child:
            kids = [c for c in q.all() if a.child in (c.name, c.id)]
        else:
            kids = q.filter_by(active=1).all()
        if not kids:
            print("no matching children"); return 2

        eff = settings_store.effective(db, s)
        ref_audio, voice, repo_id = None, eff.tts_voice, None
        engine_name = eff.tts_engine
        if a.voice:
            vp = db.query(VoiceProfile).filter_by(label=a.voice, kind="clone").first()
            if vp is None or not vp.ref_audio_path or not Path(vp.ref_audio_path).is_file():
                print(f"no cloned voice labeled {a.voice!r} with a reference clip"); return 2
            ref_audio = Path(vp.ref_audio_path).read_bytes()
            voice = vp.voice_id or s.TB_CHATTERBOX_VOICE
            engine_name, repo_id = "chatterbox", s.TB_CHATTERBOX_MODEL_CLONE
        engine = get_engine(s, name=engine_name)
        if engine.name == "chatterbox":
            from tellybear.worker import gpu
            gpu.prepare_for_tts(s, repo_id=repo_id)

        failures = 0
        for child in kids:
            try:
                text = prepare_text(a.text.format(name=child.name), keep_tags=False)
                if engine.supports_ipa:
                    text = pron_inject.inject(text, child.household_id, db)
                else:
                    text = synth._apply_respelling(text, child.household_id, db)
                kwargs = dict(voice=voice, fmt="wav", language=child.language or "en")
                if ref_audio is not None:
                    kwargs.update(ref_audio=ref_audio, cfg_weight=s.TB_CHATTERBOX_CFG_WEIGHT_CLONE,
                                  exaggeration=s.TB_CHATTERBOX_EXAGGERATION_CLONE)
                wav = synth._concat_wavs([engine.speak(text, **kwargs)],
                                         leadin_seconds=0.3, gaps=[0.0])
                wav_path = out_dir / f"{child.public_slug}.wav"
                mp3_path = out_dir / f"{child.public_slug}.mp3"
                wav_path.write_bytes(wav)
                ok = synth._transcode_to_mp3(
                    wav_path, mp3_path,
                    atempo=s.TB_TTS_ATEMPO_CLONE if ref_audio is not None else s.TB_TTS_ATEMPO,
                    loudnorm=s.TB_TTS_LOUDNORM, warmth=s.TB_TTS_WARMTH_EQ)
                wav_path.unlink(missing_ok=True)
                if not ok:
                    raise RuntimeError("MP3 transcode failed")
                print(f"ok: {child.name} -> {mp3_path}")
            except Exception as e:
                failures += 1
                print(f"FAIL {child.name}: {e}")
    finally:
        db.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
