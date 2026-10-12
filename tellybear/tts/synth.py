"""Synthesis: Story text -> audio assets.

Per-segment synthesis (Chatterbox caps ~1500 chars/~40 s; also enables multi-voice):
  0. engine = effective tts_engine; health-check; FALL BACK to Kokoro if the primary is down.
  1. prepare_text: strip paralinguistic [..] tags (consumed only by Chatterbox-Turbo, which we
     don't run) so they're never read aloud.
  2. pronunciation: Kokoro -> [name](/ipa/) inject; Chatterbox -> household respelling map.
  3. voice + emotion: child.narrator_voice; exaggeration low, lower on the wind-down segment(s).
  4. speak each segment -> concatenate WAVs with a short silence -> WAV master + MP3.
  5. persist story_asset rows (record engine+voice), set story ready, ntfy.

No SSML / invented pause markers: only inserted silence between segments.
"""
from __future__ import annotations

import io
import math
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from sqlalchemy.orm import Session

from tellybear.core.config import Settings, get_settings
from tellybear.core.logging import get_logger
from tellybear.core import notify, settings_store
from tellybear.core.types import StoryJSON
from tellybear.db.models import Child, Pronunciation, Story, StoryAsset, VoiceProfile
from tellybear.pron import inject as pron_inject
from tellybear.tts.engine import TTSEngine, get_engine

log = get_logger("tts")

# paralinguistic tags the generator may emit (the prompt's allowlist); we strip them since neither
# Chatterbox-Multilingual nor Kokoro consume them. Bare lowercase bracket tokens only, so the
# later [Name](/ipa/) injection (capitalized + parens) is never touched.
_TAG_RE = re.compile(r"\[(?:chuckle|sigh|shush|laugh)\]", re.IGNORECASE)
_SILENCE_SECONDS = 0.35


def prepare_text(text: str, keep_tags: bool = False) -> str:
    """Strip paralinguistic tags unless the engine consumes them (Chatterbox-Turbo). For
    Multilingual + Kokoro keep_tags=False so the tags are never read aloud."""
    out = text if keep_tags else _TAG_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", out).strip()


# --------------------------- WAV helpers ---------------------------
def _wav_parts(b: bytes) -> tuple[int, int, int, bytes]:
    """Parse (nchannels, sampwidth, framerate, pcm_bytes) from WAV bytes, tolerating the
    placeholder data-chunk size Kokoro streams (0x7FFFFFFF)."""
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise ValueError("not a WAV")
    nch = sw = fr = 0
    pos = 12
    pcm = b""
    while pos + 8 <= len(b):
        cid, size = struct.unpack("<4sI", b[pos:pos + 8])
        pos += 8
        if cid == b"fmt ":
            fmt = b[pos:pos + size]
            _, nch, fr, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
            sw = bits // 8
        elif cid == b"data":
            actual = len(b) - pos
            real = actual if (size == 0 or size > actual) else size
            pcm = b[pos:pos + real]
            pos += real
            continue
        pos += size + (size & 1)  # chunks are word-aligned
    if not (nch and sw and fr):
        raise ValueError("WAV missing fmt")
    return nch, sw, fr, pcm


def _concat_wavs(chunks: list[bytes], silence_seconds: float = _SILENCE_SECONDS,
                 leadin_seconds: float = 0.0, gaps: list[float] | None = None) -> bytes:
    """Concatenate WAV chunks with silence between them. `gaps`, if given, is the pause to insert
    BEFORE chunk i (len == len(chunks); gaps[0] ignored) — this drives the bedtime pacing, with
    longer pauses between segments and in the wind-down. Falls back to a uniform `silence_seconds`."""
    keep = [(c, _wav_parts(c)) for c in chunks if c]
    if not keep:
        raise RuntimeError("no audio produced")
    parts = [p for _, p in keep]
    nch, sw, fr, _ = parts[0]

    def _sil(secs: float) -> bytes:
        return b"\x00" * int(fr * nch * sw * max(0.0, secs))

    uniform = _sil(silence_seconds)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        if leadin_seconds:  # calm pause before the story starts
            w.writeframes(_sil(leadin_seconds))
        for i, (_, _, _, pcm) in enumerate(parts):
            if i:
                w.writeframes(_sil(gaps[i]) if gaps is not None else uniform)
            w.writeframes(pcm)
    return out.getvalue()


# split a segment into sentences so we can insert a small extra pause between them.
# Keeps the terminal punctuation; tolerant of decimals/ellipses being slightly over-split (a tiny
# extra pause mid-number is inaudible at bedtime).
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=[\"'“‘(A-Z0-9])")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    return parts or [text]


def _group_sentences(text: str, group_size: int = 2, min_chars: int = 25) -> list[str]:
    """Split into ~`group_size`-sentence chunks for synthesis. Single-sentence calls give
    each sentence independent prosody → choppy; groups keep natural intonation, and pacing gaps go
    BETWEEN groups. Any too-short fragment is merged into a neighbor so Chatterbox doesn't
    hallucinate noise on a bare 'Hello?'."""
    sents = _split_sentences(text)
    if group_size <= 1:
        groups = sents
    else:
        groups = [" ".join(sents[i:i + group_size]) for i in range(0, len(sents), group_size)]
    merged: list[str] = []
    for g in groups:
        if merged and len(g) < min_chars:
            merged[-1] = f"{merged[-1]} {g}".strip()
        else:
            merged.append(g)
    if len(merged) >= 2 and len(merged[0]) < min_chars:  # a short first group has no left neighbor
        merged[1] = f"{merged[0]} {merged[1]}".strip()
        merged = merged[1:]
    return merged or [text]


# bedtime-capped emotion → Chatterbox exaggeration (higher = more expressive). Kept gentle so a
# story is lively in the middle but never over-stimulating before sleep.
_EMOTION_EXAGG = {
    "sleepy": 0.25, "soothing": 0.25, "calm": 0.3, "peaceful": 0.3, "tender": 0.42,
    "warm": 0.45, "gentle": 0.45, "loving": 0.45, "kind": 0.45, "content": 0.42, "reassuring": 0.42,
    "curious": 0.55, "hopeful": 0.55, "wondering": 0.55, "surprised": 0.6, "thoughtful": 0.48,
    "relieved": 0.5, "amazed": 0.68, "delighted": 0.68,
    "playful": 0.68, "happy": 0.62, "cheerful": 0.68, "excited": 0.68, "joyful": 0.68, "silly": 0.7,
    "worried": 0.5, "sad": 0.42,
}
_EMOTION_MAX = 0.7  # wider dynamic range than before (0.6) so emotional peaks actually lift — the
# flat/monotone delivery came partly from too-tight a cap. Wind-down still forced low separately.


def _emotion_exaggeration(emotion: str | None, default: float) -> float:
    if not emotion:
        return default
    return min(_EMOTION_MAX, _EMOTION_EXAGG.get(emotion.strip().lower(), default))


def _pick(pool: list[str], seed: str) -> str:
    """Deterministic choice from a voice pool by a seed string (stable across re-synth)."""
    import hashlib
    idx = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]


def _shuffle(pool: list[str], seed: str) -> list[str]:
    """Deterministic ordering of a pool by seed (so re-synth is identical)."""
    import hashlib
    return sorted(pool, key=lambda v: hashlib.sha256(f"{seed}:{v}".encode()).hexdigest())


def assign_voices(story: Story, speakers: list[str], pool: list[str], narrator_voice: str,
                  multivoice: bool, max_voices: int = 3) -> dict[str, str]:
    """Map each speaker → a voice, capped at `max_voices` DISTINCT voices total so a story has a
    narrator + at most (max_voices-1) character voices (not one teller per character). Narrator
    gets `narrator_voice`; characters share a small deterministic palette. Stable per story.
    """
    vmap = {"narrator": narrator_voice}
    if not multivoice or len(pool) < 2 or max_voices < 2:
        return vmap
    others = [v for v in pool if v != narrator_voice] or pool
    k = max(1, min(max_voices - 1, len(others)))            # number of distinct character voices
    palette = _shuffle(others, f"{story.id}:palette")[:k]   # the only voices characters may use
    chars = [sp for sp in dict.fromkeys(speakers) if sp and sp != "narrator"]
    for sp in chars:
        vmap[sp] = palette[_pick_index(f"{story.id}:{sp}", k)]
    return vmap


def _pick_index(seed: str, n: int) -> int:
    import hashlib
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % n


def _wav_duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
        if rate and 0 < frames < 2_000_000_000:
            return frames / float(rate)
    except Exception as e:
        log.warning("could not read WAV duration (%s): %s", path, e)
    return None


def _ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _transcode_to_mp3(wav_path: Path, mp3_path: Path, atempo: float = 1.0, loudnorm: bool = False,
                      warmth: bool = False, ambient: Path | None = None, ambient_mode: str = "off",
                      ambient_db: float = -26.0, tail_seconds: float = 40.0,
                      total_seconds: float | None = None) -> bool:
    ff = _ffmpeg_exe()
    if not ff:
        log.error("no ffmpeg available for MP3 transcode")
        return False
    # atempo < 1.0 slows the narration while PRESERVING PITCH (phase vocoder) — the bedtime
    # slow-down without the resampling distortion Chatterbox's own speed<1 introduces.
    voice_af = [] if abs(atempo - 1.0) < 1e-3 else [f"atempo={atempo:.3f}"]
    post_af: list[str] = []
    if warmth:  # gentle high-shelf cut → softer, less sibilant/harsh, more lullaby-like
        post_af.append("highshelf=g=-3.5:f=4500")
    if loudnorm:
        post_af.append("loudnorm=I=-19:TP=-2:LRA=11")   # even level across chunks
    use_ambient = ambient is not None and ambient.is_file() and ambient_mode in ("under", "tail")
    _STEREO = "aformat=sample_rates=44100:channel_layouts=stereo"
    try:
        if use_ambient and ambient_mode == "under":
            # mix a looped ambient bed UNDER the narration, faded out over the final 20 s
            vol = 10 ** (ambient_db / 20.0)
            bed_af = f"volume={vol:.4f}"
            if total_seconds and total_seconds > 24:
                bed_af += f",afade=t=out:st={total_seconds - 20:.1f}:d=20"
            vchain = f"[0:a]{','.join(voice_af)}[v]" if voice_af else "[0:a]anull[v]"
            mix = "[v][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
            if post_af:
                mix += "," + ",".join(post_af)
            fc = f"{vchain};[1:a]{bed_af}[bed];{mix}[out]"
            cmd = [ff, "-y", "-i", str(wav_path), "-stream_loop", "-1", "-i", str(ambient),
                   "-filter_complex", fc, "-map", "[out]",
                   "-codec:a", "libmp3lame", "-qscale:a", "2", str(mp3_path)]
        elif use_ambient and ambient_mode == "tail":
            # play a calm rain outro AFTER the story ends (gentle fade in/out), concatenated on
            tail = max(8.0, tail_seconds)
            vchain = (f"[0:a]{','.join(voice_af + [_STEREO])}[v]" if voice_af
                      else f"[0:a]{_STEREO}[v]")
            bed = (f"[1:a]atrim=duration={tail:.1f},volume=0.5,afade=t=in:st=0:d=2,"
                   f"afade=t=out:st={tail - 4:.1f}:d=4,{_STEREO}[bed]")
            mix = "[v][bed]concat=n=2:v=0:a=1" + (("," + ",".join(post_af)) if post_af else "") + "[out]"
            fc = f"{vchain};{bed};{mix}"
            cmd = [ff, "-y", "-i", str(wav_path), "-stream_loop", "-1", "-i", str(ambient),
                   "-filter_complex", fc, "-map", "[out]",
                   "-codec:a", "libmp3lame", "-qscale:a", "2", str(mp3_path)]
        else:
            af = voice_af + post_af
            cmd = [ff, "-y", "-i", str(wav_path), *(["-filter:a", ",".join(af)] if af else []),
                   "-codec:a", "libmp3lame", "-qscale:a", "2", str(mp3_path)]
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except Exception as e:
        log.error("ffmpeg transcode failed: %s", e)
        return False


def _ambient_track(name: str | None, settings: Settings) -> Path | None:
    """Resolve <data>/ambient/<name>.mp3 for the optional bedtime ambient bed, or None."""
    if not name or name.lower() == "off":
        return None
    p = settings.data_dir / "ambient" / f"{name}.mp3"
    if not p.is_file():
        log.warning("ambient '%s' requested but %s missing — skipping bed", name, p)
        return None
    return p


# --------------------------- pronunciation (respelling) ---------------------------
def _apply_respelling(text: str, household_id: str, db: Session) -> str:
    """Chatterbox path: replace known surfaces with their phonetic respelling (longest-first,
    whole-word, case-insensitive). No-op for names without a respelling."""
    rows = (db.query(Pronunciation)
            .filter_by(household_id=household_id)
            .all())
    entries = sorted(((r.surface, r.respelling) for r in rows if r.respelling),
                     key=lambda e: len(e[0]), reverse=True)
    out = text
    for surface, respell in entries:
        rgx = re.compile(r"(?<![A-Za-z])" + re.escape(surface) + r"(?![A-Za-z])", re.IGNORECASE)
        out = rgx.sub(respell, out)
    return out


# relation/language synonyms so a character named "मम्मी"/"Mummy"/"Maa" maps to the "Mom" clone, etc.
_CLONE_ALIASES = [
    {"mum", "mom", "mummy", "mommy", "maa", "ma", "माँ", "मां", "मम्मी", "amma", "mother"},
    {"dad", "daddy", "papa", "pop", "pa", "पापा", "डैडी", "बाबा", "appa", "father"},
]


def _norm(x: str) -> str:
    return (x or "").strip().lower()


def _match_clone(speaker: str, clones_by_label: dict) -> tuple | None:
    """Match a story character to a household clone by name (exact, then relation/language alias)."""
    sp = _norm(speaker)
    if sp in clones_by_label:
        return clones_by_label[sp]
    for group in _CLONE_ALIASES:
        if sp in group:
            for lbl, spec in clones_by_label.items():
                if lbl in group:
                    return spec
    return None


def _clones_by_label(db: Session, child: Child, default_voice: str) -> dict:
    """Household clone profiles → {label_lower: (voice_id, ref_audio_bytes)} (for multi-voice casts)."""
    out: dict = {}
    for vp in db.query(VoiceProfile).filter_by(household_id=child.household_id, kind="clone").all():
        if not vp.ref_audio_path:
            continue
        try:
            out[_norm(vp.label)] = (vp.voice_id or default_voice, Path(vp.ref_audio_path).read_bytes())
        except OSError:
            log.warning("clone ref audio missing for voice_profile %s", vp.id)
    return out


def _story_uses_clone(db: Session, child: Child, segments) -> bool:
    """True if this story will be voiced (in whole or part) by a household clone — the narrator is a
    clone, or a character matches one (e.g. मम्मी → Mom). Drives the turbo↔multilingual model choice:
    a clone story wants the multilingual model (holds the parent's accent + Hindi), a predefined-voice
    story wants turbo (English + paralinguistic expression tags)."""
    if child.narrator_voice:
        vp = db.get(VoiceProfile, child.narrator_voice)
        if vp is not None and vp.kind == "clone" and vp.ref_audio_path:
            return True
    clones = _clones_by_label(db, child, "")
    if not clones:
        return False
    return any(_match_clone(sp, clones) is not None
               for sp in {sg.speaker for sg in segments} if sp)


def resolve_voice(db: Session, child: Child, engine: TTSEngine, eff, settings: Settings):
    """Returns (voice_id, ref_audio_bytes|None). The voice must match the ACTIVE engine — a
    Kokoro voice id (e.g. af_bella) for Kokoro, a predefined Chatterbox voice (e.g. Emily.wav)
    for Chatterbox — so a fallback to Kokoro doesn't pass a Chatterbox voice (→ 400)."""
    default_voice = settings.TB_KOKORO_VOICE if engine.name == "kokoro" else eff.tts_voice
    if child.narrator_voice:
        vp = db.get(VoiceProfile, child.narrator_voice)
        if vp is not None:
            if vp.kind == "clone" and vp.ref_audio_path and engine.supports_cloning:
                try:
                    return (vp.voice_id or default_voice, Path(vp.ref_audio_path).read_bytes())
                except OSError:
                    log.warning("clone ref audio missing for voice_profile %s", vp.id)
            if vp.voice_id and (vp.engine == engine.name or not vp.engine):
                return (vp.voice_id, None)
    return (default_voice, None)


# --------------------------- main entrypoint ---------------------------
def synthesize(db: Session, story: Story, settings: Settings | None = None) -> list[StoryAsset]:
    """Produce WAV + MP3 assets for `story`, mark it ready, notify."""
    import time as _t
    s = settings or get_settings()
    eff = settings_store.effective(db, s)
    child = db.get(Child, story.child_id)
    _synth_t0 = _t.monotonic()

    # 0. engine selection. For the GPU engine, FREE the LLM + (re)load the TTS model FIRST,
    # then health-check — otherwise health() (which requires model-info.loaded) always fails
    # and we'd needlessly fall back.
    parsed = StoryJSON.model_validate_json(story.story_json)
    segments = parsed.segments
    n = len(segments)

    engine = get_engine(s, name=eff.tts_engine)
    non_english = (story.language or "en").lower() != "en"
    if engine.name == "chatterbox":
        # pick the Chatterbox model for THIS story: clone-voiced → multilingual (parent accent + hi),
        # predefined-voiced → turbo (English + paralinguistic expression). A mismatch with the resident
        # model triggers a config write + restart inside the hand-off, so the right model is swapped in.
        repo_id = None
        if s.TB_CHATTERBOX_AUTOSWAP_MODEL and engine.supports_cloning:
            uses_clone = _story_uses_clone(db, child, segments)
            uses_multilingual = uses_clone or non_english
            repo_id = s.TB_CHATTERBOX_MODEL_CLONE if uses_multilingual else s.TB_CHATTERBOX_MODEL_DEFAULT
            reason = "clone" if uses_clone else ("non-english/multilingual" if non_english else "predefined")
            log.info("story %s → %s model (%s)", story.id, reason, repo_id)
        try:
            from tellybear.worker import gpu
            gpu.prepare_for_tts(s, repo_id=repo_id)  # unload LLM + restart-to-load the right Chatterbox
        except Exception as e:
            log.warning("gpu hand-off before TTS failed (continuing): %s", e)
    if not engine.health():
        if engine.name != "kokoro":
            log.warning("TTS engine '%s' unavailable; falling back to Kokoro", engine.name)
            engine = get_engine(s, name="kokoro")
        if not engine.health():
            raise RuntimeError("no TTS engine reachable (primary down and Kokoro fallback down)")

    voice, ref_audio = resolve_voice(db, child, engine, eff, s)
    speed = s.TB_KOKORO_SPEED if engine.name == "kokoro" else s.TB_CHATTERBOX_SPEED

    # --- voice variety ---
    # A clone profile (parent voice) reads the whole story; otherwise pick the narrator voice by
    # mode and, with multivoice on, a distinct voice per character.
    cloning = ref_audio is not None
    narrator_voice = voice
    voice_map: dict[str, str] = {}
    multivoice_used = False
    if not cloning:
        try:
            pool = engine.list_voices() if hasattr(engine, "list_voices") else []
        except Exception:
            pool = []
        # restrict to the operator-curated voice pool if one is set — kills the
        # "random voice roulette" where a story could land a thin/robotic voice.
        try:
            curated = settings_store.voice_pool(db)
            if curated and pool:
                pool = [v for v in pool if v in curated] or pool
        except Exception:
            pass
        if non_english and pool:
            female = [v for v in pool if v in s.TB_CHATTERBOX_FEMALE_VOICES]
            pool = female or pool
        if pool:
            mode = (s.TB_TTS_VOICE_MODE or "random").lower()
            if mode == "random":
                narrator_voice = _pick(pool, f"narrator:{story.id}")
            elif mode == "per_child":
                narrator_voice = _pick(pool, f"child:{child.id}")
            else:  # fixed
                narrator_voice = voice if voice in pool else pool[0]
        voice_map = assign_voices(story, [sg.speaker for sg in segments],
                                  pool or [narrator_voice], narrator_voice, s.TB_TTS_MULTIVOICE,
                                  max_voices=s.TB_TTS_MAX_VOICES)

    # per-speaker voice spec (voice_id, ref_audio|None). Characters named like a household clone
    # (e.g. "मम्मी" → Mom) are voiced by that clone — so Dad can narrate while Mom speaks her lines.
    # A clone-narrated story otherwise stays single-voice (no predefined mixing); a predefined-narrated
    # story uses the multivoice palette for the rest.
    clones_by_label = _clones_by_label(db, child, voice) if engine.supports_cloning else {}
    speaker_voice: dict[str, tuple] = {"narrator": (narrator_voice, ref_audio)}
    for sp in dict.fromkeys(sg.speaker for sg in segments):
        if not sp or sp == "narrator":
            continue
        m = _match_clone(sp, clones_by_label)
        if m is not None:
            speaker_voice[sp] = m
        elif not cloning and voice_map.get(sp):
            speaker_voice[sp] = (voice_map[sp], None)
        else:
            speaker_voice[sp] = speaker_voice["narrator"]
    multivoice_used = len({(vid, ref is not None) for vid, ref in speaker_voice.values()}) > 1

    # per-story language for the multilingual model (en|hi|…), default from config
    lang = (getattr(story, "language", "") or s.TB_CHATTERBOX_LANGUAGE or "en")
    # warm-up: discard one generation so the first REAL segment isn't a cold/rushed start
    if s.TB_TTS_WARMUP and engine.name == "chatterbox":
        try:
            engine.speak("Once upon a time, in a calm and cozy place.", voice=narrator_voice,
                         speed=speed, ref_audio=ref_audio, exaggeration=eff.tts_exaggeration, fmt="wav",
                         language=lang)
        except Exception as e:
            log.warning("tts warm-up failed (continuing): %s", e)

    out_dir = s.audio_dir / story.child_id
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{story.id}.wav"
    mp3_path = out_dir / f"{story.id}.mp3"

    # keep paralinguistic tags only for an engine that consumes them (Chatterbox-Turbo)
    keep_tags = engine.name == "chatterbox" and getattr(engine, "supports_tags", lambda: False)()
    # pacing: slow the read with silence, scaled up for the younger band and in the
    # wind-down. We synthesize each SENTENCE separately so a small pause can sit between them.
    # clones read at the parent's natural pace (tighter); predefined voices get the slow bedtime read
    if cloning:
        pace = s.TB_TTS_PACE_CLONE_MULT
    else:
        pace = s.TB_TTS_PACE_EARLY_MULT if (story.age_band or "").lower() == "early" else 1.0
    seg_gap = s.TB_TTS_GAP_SECONDS * pace
    sent_gap = s.TB_TTS_SENTENCE_GAP * pace
    wind_gap = s.TB_TTS_WINDDOWN_GAP * pace
    # 1-4. per-segment synthesis (split into sentences for intra-segment pauses)
    chunks: list[bytes] = []
    gaps: list[float] = []  # pause BEFORE each chunk (gaps[0] ignored by _concat_wavs)
    for i, seg in enumerate(segments):
        text = prepare_text(seg.text, keep_tags=keep_tags)
        if not text:
            continue
        if engine.supports_ipa:
            text = pron_inject.inject(text, child.household_id, db)
        else:
            text = _apply_respelling(text, child.household_id, db)
        # proportional wind-down: the final ~20% of segments (min 2) soften, not "last 2"
        is_winddown = i >= n - max(2, math.ceil(n * 0.2))
        # expressiveness: per-segment emotion → exaggeration, capped for bedtime; the final
        # wind-down segment(s) are always forced soft regardless of the labeled emotion.
        # this segment's voice (per-speaker; may be a different clone, e.g. Mom's line in a Dad story)
        seg_voice, seg_ref = speaker_voice.get(seg.speaker, speaker_voice["narrator"])
        exaggeration = None
        cfg_weight = None
        if engine.supports_emotion:
            exaggeration = s.TB_CHATTERBOX_EXAGGERATION_WINDDOWN if is_winddown \
                else _emotion_exaggeration(seg.emotion, eff.tts_exaggeration)
            if seg_ref is not None:
                # adhere tightly to this parent's reference so their accent holds (less base-model
                # drift), and keep emotion steadier so it doesn't pull the accent around.
                cfg_weight = s.TB_CHATTERBOX_CFG_WEIGHT_CLONE
                exaggeration = min(exaggeration or 1.0, s.TB_CHATTERBOX_EXAGGERATION_CLONE)
            else:
                cfg_weight = s.TB_CHATTERBOX_CFG_WEIGHT
        # the gap that precedes this segment: bigger in the wind-down so the story decelerates
        lead_gap = wind_gap if is_winddown else seg_gap
        # synthesize 2–3-sentence GROUPS (not single sentences) so prosody stays natural;
        # pacing gaps sit between groups (segment gap at a boundary, sentence gap within a segment)
        groups = _group_sentences(text, s.TB_TTS_SENT_GROUP, s.TB_TTS_MIN_CHUNK_CHARS) \
            if sent_gap > 0 else [text]
        for j, group in enumerate(groups):
            chunks.append(engine.speak(group, voice=seg_voice, speed=speed, ref_audio=seg_ref,
                                       exaggeration=exaggeration, cfg_weight=cfg_weight, fmt="wav",
                                       language=lang))
            gaps.append(lead_gap if j == 0 else sent_gap)

    wav_path.write_bytes(_concat_wavs(chunks, leadin_seconds=s.TB_TTS_LEADIN_SILENCE, gaps=gaps))
    # pitch-preserved slow-down for bedtime; clones stay near natural speed
    if cloning:
        atempo = s.TB_TTS_ATEMPO_CLONE
    else:
        atempo = s.TB_TTS_ATEMPO_EARLY if (story.age_band or "").lower() == "early" else s.TB_TTS_ATEMPO
    wav_dur = _wav_duration_seconds(wav_path)
    # the served MP3 is time-stretched, so it is longer than the WAV master by 1/atempo
    duration = (wav_dur / atempo) if (wav_dur and atempo) else wav_dur
    # ambient (rain): OPT-IN per story (story.ambient: off|under|tail), else the global default mode
    amb_mode = (getattr(story, "ambient", "") or s.TB_TTS_AMBIENT or "off").lower()
    amb_track = _ambient_track(s.TB_TTS_AMBIENT_TRACK, s) if amb_mode in ("under", "tail") else None
    if amb_track is not None and amb_mode == "tail" and duration:
        duration += s.TB_TTS_AMBIENT_TAIL_SECONDS  # the outro lengthens the served file
    if not _transcode_to_mp3(wav_path, mp3_path, atempo=atempo, loudnorm=s.TB_TTS_LOUDNORM,
                             warmth=s.TB_TTS_WARMTH_EQ, ambient=amb_track, ambient_mode=amb_mode,
                             ambient_db=s.TB_TTS_AMBIENT_DB, tail_seconds=s.TB_TTS_AMBIENT_TAIL_SECONDS,
                             total_seconds=duration):
        raise RuntimeError("MP3 production failed (no ffmpeg)")
    voice_label = f"{engine.name}:{'clone' if cloning else narrator_voice}" + ("+multi" if multivoice_used else "")

    # a story that already had audio is being RE-synthesized (e.g. a bulk re-synth to pick up new
    # voice/pacing) — a quality refresh of a story the parent already has, not a new arrival. Capture
    # this before we replace the assets so the notify below can stay silent for re-synths.
    prior_render = db.query(StoryAsset).filter_by(story_id=story.id).count() > 0
    db.query(StoryAsset).filter_by(story_id=story.id).delete()
    assets = [
        StoryAsset(story_id=story.id, kind="wav", path=str(wav_path), duration_seconds=wav_dur, voice=voice_label),
        StoryAsset(story_id=story.id, kind="mp3", path=str(mp3_path), duration_seconds=duration, voice=voice_label),
    ]
    db.add_all(assets)
    story.status = "ready"
    story.stage = None
    story.generation_seconds = (story.generation_seconds or 0.0) + (_t.monotonic() - _synth_t0)
    db.commit()

    if not prior_render:  # announce only the first time a story becomes ready; re-synths stay silent
        notify.notify_ready_to_play(child.name, story.title, slug=child.public_slug, settings=s)
    # fully free the GPU for the next generation: STOP Chatterbox (its ~1 GB Torch CUDA context
    # survives /api/unload and would keep an LLM from loading).
    # TB_GPU_CORESIDENT=1: the gen/judge model co-resides with Chatterbox — leave it running.
    if engine.name == "chatterbox" and not s.TB_GPU_CORESIDENT:
        try:
            from tellybear.worker import gpu
            gpu.stop_tts_service(s)
        except Exception as e:
            log.warning("stop TTS service after synth failed: %s", e)
    log.info("synthesized story %s via %s -> %s (%.1fs)", story.id, engine.name, wav_path.name, duration or -1)
    return assets
