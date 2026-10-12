"""Chatterbox (Multilingual) adapter — the primary engine.

GPU, MIT, expressive + zero-shot voice cloning. Served by its own FastAPI wrapper at
the Chatterbox URL (Admin tab, else TB_CHATTERBOX_URL). We use the NATIVE `/tts` endpoint (not the OpenAI `/v1/audio/speech`)
because only `/tts` exposes the `exaggeration` emotion dial and clone/predefined voice modes
(verified against the live server's OpenAPI):

  POST /tts {text, voice_mode:"predefined"|"clone", predefined_voice_id, reference_audio_filename,
             output_format:"wav"|"mp3", exaggeration, cfg_weight, temperature, split_text,
             chunk_size, language, stream:false} -> audio bytes

- supports_ipa=False (phonemize None); pronunciation via respelling / cloned voice.
- supports_cloning=True, supports_emotion=True, max_chars≈1500 (caller chunks per segment).
- Output carries an imperceptible PerTh watermark — do not strip it.
- Cloning needs the reference clip uploaded to the server first; v1 ships predefined-voice
  synthesis and logs a clear note if a clone is requested without an uploaded reference.
"""
from __future__ import annotations

from tellybear.core.config import Settings, get_settings
from tellybear.core import settings_store, tts_client
from tellybear.core.logging import get_logger

log = get_logger("chatterbox")


class ChatterboxEngine:
    name = "chatterbox"
    supports_ipa = False
    supports_cloning = True
    supports_emotion = True
    max_chars = 1500

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.base = settings_store.tts_url("chatterbox", self.s)

    def speak(self, text: str, *, voice: str, speed: float = 1.0,
              ref_audio: bytes | None = None, exaggeration: float | None = None,
              emotion: str | None = None, fmt: str = "wav", cfg_weight: float | None = None,
              language: str | None = None) -> bytes:
        body: dict = {
            "text": text,
            "output_format": fmt,
            # We already group sentences ourselves (synth._group_sentences) and insert our own pacing
            # gaps between groups. Letting the server ALSO split re-chunks the group and stitches the
            # pieces with crossfades + its own 200ms pauses → audible seam artifacts between sentences
            # and doubled pauses. Send each group as ONE utterance so there are no internal seams.
            "split_text": False,
            "stream": False,
        }
        if exaggeration is not None:
            body["exaggeration"] = exaggeration
        if cfg_weight is not None:
            body["cfg_weight"] = cfg_weight   # higher = adhere to reference voice/accent
        if language:
            body["language"] = language       # multilingual model: holds accent / enables hi etc.
        if speed and speed != 1.0:
            body["speed_factor"] = speed

        ref_name = None
        if ref_audio is not None:
            ref_name = self._upload_reference(ref_audio)
        if ref_name:
            body["voice_mode"] = "clone"
            body["reference_audio_filename"] = ref_name
        else:
            body["voice_mode"] = "predefined"
            body["predefined_voice_id"] = voice  # e.g. "Emily.wav"

        return tts_client.post_bytes(f"{self.base}/tts", body)

    def _upload_reference(self, ref_audio: bytes) -> str | None:
        """Best-effort: upload a clone reference clip; return its server filename or None.

        The filename MUST be unique per distinct voice: the server saves uploads by filename and
        SKIPS re-uploads when the name already exists, and its voice-conditioning cache is keyed on
        the file path — so reusing one name (e.g. "ref.wav") collapses every clone to whichever voice
        landed there first. We name each clip by a hash of its bytes: same voice → same name (cheap
        cache hit + skip), different voice (Dad vs Mom) → different name (distinct conditioning)."""
        try:
            import hashlib
            import httpx
            digest = hashlib.sha1(ref_audio).hexdigest()[:16]
            fname = f"ref_{digest}.wav"
            files = {"files": (fname, ref_audio, "audio/wav")}
            with httpx.Client(timeout=60.0) as c:
                r = c.post(f"{self.base}/upload_reference", files=files)
                r.raise_for_status()
                data = r.json()
            # response shape varies; try common keys
            for key in ("uploaded_files", "filenames", "files"):
                v = data.get(key) if isinstance(data, dict) else None
                if v:
                    return v[0] if isinstance(v, list) else v
            log.warning("clone upload returned unrecognized shape; using predefined voice")
            return None
        except Exception as e:
            log.warning("clone reference upload failed (%s); using predefined voice", e)
            return None

    def list_voices(self) -> list[str]:
        """Predefined voice ids (e.g. ['Emily.wav', 'Adrian.wav', …]) for per-story / multi-voice
        selection. Empty list on failure → caller keeps the configured default."""
        try:
            import httpx
            with httpx.Client(timeout=8.0) as c:
                r = c.get(f"{self.base}/v1/audio/voices")
                if r.status_code != 200:
                    return []
                data = r.json()
            vs = data.get("voices") if isinstance(data, dict) else data
            return [v for v in vs if isinstance(v, str)] if isinstance(vs, list) else []
        except Exception:
            return []

    def phonemize(self, text: str) -> str | None:
        return None  # no IPA; pronunciation via respelling / cloned voice

    def supports_tags(self) -> bool:
        """Only Chatterbox-Turbo consumes paralinguistic tags ([chuckle] etc.); Multilingual
        does not. Read it from model-info so synth keeps tags for Turbo, strips them otherwise."""
        try:
            import httpx
            with httpx.Client(timeout=5.0) as c:
                r = c.get(f"{self.base}/api/model-info")
                return r.status_code == 200 and bool(r.json().get("supports_paralinguistic_tags"))
        except Exception:
            return False

    def health(self) -> bool:
        """Healthy only when the TTS model is actually loaded in VRAM — the server answers 200
        on / even with no model, but then /tts 503s. Requiring model-info.loaded lets synth
        fall back to Kokoro cleanly instead of failing a story."""
        try:
            import httpx
            with httpx.Client(timeout=5.0) as c:
                r = c.get(f"{self.base}/api/model-info")
                return r.status_code == 200 and bool(r.json().get("loaded"))
        except Exception:
            return False
