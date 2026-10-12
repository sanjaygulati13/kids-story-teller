"""Pydantic request/response models for the parent API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


# --- children ---
class ChildCreate(BaseModel):
    name: str
    age_band: Literal["early", "older"]
    pronouns: str | None = None
    active: bool = True
    notes: str | None = None
    language: str = "en"


class ChildPatch(BaseModel):
    name: str | None = None
    age_band: Literal["early", "older"] | None = None
    pronouns: str | None = None
    active: bool | None = None
    length_override: str | None = None
    narrator_voice: str | None = None
    notes: str | None = None
    language: str | None = None


class ChildOut(BaseModel):
    id: str
    household_id: str
    public_slug: str
    name: str
    age_band: str
    pronouns: str | None
    active: bool
    length_override: str | None
    narrator_voice: str | None
    notes: str | None
    language: str = "en"
    created_at: str
    updated_at: str


# --- characters ---
class CharacterCreate(BaseModel):
    name: str
    archetype: str | None = None
    linked_child_id: str | None = None
    description: str | None = None


class CharacterPatch(BaseModel):
    name: str | None = None
    archetype: str | None = None
    linked_child_id: str | None = None
    description: str | None = None


class CharacterOut(BaseModel):
    id: str
    child_id: str
    name: str
    archetype: str | None
    linked_child_id: str | None
    description: str | None


class SuggestRequest(BaseModel):
    theme: str


class Suggestion(BaseModel):
    name: str
    archetype: str | None = None


class SuggestResponse(BaseModel):
    suggestions: list[Suggestion]


# --- pronunciation ---
class PronunciationOut(BaseModel):
    id: str
    household_id: str
    surface: str
    ipa: str | None
    respelling: str | None
    source: str


class PronunciationPreview(BaseModel):
    surface: str
    ipa: str | None = None
    source: str | None = None  # e.g. 'hindi_g2p' to use the espeak pass


class PronunciationCreate(BaseModel):
    surface: str
    ipa: str | None = None
    source: str = "manual"
    respelling: str | None = None


# --- banned vocabulary ---
class BannedCreate(BaseModel):
    term: str
    replacement: str | None = None


class BannedOut(BaseModel):
    id: str
    term: str
    replacement: str | None


# --- requests ---
class InventedCharacter(BaseModel):
    name: str
    archetype: str | None = None


class RequestCreate(BaseModel):
    child_id: str
    story_type: str
    character_ids: list[str] = Field(default_factory=list)
    # seed_stories.py path: invented cast created on the fly (name+archetype)
    characters: list[InventedCharacter] = Field(default_factory=list)
    special_request: str | None = None
    setting_hint: str | None = None        # folded into the prompt (no schema column needed)
    target_date: str | None = None
    priority: Literal["batch", "now"] = "batch"
    gen_model_override: str | None = None  # A/B a specific request on a chosen model
    auto_approve: bool = False             # trusted bulk seed: skip the parent gate
    seed_batch: bool = False               # mark as a seeded request (source)
    continue_series: bool = False          # reuse + grow the child's recurring cast
    long_form: bool = False                # ~10-min multi-chapter "epic" story
    ambient: Literal["off", "under", "tail"] = "off"   # rain: off | under narration | at the end

    @model_validator(mode="before")
    @classmethod
    def _accept_gen_model_alias(cls, data):
        if isinstance(data, dict) and not data.get("gen_model_override") and data.get("gen_model"):
            data = {**data, "gen_model_override": data["gen_model"]}
        return data


class WorldBibleCharacter(BaseModel):
    name: str
    description: str = ""


class WorldBible(BaseModel):
    characters: list[WorldBibleCharacter] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)


class StoryRequestOut(BaseModel):
    id: str
    child_id: str
    story_type: str
    character_ids: list[str]
    special_request: str | None
    target_date: str | None
    priority: str
    status: str
    source: str
    gen_model_override: str | None = None
    created_at: str


# --- admin: models & settings ---
class ModelInfo(BaseModel):
    id: str
    state: str | None = None  # 'running' | 'idle' | None


class ModelsOut(BaseModel):
    local: list[ModelInfo]
    cloud: list[ModelInfo]


class SettingsOut(BaseModel):
    active_gen_model: str
    active_judge_model: str
    judge_use_opus: bool
    gen_use_opus: bool
    tts_engine: str
    tts_voice: str
    tts_exaggeration: float
    daily_story_limit: int
    chatterbox_url: str = ""            # Admin override ("" = env default below)
    kokoro_url: str = ""
    chatterbox_url_default: str = ""    # from TB_CHATTERBOX_URL / TB_KOKORO_URL, for placeholders
    kokoro_url_default: str = ""


class SettingsPatch(BaseModel):
    active_gen_model: str | None = None
    active_judge_model: str | None = None
    judge_use_opus: bool | None = None
    gen_use_opus: bool | None = None
    tts_engine: Literal["chatterbox", "kokoro"] | None = None
    tts_voice: str | None = None
    tts_exaggeration: float | None = None
    daily_story_limit: int | None = Field(default=None, ge=1)
    chatterbox_url: str | None = Field(default=None, max_length=300)   # "" = back to the env default
    kokoro_url: str | None = Field(default=None, max_length=300)


class StatsOut(BaseModel):
    total_ready: int
    total_failed: int
    total_rejected: int
    by_gen_model: dict[str, int]
    by_engine: dict[str, int]
    gen_time: dict = Field(default_factory=dict)  # {by_model:{model:{avg_s,median_s}}}


class LibraryItem(BaseModel):
    id: str
    title: str
    child_id: str
    child_name: str
    status: str
    gen_model: str
    engine: str | None
    generation_seconds: float | None = None
    duration_seconds: float | None = None
    created_at: str
    nearest_sim: float | None = None      # max cosine to another story of the same child
    nearest_title: str | None = None      # which story it's closest to
    rating: int | None = None             # parent stars
    play_count: int = 0
    favorited: bool = False
    reject_reason: str | None = None
    craft_overall: float | None = None    # craft-judge headline (0..10)


# --- stories / approval ---
class SafetyOut(BaseModel):
    verdict: str
    categories: dict[str, str]
    reasons: str | None
    banned_hits: list[dict]
    judge_model: str


class StorySummaryOut(BaseModel):
    id: str
    title: str
    summary: str | None
    status: str
    stage: str | None = None
    child_id: str
    safety: SafetyOut | None
    duration_seconds: float | None = None       # audio length (story length) once ready
    generation_seconds: float | None = None     # operator metric
    gen_model: str | None = None
    engine: str | None = None
    created_at: str
    # quality feedback
    rating: int | None = None
    play_count: int = 0
    favorited: bool = False
    reject_reason: str | None = None
    craft_overall: float | None = None          # craft-judge headline score (0..10)


class AssetOut(BaseModel):
    kind: str
    duration_seconds: float | None
    voice: str | None


class StoryDetailOut(StorySummaryOut):
    age_band: str
    story_json: dict
    word_count: int | None
    gen_model: str
    assets: list[AssetOut]


class ApproveRequest(BaseModel):
    override_fail: bool = False


# reject/regenerate reason chips. UI enforces a pick; API defaults to "other".
REJECT_REASONS = {"too_similar", "boring", "wrong_tone", "too_long", "too_short",
                  "bad_fit_request", "safety", "other"}


class RejectRequest(BaseModel):
    reason: str | None = None
    note: str | None = None


class RateRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    note: str | None = None
