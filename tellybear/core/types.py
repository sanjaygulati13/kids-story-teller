"""Shared Pydantic models, incl. the canonical Story JSON contract."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

AgeBand = Literal["early", "older"]


class StoryCharacter(BaseModel):
    name: str
    role: str = "character"
    archetype: str | None = None


class StorySegment(BaseModel):
    speaker: str  # "narrator" or a character name present in `characters`
    text: str
    # optional bedtime-safe emotion driving TTS expressiveness. Mapped to Chatterbox's
    # `exaggeration`; ignored by Kokoro. Older stories without it still validate.
    emotion: str | None = None


class StoryJSON(BaseModel):
    """The generator must output exactly this."""
    title: str
    age_band: AgeBand
    characters: list[StoryCharacter]
    moral: str = ""
    summary: str = ""
    segments: list[StorySegment]
    estimated_word_count: int = 0

    @field_validator("segments")
    @classmethod
    def _non_empty(cls, v: list[StorySegment]) -> list[StorySegment]:
        if not v:
            raise ValueError("segments must be a non-empty ordered list")
        return v

    def narration_text(self) -> str:
        """Concatenated story text, in order (single-voice collapse)."""
        return "\n".join(s.text for s in self.segments)

    def word_count(self) -> int:
        return len(self.narration_text().split())


class SafetyVerdict(BaseModel):
    """Mirror of safety_check.SafetyResult for typed API responses."""
    verdict: Literal["pass", "fail"]
    layer: str = ""
    categories: dict[str, str] = Field(default_factory=dict)
    reasons: str = ""
    summary: str = ""
    lexical_hits: list[dict] = Field(default_factory=list)
    judge_model: str = ""
