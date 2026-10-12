"""Kokoro-FastAPI adapter — the pronunciation/offline fallback engine.

speak     -> POST {kokoro url}/v1/audio/speech  (OpenAI-compatible; Admin tab or TB_KOKORO_URL)
phonemize -> POST {kokoro url}/dev/phonemize
`input` may contain inline [name](/ipa/) overrides; supports_ipa=True. No cloning, no emotion.
"""
from __future__ import annotations

from tellybear.core.config import Settings, get_settings
from tellybear.core import settings_store, tts_client


class KokoroEngine:
    name = "kokoro"
    supports_ipa = True
    supports_cloning = False
    supports_emotion = False
    max_chars = 100_000  # Kokoro handles long input fine

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.base = settings_store.tts_url("kokoro", self.s)

    def speak(self, text: str, *, voice: str, speed: float = 1.0,
              ref_audio: bytes | None = None, exaggeration: float | None = None,
              emotion: str | None = None, fmt: str = "wav", cfg_weight: float | None = None,
              language: str | None = None) -> bytes:
        # ref_audio / exaggeration / emotion / cfg_weight / language ignored (no cloning/emotion control;
        # the Kokoro voice id already implies its language)
        body = {
            "model": "kokoro",
            "voice": voice,
            "input": text,
            "response_format": fmt,
            "speed": speed,
        }
        return tts_client.post_bytes(f"{self.base}/v1/audio/speech", body)

    def list_voices(self) -> list[str]:
        """Kokoro voice ids (e.g. af_bella) for per-story / multi-voice selection."""
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
        if not self.base:
            return None  # Kokoro not configured
        data = tts_client.post_json(f"{self.base}/dev/phonemize", {"text": text, "language": "a"})
        # Kokoro returns {"phonemes": "...", "tokens": [...]}
        return data.get("phonemes") or data.get("ipa")

    def health(self) -> bool:
        return bool(self.base) and tts_client.get_ok(f"{self.base}/health")
