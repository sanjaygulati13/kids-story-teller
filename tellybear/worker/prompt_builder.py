"""Prompt assembly for generation.

Uses the production prompts in prompts/ verbatim (gen_system.txt is self-contained: it
already carries the output schema). gen_user_template.txt is filled per request with the
child's band profile, character list, banned terms, story type and special request.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from tellybear.core.config import PROMPTS_DIR, Settings, get_settings
from tellybear.db.models import BannedTerm, Character, Child, Story, StoryRequest

AGE_RANGE = {"early": "3-5", "older": "6-8"}


def _recent_block(db: Session, child_id: str, limit: int = 10) -> str:
    """List the child's recently-told stories (title + opening line) so the generator makes the
    next one CLEARLY different — the main lever against a small model's repetition."""
    rows = (db.query(Story)
            .filter(Story.child_id == child_id, Story.status.notin_(("rejected", "failed")))
            .order_by(Story.created_at.desc())
            .limit(limit).all())
    if not rows:
        return "(none yet — you have a blank canvas)"
    lines: list[str] = []
    for st in rows:
        opening = ""
        try:
            segs = json.loads(st.story_json).get("segments", [])
            if segs:
                opening = " ".join((segs[0].get("text") or "").split()[:12])
        except Exception:
            pass
        lines.append(f'- "{st.title}"' + (f' — opens: "{opening}…"' if opening else ""))
    return "\n".join(lines)


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def band_rules_block(age_band: str) -> str:
    """Extract the band-specific rules + the always-on bedtime overlay from band_rules.md."""
    md = _read("band_rules.md")
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in md.splitlines():
        if line.startswith("## "):
            key = line[3:].strip().lower()
            if key.startswith("early"):
                current = "early"
            elif key.startswith("older"):
                current = "older"
            elif key.startswith("always"):
                current = "always"
            else:
                current = None
            if current:
                blocks[current] = []
            continue
        if current:
            blocks.setdefault(current, []).append(line)

    chosen = blocks.get(age_band, [])
    always = blocks.get("always", [])
    parts = ["\n".join(chosen).strip()]
    if always:
        parts.append("Bedtime overlay (always):\n" + "\n".join(always).strip())
    return "\n\n".join(p for p in parts if p)


def _characters_block(db: Session, request: StoryRequest) -> str:
    ids = json.loads(request.character_ids or "[]")
    if not ids:
        return "(none specified — you may invent gentle original characters)"
    chars = db.query(Character).filter(Character.id.in_(ids)).all()
    by_id = {c.id: c for c in chars}
    lines: list[str] = []
    for cid in ids:  # preserve requested order
        c = by_id.get(cid)
        if not c:
            continue
        bits = [f"- {c.name}"]
        if c.archetype:
            bits.append(f"({c.archetype})")
        if c.description:
            bits.append(f"— {c.description}")
        lines.append(" ".join(bits))
    return "\n".join(lines) if lines else "(none)"


def _notes_line(notes: str | None) -> str:
    """The parent's notes about this child (likes, fears, what's going on in their life), as one
    line. Weave likes in naturally; anything described as a fear or worry must not appear."""
    text = " ".join((notes or "").split())[:500]
    if not text:
        return "(nothing extra)"
    return (f"{text} — weave their interests in naturally where they fit; never include anything "
            "described here as a fear or worry.")


def _banned_block(db: Session, household_id: str) -> str:
    terms = db.query(BannedTerm).filter_by(household_id=household_id).all()
    if not terms:
        return "(none)"
    lines = []
    for t in terms:
        if t.replacement:
            lines.append(f"- {t.term} -> use \"{t.replacement}\" instead")
        else:
            lines.append(f"- {t.term} (do not use at all)")
    return "\n".join(lines)


@dataclass
class BuiltPrompt:
    system: str
    user: str
    model: str

    @property
    def messages(self) -> list[dict]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]


def build(db: Session, request: StoryRequest, household_id: str,
          settings: Settings | None = None, model: str | None = None) -> BuiltPrompt:
    s = settings or get_settings()
    child = db.get(Child, request.child_id)
    if child is None:
        raise ValueError(f"child {request.child_id} not found")

    system = _read("gen_system.txt")
    template = _read("gen_user_template.txt")
    # series continuity: inject the recurring cast/facts when this request continues a series
    series_block = ""
    if getattr(request, "continue_series", 0):
        from tellybear.core import world_bible
        block = world_bible.prompt_block(world_bible.load(child.world_bible), child.name)
        series_block = (block + "\n") if block else ""
    # creative breadth: a sampled palette of FAR-RANGING settings + a spark, to pull the model out of
    # its cozy-nature-at-night rut (scope/creativity fix). Seeded by request id so retries are stable
    # but every distinct request explores a different slice of the world.
    import random as _random
    from tellybear.worker import palette
    rng = _random.Random(f"{request.id}:{request.child_id}")
    fresh_settings = palette.fresh_inspiration_block(rng)
    # language: write the whole story in the child's language (default English). For Hindi, ask for
    # Devanagari with natural Hinglish loanwords — the multilingual TTS voices it correctly.
    lang = (getattr(child, "language", "") or "en").lower()
    if lang == "hi":
        fresh_settings = ("LANGUAGE: Write this ENTIRE story in simple, warm HINDI using Devanagari "
                          "script, for a young child. Short, gentle sentences. You may mix in common "
                          "everyday English words the way Indian families do (Hinglish) — but write "
                          "them in Devanagari too (e.g. गुड नाइट, टेडी). The title must also be in Hindi.\n\n"
                          + fresh_settings)
    user = template.format(
        child_name=child.name,
        pronouns=child.pronouns or "they/them",
        age_band=child.age_band,
        child_notes=_notes_line(child.notes),
        band_rules=band_rules_block(child.age_band),
        characters_block=_characters_block(db, request),
        story_type=request.story_type,
        special_request_or_none=request.special_request or "None",
        series_block=series_block,
        fresh_settings=fresh_settings,
        banned_block=_banned_block(db, household_id),
        avoid_block=_recent_block(db, request.child_id),
    )
    # Caller (worker) resolves the model from app_setting / gen_model_override at job time
    #; fall back to env default when not provided.
    resolved = model or (s.TB_OPUS_MODEL if (s.TB_ENABLE_OPUS and s.TB_OPUS_MODEL) else s.TB_LOCAL_GEN_MODEL)
    return BuiltPrompt(system=system, user=user, model=resolved)
