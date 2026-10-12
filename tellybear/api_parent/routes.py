"""Parent API. Mounted at /api/parent by main.py.

Every route is household-scoped via current_household(). Routes are sync `def` so
FastAPI runs them in a threadpool — safe for the blocking LLM/TTS calls they make.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from tellybear.core.config import get_settings
from tellybear.core.deps import current_household, current_parent, require_admin
from tellybear.core import auth
from tellybear.core.ip_names import is_known_ip
from tellybear.core.llm_client import get_llm_client
from tellybear.core import notify, settings_store
from tellybear.core.types import StoryJSON
from tellybear.core.config import PROMPTS_DIR
from tellybear.db.base import get_db
from tellybear.db.models import (
    BannedTerm, Character, Child, Household, Parent, PlaybackEvent, SafetyReview, Story, StoryAsset,
    StoryEmbedding, StoryRequest, VoiceProfile,
)
from tellybear.db.base import new_id, utcnow_iso
from tellybear.pron import lexicon as pron_lexicon
from tellybear.pron import inject as pron_inject
from tellybear.tts.engine import get_engine
from . import schemas as S

router = APIRouter()

LOG_UNITS = {
    "tellybear-api",
    "tellybear-worker",
    "chatterbox-tts",
    "tellybear-batch",
    "tellybear-watchdog",
}


# ----------------------------- helpers -----------------------------
def _child_or_404(db: Session, hh: Household, child_id: str) -> Child:
    child = db.get(Child, child_id)
    if child is None or child.household_id != hh.id:
        raise HTTPException(404, "child not found")
    return child


@router.get("/children/{child_id}/world_bible", response_model=S.WorldBible)
def get_world_bible(child_id: str, db: Session = Depends(get_db),
                    hh: Household = Depends(current_household)):
    """The child's recurring series cast + ongoing facts."""
    from tellybear.core import world_bible
    child = _child_or_404(db, hh, child_id)
    return S.WorldBible(**world_bible.load(child.world_bible))


@router.put("/children/{child_id}/world_bible", response_model=S.WorldBible)
def put_world_bible(child_id: str, body: S.WorldBible, db: Session = Depends(get_db),
                    hh: Household = Depends(current_household)):
    """Replace the child's world bible (parent curates: add/remove recurring characters + facts)."""
    from tellybear.core import world_bible
    child = _child_or_404(db, hh, child_id)
    bible = world_bible.load(world_bible.dump({
        "characters": [c.model_dump() for c in body.characters], "facts": body.facts}))
    child.world_bible = world_bible.dump(bible)
    db.commit()
    return S.WorldBible(**bible)


def _child_out(c: Child) -> S.ChildOut:
    return S.ChildOut(
        id=c.id, household_id=c.household_id, public_slug=c.public_slug, name=c.name,
        age_band=c.age_band, pronouns=c.pronouns, active=bool(c.active),
        length_override=c.length_override, narrator_voice=c.narrator_voice, notes=c.notes,
        language=getattr(c, "language", "en") or "en",
        created_at=c.created_at, updated_at=c.updated_at,
    )


def _char_out(c: Character) -> S.CharacterOut:
    return S.CharacterOut(id=c.id, child_id=c.child_id, name=c.name, archetype=c.archetype,
                          linked_child_id=c.linked_child_id, description=c.description)


def _safety_out(db: Session, story_id: str) -> S.SafetyOut | None:
    r = (db.query(SafetyReview).filter_by(story_id=story_id)
         .order_by(SafetyReview.created_at.desc()).first())
    if r is None:
        return None
    return S.SafetyOut(
        verdict=r.verdict, categories=json.loads(r.categories or "{}"),
        reasons=r.reasons, banned_hits=json.loads(r.banned_hits or "[]"),
        judge_model=r.judge_model,
    )


def _story_in_household(db: Session, hh: Household, story_id: str) -> tuple[Story, Child]:
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(404, "story not found")
    child = db.get(Child, story.child_id)
    if child is None or child.household_id != hh.id:
        raise HTTPException(404, "story not found")
    return story, child


# ----------------------------- children -----------------------------
@router.get("/children", response_model=list[S.ChildOut])
def list_children(db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    rows = db.query(Child).filter_by(household_id=hh.id).order_by(Child.created_at).all()
    return [_child_out(c) for c in rows]


@router.post("/children", response_model=S.ChildOut, status_code=201)
def create_child(body: S.ChildCreate, db: Session = Depends(get_db),
                 hh: Household = Depends(current_household)):
    c = Child(household_id=hh.id, name=body.name, age_band=body.age_band,
              pronouns=body.pronouns, active=1 if body.active else 0, notes=body.notes,
              language=body.language or "en")
    db.add(c); db.commit(); db.refresh(c)
    return _child_out(c)


@router.get("/children/{child_id}", response_model=S.ChildOut)
def get_child(child_id: str, db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    return _child_out(_child_or_404(db, hh, child_id))


@router.patch("/children/{child_id}", response_model=S.ChildOut)
def patch_child(child_id: str, body: S.ChildPatch, db: Session = Depends(get_db),
                hh: Household = Depends(current_household)):
    c = _child_or_404(db, hh, child_id)
    data = body.model_dump(exclude_unset=True)
    if "active" in data:
        c.active = 1 if data.pop("active") else 0
    if "narrator_voice" in data:
        nv = data["narrator_voice"]
        if nv in (None, "", "default"):
            data["narrator_voice"] = None
        elif db.query(VoiceProfile).filter_by(id=nv, household_id=hh.id).one_or_none() is None:
            raise HTTPException(422, "unknown voice for this family")
    for k, v in data.items():
        setattr(c, k, v)
    db.commit(); db.refresh(c)
    return _child_out(c)


def _cascade_delete_story(db: Session, story_id: str) -> None:
    """Delete a story + its assets (files on disk), reviews, playback."""
    for a in db.query(StoryAsset).filter_by(story_id=story_id).all():
        try:
            p = Path(a.path)
            if p.is_file():
                p.unlink()
        except OSError:
            pass
    db.query(StoryAsset).filter_by(story_id=story_id).delete()
    db.query(SafetyReview).filter_by(story_id=story_id).delete()
    db.query(PlaybackEvent).filter_by(story_id=story_id).delete()
    s = db.get(Story, story_id)
    if s is not None:
        db.delete(s)


@router.post("/children/{child_id}/rotate-slug", response_model=S.ChildOut)
def rotate_slug(child_id: str, db: Session = Depends(get_db),
                hh: Household = Depends(current_household)):
    """Issue a new player link for this child; the old /play/<slug> link (and any device flashed
    with it) stops working immediately. Use this if a link leaks."""
    from tellybear.db.base import new_slug
    c = _child_or_404(db, hh, child_id)
    c.public_slug = new_slug()
    db.commit(); db.refresh(c)
    return _child_out(c)


@router.delete("/children/{child_id}", status_code=204)
def delete_child(child_id: str, db: Session = Depends(get_db),
                 hh: Household = Depends(current_household)):
    """Permanently delete a child and everything that belongs ONLY to them — their stories (audio
    files + reviews + playback), requests, and characters. Characters in
    OTHER children that merely referenced this child as a sibling have that link cleared, not
    deleted. Household-scoped (any parent in the household)."""
    child = _child_or_404(db, hh, child_id)
    for s in db.query(Story).filter_by(child_id=child_id).all():
        _cascade_delete_story(db, s.id)
    db.flush()  # stories must be gone before the bulk request delete (FK story.request_id)
    db.query(StoryRequest).filter_by(child_id=child_id).delete()
    db.query(PlaybackEvent).filter_by(child_id=child_id).delete()
    # clear sibling links pointing at this child, then delete this child's own characters
    for ch in db.query(Character).filter_by(linked_child_id=child_id).all():
        ch.linked_child_id = None
    db.query(Character).filter_by(child_id=child_id).delete()
    db.delete(child)
    db.commit()
    return Response(status_code=204)


# ----------------------------- co-parents -----------------------------
from pydantic import BaseModel  # noqa: E402


class CoParentIn(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class CoParentOut(BaseModel):
    id: str
    email: str | None
    display_name: str
    is_admin: bool


def _coparent_out(p: Parent) -> CoParentOut:
    return CoParentOut(id=p.id, email=p.email, display_name=p.display_name, is_admin=bool(p.is_admin))


@router.get("/coparents", response_model=list[CoParentOut])
def list_coparents(db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    rows = db.query(Parent).filter_by(household_id=hh.id).order_by(Parent.created_at).all()
    return [_coparent_out(p) for p in rows]


@router.post("/coparents", response_model=CoParentOut, status_code=201)
def add_coparent(body: CoParentIn, db: Session = Depends(get_db),
                 me: Parent = Depends(current_parent), hh: Household = Depends(current_household)):
    """Provision a login for another parent in THIS household — they then share all of its
    children/stories/approvals. Any household member can add a co-parent."""
    email = (body.email or "").strip().lower()
    if "@" not in email or "." not in email:
        raise HTTPException(422, "enter a valid email address")
    from tellybear.api_auth.routes import check_password_rules
    check_password_rules(body.password)
    if auth.lookup_parent_by_email(db, email) is not None:
        raise HTTPException(409, "that email already has an account")
    p = Parent(household_id=hh.id, display_name=(body.display_name or email.split("@")[0]).strip(),
               email=email, password_hash=auth.hash_password(body.password), is_admin=0)
    db.add(p); db.commit(); db.refresh(p)
    return _coparent_out(p)


# ----------------------------- characters -----------------------------
@router.get("/children/{child_id}/characters", response_model=list[S.CharacterOut])
def list_characters(child_id: str, db: Session = Depends(get_db),
                    hh: Household = Depends(current_household)):
    _child_or_404(db, hh, child_id)
    rows = db.query(Character).filter_by(child_id=child_id).order_by(Character.created_at).all()
    return [_char_out(c) for c in rows]


@router.post("/children/{child_id}/characters", response_model=S.CharacterOut, status_code=201)
def create_character(child_id: str, body: S.CharacterCreate, db: Session = Depends(get_db),
                     hh: Household = Depends(current_household)):
    _child_or_404(db, hh, child_id)
    if is_known_ip(body.name):
        raise HTTPException(422, f"'{body.name}' looks like a copyrighted/branded character; "
                                 "use an original name.")
    if body.linked_child_id:
        _child_or_404(db, hh, body.linked_child_id)  # ensure sibling is in household
    c = Character(child_id=child_id, name=body.name, archetype=body.archetype,
                  linked_child_id=body.linked_child_id, description=body.description)
    db.add(c); db.commit(); db.refresh(c)
    return _char_out(c)


@router.patch("/characters/{char_id}", response_model=S.CharacterOut)
def patch_character(char_id: str, body: S.CharacterPatch, db: Session = Depends(get_db),
                    hh: Household = Depends(current_household)):
    c = db.get(Character, char_id)
    if c is None or _child_or_404(db, hh, c.child_id) is None:
        raise HTTPException(404, "character not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] and is_known_ip(data["name"]):
        raise HTTPException(422, f"'{data['name']}' looks like a copyrighted character.")
    if data.get("linked_child_id"):
        _child_or_404(db, hh, data["linked_child_id"])
    for k, v in data.items():
        setattr(c, k, v)
    db.commit(); db.refresh(c)
    return _char_out(c)


@router.delete("/characters/{char_id}", status_code=204)
def delete_character(char_id: str, db: Session = Depends(get_db),
                     hh: Household = Depends(current_household)):
    c = db.get(Character, char_id)
    if c is None:
        raise HTTPException(404, "character not found")
    _child_or_404(db, hh, c.child_id)
    db.delete(c); db.commit()
    return Response(status_code=204)


@router.get("/children/{child_id}/characters/from-stories", response_model=S.SuggestResponse)
def characters_from_stories(child_id: str, db: Session = Depends(get_db),
                            hh: Household = Depends(current_household)):
    """Extract reusable characters from this child's existing stories (the parent's 'extract
    from previous descriptions' ask). Returns distinct character names that appeared in any of
    the child's non-rejected stories and are NOT already saved — original names only."""
    child = _child_or_404(db, hh, child_id)
    existing = {c.name.strip().lower()
                for c in db.query(Character).filter_by(child_id=child_id).all()}
    skip = existing | {child.name.strip().lower(), "narrator"}
    found: dict[str, S.Suggestion] = {}  # keyed by lowercased name → first occurrence wins
    stories = (db.query(Story)
               .filter(Story.child_id == child_id, Story.status != "rejected")
               .order_by(Story.created_at.desc()).all())
    for st in stories:
        try:
            chars = json.loads(st.story_json).get("characters", [])
        except Exception:
            continue
        for ch in chars:
            name = (ch.get("name") or "").strip()
            key = name.lower()
            if not name or key in skip or key in found or is_known_ip(name):
                continue
            found[key] = S.Suggestion(name=name, archetype=ch.get("archetype"))
    return S.SuggestResponse(suggestions=list(found.values()))


@router.post("/children/{child_id}/characters/suggest", response_model=S.SuggestResponse)
def suggest_characters(child_id: str, body: S.SuggestRequest, db: Session = Depends(get_db),
                       hh: Household = Depends(current_household)):
    _child_or_404(db, hh, child_id)
    s = get_settings()
    # NOTE: use .replace, not .format — the prompt contains literal JSON braces ({ "suggestions": … })
    # that str.format would misparse as fields (KeyError). Only {theme} is a real placeholder.
    prompt = (PROMPTS_DIR / "character_suggest.txt").read_text(encoding="utf-8").replace("{theme}", body.theme)
    # Use the ADMIN-selected gen model (settings_store), not the raw env default — the env can drift
    # out of sync with the live llama-swap lineup (that's what 400'd this before). Fall back to env.
    eff = settings_store.effective(db, s)
    model = s.TB_OPUS_MODEL if (s.TB_ENABLE_OPUS and s.TB_OPUS_MODEL) else (eff.active_gen_model or s.TB_LOCAL_GEN_MODEL)
    raw = get_llm_client().generate([{"role": "user", "content": prompt}], model=model, json_mode=True)
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        suggestions = [S.Suggestion(name=x["name"], archetype=x.get("archetype"))
                       for x in data.get("suggestions", []) if not is_known_ip(x.get("name", ""))]
    except Exception:
        raise HTTPException(502, "character suggestion model returned malformed output")
    return S.SuggestResponse(suggestions=suggestions)


# ----------------------------- pronunciation -----------------------------
@router.get("/pronunciations", response_model=list[S.PronunciationOut])
def list_pronunciations(db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    return [S.PronunciationOut(id=p.id, household_id=p.household_id, surface=p.surface,
                               ipa=p.ipa, respelling=p.respelling, source=p.source)
            for p in pron_lexicon.list_for(db, hh.id)]


@router.post("/pronunciations/preview")
def preview_pronunciation(body: S.PronunciationPreview, hh: Household = Depends(current_household)):
    s = get_settings()
    # The tuner is IPA-based, so always preview on Kokoro (the only IPA-capable engine),
    # regardless of the active narration engine.
    engine = get_engine(s, name="kokoro")
    ipa = body.ipa
    if body.source == "hindi_g2p" and not ipa:
        try:
            ipa = pron_lexicon.hindi_g2p(body.surface)
        except NotImplementedError as e:
            raise HTTPException(501, str(e))
    text = f"[{body.surface}](/{ipa}/)" if ipa else body.surface
    audio = engine.speak(text, voice=s.TB_KOKORO_VOICE, speed=s.TB_KOKORO_SPEED, fmt="mp3")
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/pronunciations", response_model=S.PronunciationOut, status_code=201)
def upsert_pronunciation(body: S.PronunciationCreate, db: Session = Depends(get_db),
                         hh: Household = Depends(current_household)):
    p = pron_lexicon.upsert(db, hh.id, body.surface, body.ipa, body.source, body.respelling)
    return S.PronunciationOut(id=p.id, household_id=p.household_id, surface=p.surface,
                              ipa=p.ipa, respelling=p.respelling, source=p.source)


@router.delete("/pronunciations/{pron_id}", status_code=204)
def delete_pronunciation(pron_id: str, db: Session = Depends(get_db),
                         hh: Household = Depends(current_household)):
    if not pron_lexicon.delete(db, hh.id, pron_id):
        raise HTTPException(404, "pronunciation not found")
    return Response(status_code=204)


# ----------------------------- banned vocabulary -----------------------------
@router.get("/banned", response_model=list[S.BannedOut])
def list_banned(db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    rows = db.query(BannedTerm).filter_by(household_id=hh.id).order_by(BannedTerm.term).all()
    return [S.BannedOut(id=b.id, term=b.term, replacement=b.replacement) for b in rows]


@router.post("/banned", response_model=S.BannedOut, status_code=201)
def create_banned(body: S.BannedCreate, db: Session = Depends(get_db),
                  hh: Household = Depends(current_household)):
    existing = db.query(BannedTerm).filter_by(household_id=hh.id, term=body.term).one_or_none()
    if existing:
        existing.replacement = body.replacement
        db.commit(); db.refresh(existing)
        return S.BannedOut(id=existing.id, term=existing.term, replacement=existing.replacement)
    b = BannedTerm(household_id=hh.id, term=body.term, replacement=body.replacement)
    db.add(b); db.commit(); db.refresh(b)
    return S.BannedOut(id=b.id, term=b.term, replacement=b.replacement)


@router.delete("/banned/{banned_id}", status_code=204)
def delete_banned(banned_id: str, db: Session = Depends(get_db),
                  hh: Household = Depends(current_household)):
    b = db.query(BannedTerm).filter_by(id=banned_id, household_id=hh.id).one_or_none()
    if b is None:
        raise HTTPException(404, "banned term not found")
    db.delete(b); db.commit()
    return Response(status_code=204)


# ----------------------------- voices: parent-voice cloning -----------------------------
# A clean, phonetically-rich, bedtime-register reference passage. Read warmly and slowly, as if
# reading to your child. ~20–40 s is ideal for a zero-shot clone; up to ~60 s is fine.
VOICE_SCRIPT = (
    "Hello, my love. It's time to settle down now, all cozy and warm.\n\n"
    "On a quiet little hill, a small gray owl watched the very first stars blink awake, while a "
    "gentle breeze hummed through the tall, whispering trees.\n\n"
    "\"Are you sleepy yet?\" asked the owl. \"Almost,\" yawned the little fox.\n\n"
    "You are safe, you are loved, and the whole wide world is drifting off to sleep. "
    "Goodnight, little one. Sweet dreams."
)


MAX_VOICE_UPLOAD_BYTES = 20 * 1024 * 1024


def _transcode_to_wav(src: Path, dst: Path) -> bool:
    """Convert any uploaded audio (m4a/webm/mp3/…) to a clean mono 24 kHz WAV for cloning."""
    import shutil as _sh
    ff = _sh.which("ffmpeg")
    if not ff:
        try:
            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return False
    try:
        import subprocess
        # mono 24 kHz, trimmed to 28 s — Chatterbox rejects reference clips > 30 s
        # local files only (no playlist/concat demuxers reaching out to URLs), bounded runtime
        subprocess.run([ff, "-y", "-protocol_whitelist", "file", "-i", str(src),
                        "-ac", "1", "-ar", "24000", "-t", "28", str(dst)],
                       capture_output=True, check=True, timeout=60)
        return True
    except Exception:
        return False


def _voice_out(v: VoiceProfile) -> dict:
    return {"id": v.id, "label": v.label, "kind": v.kind, "engine": v.engine,
            "has_clip": bool(v.ref_audio_path), "created_at": v.created_at}


@router.get("/voices")
def list_voices(db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    rows = db.query(VoiceProfile).filter_by(household_id=hh.id).order_by(VoiceProfile.created_at).all()
    return {"voices": [_voice_out(v) for v in rows], "reading_script": VOICE_SCRIPT}


@router.post("/voices", status_code=201)
def create_voice(file: UploadFile = File(...), label: str = Form(...), consent: bool = Form(False),
                 db: Session = Depends(get_db), parent: Parent = Depends(current_parent),
                 hh: Household = Depends(current_household)):
    """Create a cloned voice profile from an uploaded recording. Consent is REQUIRED:
    the parent affirms they own/may use this voice for private family use. The clip is transcoded
    to WAV and stored household-scoped under TB_DATA_DIR/voices/<household_id>/."""
    if not consent:
        raise HTTPException(422, "consent is required to create a cloned voice")
    s = get_settings()
    vid = new_id()
    vdir = s.voices_dir / hh.id
    vdir.mkdir(parents=True, exist_ok=True)
    raw = vdir / f"{vid}.upload"
    wav = vdir / f"{vid}.wav"
    data = file.file.read(MAX_VOICE_UPLOAD_BYTES + 1)
    if len(data) > MAX_VOICE_UPLOAD_BYTES:
        raise HTTPException(413, "recording too large (max 20 MB)")
    raw.write_bytes(data)
    ok = _transcode_to_wav(raw, wav)
    try:
        raw.unlink()
    except OSError:
        pass
    if not ok or not wav.is_file() or wav.stat().st_size < 2000:
        raise HTTPException(400, "could not process that audio — try a clear recording (m4a/wav/mp3)")
    vp = VoiceProfile(id=vid, household_id=hh.id, label=label.strip() or "Parent voice",
                      kind="clone", engine="chatterbox", ref_audio_path=str(wav),
                      consent_by=parent.id, consent_at=utcnow_iso())
    db.add(vp); db.commit()
    return _voice_out(vp)


@router.delete("/voices/{voice_id}", status_code=204)
def delete_voice(voice_id: str, db: Session = Depends(get_db),
                 hh: Household = Depends(current_household)):
    vp = db.query(VoiceProfile).filter_by(id=voice_id, household_id=hh.id).one_or_none()
    if vp is None:
        raise HTTPException(404, "voice not found")
    # unset it from any child that uses it, delete the clip, then the profile
    for c in db.query(Child).filter_by(narrator_voice=voice_id).all():
        c.narrator_voice = None
    if vp.ref_audio_path:
        try:
            Path(vp.ref_audio_path).unlink()
        except OSError:
            pass
    db.delete(vp); db.commit()
    return Response(status_code=204)


# ----------------------------- requests -----------------------------
def create_story_request(db: Session, body: S.RequestCreate, source_override: str | None = None) -> StoryRequest:
    char_ids = list(body.character_ids)
    # invented cast (seed_stories.py path): create Character rows on the fly, skip IP names
    for ic in body.characters:
        if is_known_ip(ic.name):
            continue
        c = Character(child_id=body.child_id, name=ic.name, archetype=ic.archetype)
        db.add(c); db.flush()
        char_ids.append(c.id)
    # fold the optional setting hint into the special request (no schema column needed)
    special = body.special_request
    if body.setting_hint:
        special = (f"{special}. " if special else "") + f"Set it in {body.setting_hint}."
    source = source_override or ("seed_auto" if body.auto_approve else ("seed" if body.seed_batch else "parent"))
    req = StoryRequest(
        child_id=body.child_id, story_type=body.story_type,
        character_ids=json.dumps(char_ids), special_request=special,
        target_date=body.target_date, priority=body.priority,
        gen_model_override=(body.gen_model_override or "").strip() or None,
        status="queued", source=source,
        continue_series=1 if body.continue_series else 0,
        long_form=1 if body.long_form else 0,
        ambient=body.ambient,
    )
    db.add(req); db.commit(); db.refresh(req)
    return req


@router.post("/requests", response_model=S.StoryRequestOut, status_code=201)
def create_request(body: S.RequestCreate, db: Session = Depends(get_db),
                   hh: Household = Depends(current_household)):
    _child_or_404(db, hh, body.child_id)
    if body.character_ids:  # only this family's characters may be pulled into a story
        hh_child_ids = [c.id for c in db.query(Child.id).filter_by(household_id=hh.id).all()]
        ok = db.query(Character.id).filter(Character.id.in_(body.character_ids),
                                           Character.child_id.in_(hh_child_ids)).count()
        if ok != len(set(body.character_ids)):
            raise HTTPException(422, "unknown character for this family")
    req = create_story_request(db, body)
    return _request_out(req)


def _request_out(req: StoryRequest) -> S.StoryRequestOut:
    return S.StoryRequestOut(
        id=req.id, child_id=req.child_id, story_type=req.story_type,
        character_ids=json.loads(req.character_ids or "[]"), special_request=req.special_request,
        target_date=req.target_date, priority=req.priority, status=req.status,
        source=req.source, gen_model_override=req.gen_model_override, created_at=req.created_at,
    )


@router.get("/requests", response_model=list[S.StoryRequestOut])
def list_requests(status: str | None = None, db: Session = Depends(get_db),
                  hh: Household = Depends(current_household)):
    child_ids = [c.id for c in db.query(Child).filter_by(household_id=hh.id).all()]
    q = db.query(StoryRequest).filter(StoryRequest.child_id.in_(child_ids))
    if status:
        q = q.filter(StoryRequest.status == status)
    return [_request_out(r) for r in q.order_by(StoryRequest.created_at.desc()).all()]


# ----------------------------- admin: models & settings (owner-only) -----------------------------
def _llamaswap_root(s) -> str:
    base = s.TB_LLM_URL.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


@router.get("/models", response_model=S.ModelsOut)
def list_models(_a: Parent = Depends(require_admin), hh: Household = Depends(current_household)):
    """Model ids from the OpenAI-compatible gateway's /models (+ Opus when enabled). Drives the
    Admin/New dropdowns."""
    s = get_settings()
    local: list[S.ModelInfo] = []
    try:
        with httpx.Client(timeout=8.0) as c:
            hdr = {"Authorization": f"Bearer {s.TB_LLM_API_KEY}"} if s.TB_LLM_API_KEY else None
            data = c.get(f"{s.TB_LLM_URL.rstrip('/')}/models", headers=hdr).json()
        for m in data.get("data", []):
            local.append(S.ModelInfo(id=m.get("id", ""), state=None))
    except Exception:
        pass  # if the gateway is down the dropdown is just empty; not fatal
    cloud: list[S.ModelInfo] = []
    if s.TB_ENABLE_OPUS:
        cloud.append(S.ModelInfo(id=s.TB_OPUS_MODEL or "opus", state=None))
    return S.ModelsOut(local=local, cloud=cloud)


# ----------------------------- voice audition / curation (Admin) -----------------------------
def _voice_sample_path(s, voice: str):
    from pathlib import Path
    safe = "".join(c for c in voice if c.isalnum() or c in "._-")  # no path traversal
    return Path(s.data_dir) / "voice_samples" / f"{safe}.mp3"


@router.get("/admin/voices")
def admin_voices(_a: Parent = Depends(require_admin), db: Session = Depends(get_db)):
    """Every predefined TTS voice + whether it's in the curated pool + whether a 15s sample exists.
    Drives the Admin audition list so the operator can keep only the good ones (kills random-voice
    roulette)."""
    from pathlib import Path
    s = get_settings()
    eff = settings_store.effective(db, s)
    # Live list from the engine — but the TTS engine is STOPPED when idle (to free the GPU), so this
    # often fails. Fall back to / union with the voices we already have 15s samples for on disk, plus
    # the curated pool, so the audition list survives whether or not Chatterbox is running.
    try:
        voices = list(get_engine(s, name=eff.tts_engine).list_voices())
    except Exception:
        voices = []
    curated = set(settings_store.voice_pool(db))
    sample_dir = Path(s.data_dir) / "voice_samples"
    on_disk = sorted(p.name[:-4] for p in sample_dir.glob("*.mp3")) if sample_dir.is_dir() else []
    seen = set(voices)
    for v in on_disk + sorted(curated):  # append anything the engine didn't return
        if v not in seen:
            voices.append(v); seen.add(v)
    return {
        "voices": [{"id": v, "enabled": (v in curated) if curated else True,
                    "has_sample": _voice_sample_path(s, v).is_file()} for v in voices],
        "curated": bool(curated),
    }


class VoicePoolIn(BaseModel):
    enabled: list[str]


@router.put("/admin/voices/pool")
def admin_set_voice_pool(body: VoicePoolIn, _a: Parent = Depends(require_admin), db: Session = Depends(get_db)):
    settings_store.set_voice_pool(db, body.enabled)
    return {"ok": True, "count": len(body.enabled)}


@router.get("/admin/voice_sample")
def admin_voice_sample(voice: str, _a: Parent = Depends(require_admin)):
    from fastapi.responses import FileResponse
    s = get_settings()
    p = _voice_sample_path(s, voice)
    if not p.is_file():
        raise HTTPException(404, "no sample (run scripts/gen_voice_samples.py)")
    return FileResponse(str(p), media_type="audio/mpeg")


@router.get("/admin/logs")
def admin_logs(unit: str = "tellybear-api", lines: int = 200, _a: Parent = Depends(require_admin)):
    if unit not in LOG_UNITS:
        raise HTTPException(400, "unknown log unit")
    n = max(1, min(lines, 1000))
    try:
        p = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(n), "--no-pager", "--output", "cat"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "log read timed out")
    except OSError as e:
        raise HTTPException(500, f"could not read logs: {e}")
    if p.returncode != 0:
        raise HTTPException(502, p.stderr.strip() or "journalctl failed")
    return {"unit": unit, "lines": n, "text": p.stdout}


@router.get("/system/loaded")
def system_loaded(_a: Parent = Depends(require_admin), hh: Household = Depends(current_household)):
    """Passthrough of llama-swap /running so Admin can show what's currently in VRAM."""
    s = get_settings()
    try:
        with httpx.Client(timeout=8.0) as c:
            return c.get(f"{_llamaswap_root(s)}/running").json()
    except Exception as e:
        return {"running": [], "error": str(e)}


def _settings_out(db: Session) -> S.SettingsOut:
    s = get_settings()
    return S.SettingsOut(**settings_store.effective(db, s).as_dict(),
                         chatterbox_url_default=s.TB_CHATTERBOX_URL, kokoro_url_default=s.TB_KOKORO_URL)


@router.get("/settings", response_model=S.SettingsOut)
def get_settings_effective(_a: Parent = Depends(require_admin), db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    return _settings_out(db)


@router.put("/settings", response_model=S.SettingsOut)
def put_settings(body: S.SettingsPatch, _a: Parent = Depends(require_admin), db: Session = Depends(get_db),
                 hh: Household = Depends(current_household)):
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    for key in settings_store.URL_KEYS:
        url = (updates.get(key) or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            raise HTTPException(422, f"{key} must start with http:// or https://")
    # validate model ids against the live list (best-effort; skip if llama-swap unreachable)
    if "active_gen_model" in updates or "active_judge_model" in updates:
        known = {m.id for m in list_models(hh).local} | {m.id for m in list_models(hh).cloud}
        for key in ("active_gen_model", "active_judge_model"):
            if key in updates and known and updates[key] not in known:
                raise HTTPException(422, f"unknown model id '{updates[key]}'")
    settings_store.set_many(db, updates)
    return _settings_out(db)


@router.get("/admin/tts-health")
def admin_tts_health(_a: Parent = Depends(require_admin)):
    """Can the server reach each TTS server right now (with the current Admin / env URLs)?"""
    s = get_settings()
    out = {}
    for name, path in (("chatterbox", "/api/model-info"), ("kokoro", "/health")):
        url = settings_store.tts_url(name, s)
        info: dict = {"url": url, "ok": False}
        if url:
            try:
                with httpx.Client(timeout=5.0) as c:
                    r = c.get(f"{url}{path}")
                info["ok"] = r.status_code == 200
                if name == "chatterbox" and info["ok"]:
                    # with TB_GPU_HANDOFF the model is unloaded between stories; that's normal
                    info["model_loaded"] = bool(r.json().get("loaded"))
            except Exception as e:
                info["error"] = type(e).__name__
        out[name] = info
    return out


@router.get("/stats", response_model=S.StatsOut)
def stats(_a: Parent = Depends(require_admin), db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    child_ids = [c.id for c in db.query(Child).filter_by(household_id=hh.id).all()]
    stories = db.query(Story).filter(Story.child_id.in_(child_ids)).all() if child_ids else []
    by_model: dict[str, int] = {}
    by_engine: dict[str, int] = {}
    ready = failed = rejected = 0
    for st in stories:
        if st.status == "ready":
            ready += 1
            by_model[st.gen_model] = by_model.get(st.gen_model, 0) + 1
            asset = db.query(StoryAsset).filter_by(story_id=st.id, kind="mp3").first()
            engine = (asset.voice.split(":")[0] if asset and asset.voice else "unknown")
            by_engine[engine] = by_engine.get(engine, 0) + 1
        elif st.status == "failed":
            failed += 1
        elif st.status == "rejected":
            rejected += 1
    # per-model generation time (avg/median) for the Admin speed comparison
    times: dict[str, list[float]] = {}
    for st in stories:
        if st.status == "ready" and st.generation_seconds:
            times.setdefault(st.gen_model, []).append(st.generation_seconds)
    gen_time = {"by_model": {}}
    for model, xs in times.items():
        xs.sort()
        median = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
        gen_time["by_model"][model] = {"avg_s": round(sum(xs) / len(xs), 1), "median_s": round(median, 1)}
    return S.StatsOut(total_ready=ready, total_failed=failed, total_rejected=rejected,
                      by_gen_model=by_model, by_engine=by_engine, gen_time=gen_time)


@router.get("/library", response_model=list[S.LibraryItem])
def library(db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    """Operator view: every story (any status) for the household, newest first."""
    children = {c.id: c.name for c in db.query(Child).filter_by(household_id=hh.id).all()}
    rows = (db.query(Story).filter(Story.child_id.in_(list(children) or [""]))
            .order_by(Story.created_at.desc()).all())
    nn = _nearest_neighbors(db, list(children))  # story_id -> (sim, other_title) per child
    items = []
    for st in rows:
        asset = db.query(StoryAsset).filter_by(story_id=st.id, kind="mp3").first()
        engine = asset.voice.split(":")[0] if asset and asset.voice else None
        near = nn.get(st.id)
        items.append(S.LibraryItem(
            id=st.id, title=st.title, child_id=st.child_id,
            child_name=children.get(st.child_id, "?"), status=st.status,
            gen_model=st.gen_model, engine=engine, generation_seconds=st.generation_seconds,
            duration_seconds=asset.duration_seconds if asset else None, created_at=st.created_at,
            nearest_sim=round(near[0], 3) if near else None,
            nearest_title=near[1] if near else None,
            rating=st.rating, play_count=st.play_count or 0, favorited=bool(st.favorited),
            reject_reason=st.reject_reason,
            craft_overall=_craft_overall(st),
        ))
    return items


def _nearest_neighbors(db: Session, child_ids: list[str]) -> dict[str, tuple[float, str]]:
    """For each story with an embedding, its single closest OTHER story of the same child
    (cosine, title). Brute force within each child — fine for a household's corpus."""
    from tellybear.variety import embed as _embed
    titles = {s.id: s.title for s in db.query(Story.id, Story.title)
              .filter(Story.child_id.in_(child_ids or [""])).all()}
    by_child: dict[str, list[tuple[str, list[float]]]] = {}
    for row in (db.query(StoryEmbedding)
                .filter(StoryEmbedding.child_id.in_(child_ids or [""])).all()):
        by_child.setdefault(row.child_id, []).append((row.story_id, json.loads(row.vec)))
    out: dict[str, tuple[float, str]] = {}
    for vecs in by_child.values():
        for sid, v in vecs:
            best: tuple[float, str] | None = None
            for osid, ov in vecs:
                if osid == sid or len(ov) != len(v):
                    continue
                sc = _embed.cosine(v, ov)
                if best is None or sc > best[0]:
                    best = (sc, titles.get(osid, "?"))
            if best is not None:
                out[sid] = best
    return out


@router.delete("/stories/{story_id}", status_code=204)
def delete_story(story_id: str, db: Session = Depends(get_db),
                 hh: Household = Depends(current_household)):
    """Permanently delete a story + cascade: asset files+rows, safety_reviews,
    playback_events, the story row. Owner-only."""
    _story_in_household(db, hh, story_id)  # household scope check
    _cascade_delete_story(db, story_id)
    db.commit()
    return Response(status_code=204)


# ----------------------------- stories / approval -----------------------------
IN_PROGRESS_STATUSES = ("queued", "generating", "judging", "synthesizing")


def _craft_overall(story: Story) -> float | None:
    """Pull the headline craft score (0..10) out of story.craft_json."""
    if not story.craft_json:
        return None
    try:
        return json.loads(story.craft_json).get("overall")
    except Exception:
        return None


def _summary_out(db: Session, story: Story) -> S.StorySummaryOut:
    mp3 = db.query(StoryAsset).filter_by(story_id=story.id, kind="mp3").first()
    engine = mp3.voice.split(":")[0] if mp3 and mp3.voice else None
    return S.StorySummaryOut(
        id=story.id, title=story.title, summary=story.summary, status=story.status,
        stage=story.stage, child_id=story.child_id, safety=_safety_out(db, story.id),
        duration_seconds=mp3.duration_seconds if mp3 else None,
        generation_seconds=story.generation_seconds, gen_model=story.gen_model, engine=engine,
        created_at=story.created_at, rating=story.rating, play_count=story.play_count or 0,
        favorited=bool(story.favorited), reject_reason=story.reject_reason,
        craft_overall=_craft_overall(story),
    )


@router.get("/stories", response_model=list[S.StorySummaryOut])
def list_stories(child_id: str | None = None, status: str | None = None,
                 db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    child_ids = [c.id for c in db.query(Child).filter_by(household_id=hh.id).all()]
    q = db.query(Story).filter(Story.child_id.in_(child_ids))
    if child_id:
        q = q.filter(Story.child_id == child_id)
    if status == "in_progress":
        q = q.filter(Story.status.in_(IN_PROGRESS_STATUSES))
    elif status:
        q = q.filter(Story.status == status)
    return [_summary_out(db, s) for s in q.order_by(Story.created_at.desc()).all()]


@router.get("/stories/{story_id}", response_model=S.StoryDetailOut)
def get_story(story_id: str, db: Session = Depends(get_db), hh: Household = Depends(current_household)):
    story, _ = _story_in_household(db, hh, story_id)
    assets = db.query(StoryAsset).filter_by(story_id=story.id).all()
    base = _summary_out(db, story)
    return S.StoryDetailOut(
        **base.model_dump(),
        age_band=story.age_band, story_json=json.loads(story.story_json),
        word_count=story.word_count,
        assets=[S.AssetOut(kind=a.kind, duration_seconds=a.duration_seconds, voice=a.voice)
                for a in assets],
    )


@router.post("/stories/{story_id}/approve", response_model=S.StorySummaryOut)
def approve_story(story_id: str, body: S.ApproveRequest, db: Session = Depends(get_db),
                  hh: Household = Depends(current_household)):
    story, child = _story_in_household(db, hh, story_id)
    if story.status != "needs_approval":
        raise HTTPException(409, f"story is '{story.status}', not 'needs_approval'")
    # Only a CONTENT-category concern locks one-tap approve. A verdict 'fail' with zero flagged
    # categories is a non-content gate (e.g. word count out of band) — safe to play, approvable
    # without an override. This is why "Needs a look — 0 flags" no longer demands an override.
    verdict = _safety_out(db, story.id)
    content_concern = bool(verdict) and any(v == "concern" for v in (verdict.categories or {}).values())
    if content_concern and not body.override_fail:
        raise HTTPException(403, "safety flagged a content concern; set override_fail=true to approve anyway")
    story.status = "approved"  # the worker claims approved stories and synthesizes them
    db.commit(); db.refresh(story)
    return _summary_out(db, story)


@router.post("/stories/{story_id}/rate", response_model=S.StorySummaryOut)
def rate_story(story_id: str, body: S.RateRequest, db: Session = Depends(get_db),
               hh: Household = Depends(current_household)):
    """Parent quality rating, 1–5 stars. Repeatable — overwrites. Calibrates the
    craft judge; sparse is fine."""
    story, _ = _story_in_household(db, hh, story_id)
    story.rating = body.rating
    story.rating_note = body.note
    db.commit(); db.refresh(story)
    return _summary_out(db, story)


def _store_reject_reason(story: Story, body: S.RejectRequest) -> None:
    """Persist the reject/regenerate reason chip + optional note. The single most
    informative label we used to throw away. Unknown reasons fall back to 'other'."""
    reason = (body.reason or "other").strip().lower()
    story.reject_reason = reason if reason in S.REJECT_REASONS else "other"
    story.reject_note = body.note


@router.post("/stories/{story_id}/reject", response_model=S.StorySummaryOut)
def reject_story(story_id: str, body: S.RejectRequest, db: Session = Depends(get_db),
                 hh: Household = Depends(current_household)):
    story, _ = _story_in_household(db, hh, story_id)
    story.status = "rejected"
    _store_reject_reason(story, body)
    db.commit(); db.refresh(story)
    return _summary_out(db, story)


@router.post("/stories/{story_id}/regenerate", status_code=202)
def regenerate_story(story_id: str, body: S.RejectRequest = S.RejectRequest(),
                     db: Session = Depends(get_db),
                     hh: Household = Depends(current_household)):
    """Reject this story and re-queue a fresh attempt (a request may produce a new story
    version). Reuses the original request when present so the same cast/type/setting is kept; the
    worker generates a new story with the current prompts. Runs at 'now' priority. The reason chip
 is stored on the rejected version."""
    story, _ = _story_in_household(db, hh, story_id)
    story.status = "rejected"
    _store_reject_reason(story, body)
    req = db.get(StoryRequest, story.request_id) if story.request_id else None
    if req is not None:
        req.status = "queued"
        req.priority = "now"
        db.commit()
        return {"request_id": req.id, "status": "queued"}
    # imported/starter story with no request → build a fresh request from its own cast/type
    sj = json.loads(story.story_json)
    chars = [{"name": c.get("name"), "archetype": c.get("archetype")}
             for c in sj.get("characters", []) if c.get("name")]
    char_ids: list[str] = []
    for ic in chars:
        if is_known_ip(ic["name"]):
            continue
        c = Character(child_id=story.child_id, name=ic["name"], archetype=ic.get("archetype"))
        db.add(c); db.flush(); char_ids.append(c.id)
    new = StoryRequest(child_id=story.child_id, story_type=sj.get("story_type", "bedtime_calm"),
                       character_ids=json.dumps(char_ids), special_request=None,
                       priority="now", status="queued", source="parent")
    db.add(new); db.commit()
    return {"request_id": new.id, "status": "queued"}


@router.post("/stories/{story_id}/preview-audio")
def preview_audio(story_id: str, db: Session = Depends(get_db),
                  hh: Household = Depends(current_household)):
    story, child = _story_in_household(db, hh, story_id)
    s = get_settings()
    eff = settings_store.effective(db, s)
    engine = get_engine(s, name=eff.tts_engine)
    if not engine.health() and engine.name != "kokoro":
        engine = get_engine(s, name="kokoro")
    if engine.name == "chatterbox":
        try:
            from tellybear.worker import gpu
            gpu.prepare_for_tts(s)
        except Exception:
            pass
    parsed = StoryJSON.model_validate_json(story.story_json)
    from tellybear.tts.synth import prepare_text
    sample = prepare_text(" ".join(seg.text for seg in parsed.segments[:2]) or parsed.title)
    voice = eff.tts_voice
    if engine.supports_ipa:
        sample = pron_inject.inject(sample, hh.id, db)
        voice = s.TB_KOKORO_VOICE
    exaggeration = eff.tts_exaggeration if engine.supports_emotion else None
    audio = engine.speak(sample, voice=voice, speed=s.TB_KOKORO_SPEED if engine.name == "kokoro" else 1.0,
                         exaggeration=exaggeration, fmt="mp3")
    return Response(content=audio, media_type="audio/mpeg")
