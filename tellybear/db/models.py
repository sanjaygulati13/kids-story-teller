"""ORM models for the database schema.

Portable types only: TEXT ids (CHAR(36)/uuid string), TEXT timestamps (UTC ISO-8601),
INTEGER booleans, REAL for durations. JSON-bearing columns are stored as TEXT and
(de)serialized by the application layer.
"""
from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, REAL, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tellybear.db.base import Base, new_id, new_slug, utcnow_iso


def _id_col() -> Mapped[str]:
    return mapped_column(Text, primary_key=True, default=new_id)


class Household(Base):
    __tablename__ = "household"
    id: Mapped[str] = _id_col()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)


class Parent(Base):
    __tablename__ = "parent"
    id: Mapped[str] = _id_col()
    household_id: Mapped[str] = mapped_column(Text, ForeignKey("household.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    external_subject: Mapped[str | None] = mapped_column(Text, nullable=True)  # reserved for an external identity provider
    # built-in accounts: bcrypt hash only — never the plaintext. password null until claimed.
    email: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_admin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)


class Session(Base):
    __tablename__ = "session"
    id: Mapped[str] = _id_col()
    parent_id: Mapped[str] = mapped_column(Text, ForeignKey("parent.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # sha256 of the cookie token
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)


class Child(Base):
    __tablename__ = "child"
    id: Mapped[str] = _id_col()
    household_id: Mapped[str] = mapped_column(Text, ForeignKey("household.id"), nullable=False)
    # capability id for the unauthenticated /play/{slug} route; never a FK target.
    public_slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True, default=new_slug)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    age_band: Mapped[str] = mapped_column(Text, nullable=False)
    pronouns: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    length_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK added with use_alter because voice_profile is created after child.
    narrator_voice: Mapped[str | None] = mapped_column(
        Text, ForeignKey("voice_profile.id", use_alter=True, name="fk_child_narrator_voice"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="en")  # story language (en|hi|…)
    # recurring series cast + ongoing facts: JSON {characters:[{name,description}], facts:[str]}
    world_bible: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)
    __table_args__ = (CheckConstraint("age_band IN ('early','older')", name="ck_child_age_band"),)


class Character(Base):
    __tablename__ = "character"
    id: Mapped[str] = _id_col()
    child_id: Mapped[str] = mapped_column(Text, ForeignKey("child.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # ORIGINAL name
    archetype: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_child_id: Mapped[str | None] = mapped_column(Text, ForeignKey("child.id"), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)


class Pronunciation(Base):
    __tablename__ = "pronunciation"
    id: Mapped[str] = _id_col()
    household_id: Mapped[str] = mapped_column(Text, ForeignKey("household.id"), nullable=False)
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    ipa: Mapped[str | None] = mapped_column(Text, nullable=True)
    respelling: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)
    __table_args__ = (UniqueConstraint("household_id", "surface", name="uq_pron_household_surface"),)


class VoiceProfile(Base):
    __tablename__ = "voice_profile"
    id: Mapped[str] = _id_col()
    household_id: Mapped[str] = mapped_column(Text, ForeignKey("household.id"), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    engine: Mapped[str] = mapped_column(Text, nullable=False)
    voice_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)
    __table_args__ = (CheckConstraint("kind IN ('builtin','clone')", name="ck_voice_kind"),)


class BannedTerm(Base):
    __tablename__ = "banned_term"
    id: Mapped[str] = _id_col()
    household_id: Mapped[str] = mapped_column(Text, ForeignKey("household.id"), nullable=False)
    term: Mapped[str] = mapped_column(Text, nullable=False)
    replacement: Mapped[str | None] = mapped_column(Text, nullable=True)  # null = forbid entirely
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    __table_args__ = (UniqueConstraint("household_id", "term", name="uq_banned_household_term"),)


class StoryRequest(Base):
    __tablename__ = "story_request"
    id: Mapped[str] = _id_col()
    child_id: Mapped[str] = mapped_column(Text, ForeignKey("child.id"), nullable=False)
    story_type: Mapped[str] = mapped_column(Text, nullable=False)
    character_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    special_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(Text, nullable=False, default="batch")  # batch|now
    # if set, generate this request with this model id (A/B); else app_setting active_gen_model
    gen_model_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    continue_series: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # series continuity
    long_form: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # long-form ~10-min epic
    ambient: Mapped[str] = mapped_column(Text, nullable=False, default="off")  # off|under|tail (rain)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")  # queued|generating|generated|failed
    source: Mapped[str] = mapped_column(Text, nullable=False, default="parent")  # parent|starter_import
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)


class Story(Base):
    __tablename__ = "story"
    id: Mapped[str] = _id_col()
    request_id: Mapped[str | None] = mapped_column(Text, ForeignKey("story_request.id"), nullable=True)
    child_id: Mapped[str] = mapped_column(Text, ForeignKey("child.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    age_band: Mapped[str] = mapped_column(Text, nullable=False)
    story_json: Mapped[str] = mapped_column(Text, nullable=False)  # canonical Story JSON
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gen_model: Mapped[str] = mapped_column(Text, nullable=False)
    # queued|generating|judging|needs_approval|approved|rejected|synthesizing|ready|failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="needs_approval")
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="short")  # short | long
    ambient: Mapped[str] = mapped_column(Text, nullable=False, default="off")  # off|under|tail (rain)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="en")  # en|hi|… (gen + TTS)
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)  # human-readable, for parent UI
    generation_seconds: Mapped[float | None] = mapped_column(REAL, nullable=True)  # gen+judge+synth
    gen_step_seconds: Mapped[float | None] = mapped_column(REAL, nullable=True)  # just the gen call
    # quality feedback: parent rating, reject reason, replay count, craft-judge verdict
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)            # 1..5
    rating_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)        # chip enum value
    reject_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # qualifying plays
    favorited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    craft_json: Mapped[str | None] = mapped_column(Text, nullable=True)           # craft-judge verdict
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)

    reviews: Mapped[list["SafetyReview"]] = relationship(backref="story", cascade="all, delete-orphan")
    assets: Mapped[list["StoryAsset"]] = relationship(backref="story", cascade="all, delete-orphan")


class SafetyReview(Base):
    __tablename__ = "safety_review"
    id: Mapped[str] = _id_col()
    story_id: Mapped[str] = mapped_column(Text, ForeignKey("story.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)  # pass|fail
    categories: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    banned_hits: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    judge_model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class StoryAsset(Base):
    __tablename__ = "story_asset"
    id: Mapped[str] = _id_col()
    story_id: Mapped[str] = mapped_column(Text, ForeignKey("story.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # wav|mp3|voice_variant
    path: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(REAL, nullable=True)
    voice: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class PlaybackEvent(Base):
    __tablename__ = "playback_event"
    id: Mapped[str] = _id_col()
    child_id: Mapped[str] = mapped_column(Text, ForeignKey("child.id"), nullable=False)
    story_id: Mapped[str] = mapped_column(Text, ForeignKey("story.id"), nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)  # play|pause|complete|progress
    position_seconds: Mapped[float | None] = mapped_column(REAL, nullable=True)
    percent: Mapped[float | None] = mapped_column(REAL, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class StoryEmbedding(Base):
    """Semantic fingerprint of a story for the variety guard. A small CPU embedding
    (no GPU/VRAM) of title+body; brute-force cosine over a child's stored vectors at generation
    time rejects near-duplicate plots. One row per story; recomputed only if the model changes."""
    __tablename__ = "story_embedding"
    story_id: Mapped[str] = mapped_column(Text, ForeignKey("story.id", ondelete="CASCADE"),
                                          primary_key=True)
    child_id: Mapped[str] = mapped_column(Text, ForeignKey("child.id"), nullable=False, index=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)   # embedding model id (corpus may mix)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vec: Mapped[str] = mapped_column(Text, nullable=False)      # JSON list[float], L2-normalized
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class AppSetting(Base):
    """Operator/admin runtime settings. Overrides TB_* env defaults so models/engine
    can be switched from the Admin page without editing env or restarting. Effective value =
    app_setting[key] if present, else the matching TB_* default (see core.settings_store)."""
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # text/JSON, parsed by type at read
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso)
