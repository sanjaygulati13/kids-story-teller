"""Configuration & environment loading.

All config comes from the environment (a .env loaded here). No secrets in code or DB.
Only two values are required — TB_LLM_URL (an OpenAI-compatible endpoint) and TB_ADMIN_EMAIL;
everything else has a default, and models / TTS engine / voice / daily limit can be changed live
in the parent app's Admin tab. Invalid or missing values fail fast with a clear message.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env from the repo root (parent of the tellybear package) before Settings reads env.
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

PROMPTS_DIR = _REPO_ROOT / "prompts"
DEFAULT_LOCAL_GEN_MODEL = "gemma-3-12b-it"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- core service ---
    TB_API_HOST: str = "0.0.0.0"
    TB_API_PORT: int = 8090
    TB_DATA_DIR: str = "./data"
    TB_DB_URL: str = ""                 # empty = SQLite at <TB_DATA_DIR>/tellybear.db

    # --- LLM: any OpenAI-compatible endpoint (required) ---
    TB_LLM_URL: str                     # base URL ending in /v1, e.g. http://localhost:8080/v1
    # Defaults only — both models can be picked live in the Admin tab from the gateway's /models.
    TB_LOCAL_GEN_MODEL: str = DEFAULT_LOCAL_GEN_MODEL
    TB_LOCAL_JUDGE_MODEL: str = ""      # empty = use the story model (works, but a separate,
                                        # stronger judge model is much safer)
    TB_GEN_TEMPERATURE: float = 0.95    # hotter generation = more variety / less repetition
    # --- GPU co-residency ---
    # When true, the LLM (gen/judge) and Chatterbox TTS are allowed to stay resident on the GPU
    # simultaneously — the bedtime gen->judge->synth path SKIPS the stop-Chatterbox / unload-LLM
    # hand-off (see tellybear/worker/gpu.py). Only flip this on for a gen+judge combo verified to
    # fit the VRAM budget together with Chatterbox (Chatterbox needs ~4-5 GB on top of the LLM).
    # Default OFF keeps the stop-and-swap behavior, which is required whenever the models don't
    # fit together (e.g. a 12B+ model on a 16 GB card).
    TB_GPU_CORESIDENT: bool = False
    # Master switch for the whole GPU hand-off (llama-swap load/unload, rocm-smi VRAM polling,
    # systemctl control of the Chatterbox service). Turn OFF unless you run that exact stack.
    TB_GPU_HANDOFF: bool = True
    # Optional bearer token for a hosted / keyed OpenAI-compatible LLM endpoint.
    TB_LLM_API_KEY: str = ""
    # Prepend Qwen's default system line for models whose id contains "qwen" (some Qwen GGUF chat
    # templates behave better with it). Off by default.
    TB_LLM_QWEN_PREFIX: bool = False
    # --- variety guard: semantic de-dup via small CPU embeddings (no GPU/VRAM) ---
    TB_VARIETY_GUARD: bool = True               # reject near-duplicate plots, regenerate
    TB_EMBED_MODEL: str = "BAAI/bge-small-en-v1.5"   # fastembed (ONNX, CPU); ~130MB, 384-dim
    TB_VARIETY_SIM_THRESHOLD: float = 0.82      # cosine >= this vs an existing story = too similar
    TB_VARIETY_MAX_RETRY: int = 1               # extra regenerations when too similar (bounds cost)
    # --- best-of-N selection + craft judge + editor pass ---
    TB_GEN_CANDIDATES: int = 3                  # candidates per request in the overnight BATCH
    TB_GEN_CANDIDATES_NOW: int = 1              # candidates for `now`-priority (keep fast)
    TB_GEN_EDITOR: bool = True                  # one editor revision on the selected winner (batch)
    TB_GEN_EDITOR_NOW: bool = False             # editor on `now` path (off — keeps it fast)
    # two-pass generation: Pass A = plain prose (no schema pressure), Pass B = structure into
    # JSON at low temp. Off by default — A/B via gen_model_override before enabling. Smaller
    # models that struggle to write good prose AND valid JSON at once benefit most.
    TB_GEN_TWO_PASS: bool = False
    # long-form "epic" mode: outline → per-chapter expand (with a continuity ledger) → stitch.
    # The per-request `long` flag gates each story; these set the capability + sizing. ~10 min read.
    TB_LONGFORM_ENABLED: bool = True
    TB_LONGFORM_TARGET_WORDS: int = 2000
    TB_LONGFORM_CHAPTERS: int = 6
    TB_LONGFORM_CHAPTER_WORDS: int = 350

    # --- TTS (Chatterbox primary, Kokoro fallback) ---
    TB_TTS_ENGINE: str = "chatterbox"
    # Chatterbox (GPU, expressive + cloning) — primary
    TB_CHATTERBOX_URL: str = "http://localhost:8890"
    TB_CHATTERBOX_VOICE: str = "Emily.wav"               # predefined voice id (/v1/audio/voices)
    TB_CHATTERBOX_EXAGGERATION: float = 0.4              # emotion intensity (low for bedtime)
    TB_CHATTERBOX_EXAGGERATION_WINDDOWN: float = 0.25    # softer on the final wind-down segment(s)
    TB_CHATTERBOX_SPEED: float = 1.0                     # 1.0 = native; <1 slows but distorts the voice
    # classifier-free guidance: higher = output adheres MORE to the reference voice/accent (less
    # base-model drift). Clones use the higher value so a parent's accent holds.
    TB_CHATTERBOX_CFG_WEIGHT: float = 0.4   # lower = looser, more expressive delivery (less flat/monotone)
    TB_CHATTERBOX_CFG_WEIGHT_CLONE: float = 0.8   # high adherence so the parent's accent holds
    # language for the Chatterbox MULTILINGUAL model (en, hi, …). The English/turbo model has a US-accent
    # prior that overrides a clone's accent; multilingual + en holds the reference accent far better.
    TB_CHATTERBOX_LANGUAGE: str = "en"
    TB_CHATTERBOX_EXAGGERATION_CLONE: float = 0.35       # steadier on clones so emotion doesn't pull the accent
    # per-story model selection (auto-swap): a clone-voiced story (Mom/Dad) uses the MULTILINGUAL
    # model so the parent's accent + Hindi hold; a predefined-voice story uses TURBO (English, with
    # paralinguistic expression tags). synth swaps the resident Chatterbox model per story when needed.
    TB_CHATTERBOX_AUTOSWAP_MODEL: bool = True
    TB_CHATTERBOX_MODEL_CLONE: str = "chatterbox-multilingual"   # holds parent accent + supports hi
    TB_CHATTERBOX_MODEL_DEFAULT: str = "chatterbox-turbo"        # English + paralinguistic expression tags
    TB_CHATTERBOX_FEMALE_VOICES: list[str] = ["Abigail.wav", "Alice.wav", "Cora.wav", "Elena.wav", "Emily.wav", "Gianna.wav", "Jade.wav", "Layla.wav", "Olivia.wav"]
    # voice variety: how the per-story narrator voice is chosen, and whether characters get
    # their own voices. random = a different (deterministic) voice per story; per_child = stable per
    # child; fixed = always TB_CHATTERBOX_VOICE.
    TB_TTS_VOICE_MODE: str = "random"                    # random | per_child | fixed
    TB_TTS_MULTIVOICE: bool = True                       # distinct voice per character (narrator + cast)
    TB_TTS_MAX_VOICES: int = 3                           # cap voices/story: narrator + (n-1) chars
    # story length bounds (structural guard; tune shorter for young attention spans)
    TB_WORDS_EARLY_MIN: int = 240
    TB_WORDS_EARLY_MAX: int = 560
    TB_WORDS_OLDER_MIN: int = 300
    TB_WORDS_OLDER_MAX: int = 700
    TB_TTS_WARMUP: bool = True                           # discard a warm-up gen so seg-1 isn't cold/rushed
    # pacing: slow the read for bedtime with SILENCE (not speed — Chatterbox <1.0 distorts).
    TB_TTS_LEADIN_SILENCE: float = 1.0                   # calm pause before the story starts
    TB_TTS_GAP_SECONDS: float = 0.6                      # silence between segments
    TB_TTS_SENTENCE_GAP: float = 0.28                    # silence between sentences within a segment (0=off)
    TB_TTS_WINDDOWN_GAP: float = 1.1                     # bigger gap around the final wind-down segments
    TB_TTS_PACE_EARLY_MULT: float = 1.3                  # younger band = slower; multiplies every gap above
    # speaking-rate slowdown via ffmpeg atempo (pitch-preserved time-stretch). <1.0 = slower.
    # 1.0 disables. Chatterbox's own speed<1 distorts, so we slow in the transcode instead.
    TB_TTS_ATEMPO: float = 0.9                           # older band (6–8): ~10% slower
    TB_TTS_ATEMPO_EARLY: float = 0.85                    # early band (3–5): ~15% slower
    # a cloned PARENT voice already sounds human — the synthetic slow-down makes it feel draggy/
    # digital, so clones play near natural speed with tighter pauses (parents read a bit faster).
    TB_TTS_ATEMPO_CLONE: float = 1.0                     # 1.0 = the parent's natural recorded pace
    TB_TTS_PACE_CLONE_MULT: float = 0.6                  # shorter inter-segment/sentence gaps for clones
    # synthesis granularity + seams. Synthesize 2–3-sentence GROUPS, not single sentences,
    # so prosody stays natural within a group (single-sentence calls sound choppy); pacing gaps go
    # between groups. Merge tiny fragments so Chatterbox doesn't hallucinate noise on "Hello?".
    TB_TTS_SENT_GROUP: int = 2                           # sentences per synthesis chunk (1 = per-sentence)
    TB_TTS_MIN_CHUNK_CHARS: int = 25                     # merge a chunk shorter than this into its neighbor
    TB_TTS_LOUDNORM: bool = True                         # ffmpeg loudnorm (I=-19 LUFS) — even level across chunks
    TB_TTS_WARMTH_EQ: bool = True                        # gentle high-shelf cut → warmer, less harsh/sibilant TTS
    # optional ambient bed. OPT-IN per story (story.ambient); this is just the global default
    # MODE when a request doesn't specify one. off = none; under = mixed quietly under the narration;
    # tail = a calm rain outro that plays only AFTER the story ends. Track = <data>/ambient/<track>.mp3.
    TB_TTS_AMBIENT: str = "off"                          # global default mode: off | under | tail
    TB_TTS_AMBIENT_TRACK: str = "rain"                   # which file in <data>/ambient/
    TB_TTS_AMBIENT_DB: float = -26.0                     # bed level when mixed UNDER the voice (dB)
    TB_TTS_AMBIENT_TAIL_SECONDS: float = 40.0           # length of the rain outro in "tail" mode
    # systemd --user unit for the Chatterbox server. /api/unload leaves ~1 GB of Torch CUDA
    # context resident; only stopping the PROCESS frees it (needed so a 16 GB LLM can load).
    TB_CHATTERBOX_SERVICE: str = "chatterbox-tts.service"
    # Kokoro (CPU, exact IPA pronunciation, offline) — fallback + pronunciation preview
    TB_KOKORO_URL: str = ""             # empty = no Kokoro (no fallback engine, no IPA previews)
    TB_KOKORO_VOICE: str = "af_bella"
    TB_KOKORO_SPEED: float = 0.9

    # --- notifications ---
    TB_NTFY_URL: str = ""           # empty URL or topic = notifications off
    TB_NTFY_TOPIC: str = ""

    # --- accounts / auth ---
    TB_ADMIN_EMAIL: str                 # first signup with this email becomes admin; only admin sees Admin
    TB_SESSION_TTL_DAYS: int = 30
    TB_OPEN_SIGNUP: bool = False        # invite-only by default; admins provision co-parents
    TB_COOKIE_SECURE: bool = False      # set true behind HTTPS (reverse proxy / tunnel) so cookies are Secure
    TB_API_TOKEN: str = ""              # optional service token for headless scripts (X-API-Token
                                        # header → admin household). Empty = disabled.

    # --- cloud model (optional, off by default) ---
    TB_ENABLE_OPUS: bool = False
    TB_ANTHROPIC_API_KEY: str | None = None
    TB_OPUS_MODEL: str | None = None

    # --- misc ---
    TB_HSA_OVERRIDE: str | None = None
    TB_BATCH_HOUR_LOCAL: int = 20       # local hour the nightly batch writes queued stories
    TB_PARENT_APP_URL: str | None = None
    TB_API_DOCS: bool = False           # expose /docs + /openapi.json

    @field_validator("TB_TTS_ENGINE")
    @classmethod
    def _engine_known(cls, v: str) -> str:
        if v not in ("chatterbox", "kokoro"):
            raise ValueError("TB_TTS_ENGINE must be 'chatterbox' or 'kokoro'")
        return v

    @model_validator(mode="after")
    def _conditional_requirements(self) -> "Settings":
        missing: list[str] = []
        if not self.TB_DB_URL:
            self.TB_DB_URL = f"sqlite:///{(Path(self.TB_DATA_DIR).expanduser() / 'tellybear.db').as_posix()}"
        if not self.TB_LOCAL_JUDGE_MODEL:
            self.TB_LOCAL_JUDGE_MODEL = self.TB_LOCAL_GEN_MODEL
        # Opus stays conditional.
        if self.TB_ENABLE_OPUS:
            if not self.TB_ANTHROPIC_API_KEY:
                missing.append("TB_ANTHROPIC_API_KEY (required when TB_ENABLE_OPUS=true)")
            if not self.TB_OPUS_MODEL:
                missing.append("TB_OPUS_MODEL (required when TB_ENABLE_OPUS=true)")
        if missing:
            raise ValueError("missing required configuration: " + "; ".join(missing))
        return self

    # --- derived paths ---
    @property
    def data_dir(self) -> Path:
        return Path(self.TB_DATA_DIR).expanduser()

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.audio_dir, self.logs_dir, self.voices_dir):
            d.mkdir(parents=True, exist_ok=True)


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Tellybear configuration error — fix your .env / environment:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        typ = err.get("type", "")
        if typ == "missing":
            lines.append(f"  - {loc}: required variable is not set")
        else:
            lines.append(f"  - {loc}: {err.get('msg')}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return validated settings; fail fast with a clear message on any problem."""
    try:
        s = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        sys.stderr.write(_format_validation_error(exc) + "\n")
        raise SystemExit(2) from exc
    return s
