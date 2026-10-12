"""TTS engine interface + selector.

Two engines behind one Protocol, chosen by the effective `tts_engine` setting (Admin override
over TB_TTS_ENGINE). synth.py depends only on this Protocol, so engines are hot-swappable and
never leak into the rest of the pipeline. Engines ignore params they don't support.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from tellybear.core.config import Settings, get_settings


@runtime_checkable
class TTSEngine(Protocol):
    name: str
    supports_ipa: bool          # Kokoro True; Chatterbox False
    supports_cloning: bool      # Kokoro False; Chatterbox True
    supports_emotion: bool      # Kokoro False; Chatterbox True
    max_chars: int              # per-call char ceiling (Chatterbox ~1500; Kokoro large)

    def speak(self, text: str, *, voice: str, speed: float = 1.0,
              ref_audio: bytes | None = None, exaggeration: float | None = None,
              emotion: str | None = None, fmt: str = "wav", cfg_weight: float | None = None,
              language: str | None = None) -> bytes: ...

    def phonemize(self, text: str) -> str | None: ...  # None if unsupported

    def health(self) -> bool: ...  # is the engine reachable right now?


def get_engine(settings: Settings | None = None, name: str | None = None) -> TTSEngine:
    """Return the engine by explicit `name`, else the configured TB_TTS_ENGINE.
    (synth passes the effective `tts_engine` from app_setting.)"""
    s = settings or get_settings()
    engine = (name or s.TB_TTS_ENGINE).lower()
    if engine == "chatterbox":
        from tellybear.tts.engine_chatterbox import ChatterboxEngine
        return ChatterboxEngine(s)
    from tellybear.tts.engine_kokoro import KokoroEngine
    return KokoroEngine(s)
