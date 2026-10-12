"""Speech-to-text helpers for child device voice requests."""
from __future__ import annotations

from tempfile import NamedTemporaryFile
from threading import Lock

MAX_AUDIO_BYTES = 4 * 1024 * 1024

_INITIAL_PROMPT = "A young child is asking for a short bedtime story."

_model = None
_model_lock = Lock()


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace(".", " ").replace(",", " ").split())


def _is_prompt_echo(text: str) -> bool:
    """Whisper echoes its own initial_prompt (once or repeated) when it hears
    (near-)silence. Treat a transcript that is nothing but prompt echoes as empty."""
    remainder = _normalize(text).replace(_normalize(_INITIAL_PROMPT), "").strip()
    return remainder == ""


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                _model = WhisperModel("small", device="cpu", compute_type="int8")
    return _model


def transcribe(audio_bytes: bytes, suffix: str) -> str:
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("audio clip too large")

    with NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        segments, _info = _get_model().transcribe(
            tmp.name,
            initial_prompt=_INITIAL_PROMPT,
            vad_filter=True,                 # drop non-speech so silence -> no segments
            condition_on_previous_text=False,  # stop the prompt/segment repetition loop
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if _is_prompt_echo(text):
            return ""
        return text
