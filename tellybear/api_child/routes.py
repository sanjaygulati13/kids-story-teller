"""Child API. Mounted at /api/child by main.py.

Dead-simple read + playback surface. No auth in v1 (the parent points the device at the
right child URL). Audio is range-enabled for seek/AirPlay. Playback auto-marks a story
"played" on a `complete` event or a `progress` with percent>=40.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from tellybear.api_parent.routes import create_story_request
from tellybear.api_parent.schemas import RequestCreate
from tellybear.core.config import get_settings
from tellybear.core import settings_store
from tellybear.core.stt import transcribe
from tellybear.db.base import get_db
from tellybear.db.models import Child, PlaybackEvent, Story, StoryAsset, StoryRequest

router = APIRouter()

PLAYED_PERCENT_THRESHOLD = 40.0  # a story counts as played once 40% has been heard
MAX_VOICE_BYTES = 4 * 1024 * 1024
# A voice request skips the parent gate (auto-approve after a double safety judge), so cap how many
# a device — or anyone holding the child's link — can trigger.
MAX_VOICE_REQUESTS_PER_DAY = 6


class CatalogItem(BaseModel):
    story_id: str
    title: str
    played: bool
    duration_seconds: float | None
    story_type: str | None = None
    created_at: str | None = None


class NextStoryOut(BaseModel):
    story_id: str
    title: str
    audio_url: str
    duration_seconds: float | None


class AnnouncementOut(BaseModel):
    available: bool
    audio_url: str
    unplayed_count: int


class VoiceRequestOut(BaseModel):
    request_id: str
    transcript: str


class VoiceStoryOut(BaseModel):
    ready: bool
    story_id: str | None
    title: str | None
    audio_url: str | None
    duration_seconds: float | None


class PlaybackIn(BaseModel):
    child_id: str
    story_id: str
    event: str  # play | pause | progress | complete
    position_seconds: float | None = None
    percent: float | None = None


class FavoriteIn(BaseModel):
    story_id: str


def _is_played(db: Session, child_id: str, story_id: str) -> bool:
    q = db.query(PlaybackEvent).filter(
        PlaybackEvent.child_id == child_id,
        PlaybackEvent.story_id == story_id,
        or_(
            PlaybackEvent.event == "complete",
            PlaybackEvent.percent >= PLAYED_PERCENT_THRESHOLD,
        ),
    )
    return db.query(q.exists()).scalar()


class ChildProfile(BaseModel):
    name: str


def _resolve_child(db: Session, key: str) -> Child:
    """Resolve a child by its public_slug (the capability id in /play/{slug}) ONLY — never by the
    internal id, so rotating the slug really revokes a leaked link."""
    child = db.query(Child).filter_by(public_slug=key).one_or_none()
    if child is None:
        raise HTTPException(404, "not found")
    return child


@router.get("/profile/{key}", response_model=ChildProfile)
def child_profile(key: str, db: Session = Depends(get_db)):
    """Minimal public profile for the player greeting (name only — no settings, no list)."""
    child = _resolve_child(db, key)
    return ChildProfile(name=child.name)


@router.get("/catalog/{key}", response_model=list[CatalogItem])
def catalog(key: str, pick: str | None = None, db: Session = Depends(get_db)):
    child = _resolve_child(db, key)
    child_id = child.id
    stories = (
        db.query(Story)
        .filter(Story.child_id == child_id, Story.status == "ready")
        .order_by(Story.created_at.desc())
        .all()
    )
    items: list[CatalogItem] = []
    for s in stories:
        mp3 = db.query(StoryAsset).filter_by(story_id=s.id, kind="mp3").first()
        story_type = None
        if s.request_id:
            req = db.get(StoryRequest, s.request_id)
            story_type = req.story_type if req else None
        items.append(CatalogItem(
            story_id=s.id, title=s.title, played=_is_played(db, child_id, s.id),
            duration_seconds=mp3.duration_seconds if mp3 else None, story_type=story_type,
            created_at=s.created_at,
        ))
    # unplayed first, then by recency (already sorted by recency above) — stable sort by played
    items.sort(key=lambda it: it.played)
    # server fallback for the 🎲 Surprise me button: one random unplayed (else any)
    if pick == "random_unplayed" and items:
        pool = [it for it in items if not it.played] or items
        return [pool[secrets.randbelow(len(pool))]]
    return items


def _local_day_bounds_utc_iso() -> tuple[str, str]:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def _completed_today(db: Session, child_id: str) -> int:
    start, end = _local_day_bounds_utc_iso()
    return db.query(PlaybackEvent).filter(
        PlaybackEvent.child_id == child_id,
        PlaybackEvent.event == "complete",
        PlaybackEvent.created_at >= start,
        PlaybackEvent.created_at < end,
    ).count()


@router.get("/next/{slug}", response_model=NextStoryOut)
def next_story(slug: str, db: Session = Depends(get_db)):
    child = _resolve_child(db, slug)
    stories = (
        db.query(Story)
        .filter(Story.child_id == child.id, Story.status == "ready")
        .order_by(Story.created_at.desc())
        .all()
    )
    if not stories:
        raise HTTPException(404, "story not found")

    limit = max(1, settings_store.effective(db).daily_story_limit)
    if _completed_today(db, child.id) >= limit:
        raise HTTPException(429, "daily limit reached")

    pool = [s for s in stories if not _is_played(db, child.id, s.id)] or stories
    story = pool[secrets.randbelow(len(pool))]
    mp3 = db.query(StoryAsset).filter_by(story_id=story.id, kind="mp3").first()
    return NextStoryOut(
        story_id=story.id,
        title=story.title,
        audio_url=f"/api/child/audio/{story.id}?slug={slug}",
        duration_seconds=mp3.duration_seconds if mp3 else None,
    )


def _announcement_path(slug: str) -> Path:
    return get_settings().data_dir / "announcements" / f"{slug}.mp3"


@router.get("/announcement/{slug}", response_model=AnnouncementOut)
def announcement(slug: str, db: Session = Depends(get_db)):
    child = _resolve_child(db, slug)
    stories = (
        db.query(Story)
        .filter(Story.child_id == child.id, Story.status == "ready")
        .order_by(Story.created_at.desc())
        .all()
    )
    unplayed_count = sum(1 for s in stories if not _is_played(db, child.id, s.id))
    path = _announcement_path(slug)
    return AnnouncementOut(
        available=path.is_file(),
        audio_url=f"/api/child/announcement/{slug}/audio",
        unplayed_count=unplayed_count,
    )


@router.get("/announcement/{slug}/audio")
def announcement_audio(slug: str, db: Session = Depends(get_db)):
    _resolve_child(db, slug)
    path = _announcement_path(slug)
    if not path.is_file():
        raise HTTPException(404, "announcement not found")
    return FileResponse(str(path), media_type="audio/mpeg")


@router.post("/voice/{slug}", response_model=VoiceRequestOut)
def voice_request(slug: str, audio: UploadFile = File(...), db: Session = Depends(get_db)):
    child = _resolve_child(db, slug)
    in_flight = db.query(StoryRequest).filter(
        StoryRequest.child_id == child.id, StoryRequest.source == "voice",
        StoryRequest.status.in_(("queued", "generating"))).count()
    if in_flight:
        raise HTTPException(429, "a story is already being made")
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    recent = db.query(StoryRequest).filter(
        StoryRequest.child_id == child.id, StoryRequest.source == "voice",
        StoryRequest.created_at >= since).count()
    if recent >= MAX_VOICE_REQUESTS_PER_DAY:
        raise HTTPException(429, "that's enough new stories for today")
    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in {".wav", ".mp3"}:
        content_type = (audio.content_type or "").lower()
        if content_type == "audio/wav" or content_type == "audio/x-wav":
            suffix = ".wav"
        elif content_type in {"audio/mpeg", "audio/mp3"}:
            suffix = ".mp3"
        else:
            raise HTTPException(422, "audio must be WAV or MP3")

    audio_bytes = audio.file.read(MAX_VOICE_BYTES + 1)
    if len(audio_bytes) > MAX_VOICE_BYTES:
        raise HTTPException(413, "audio clip too large")
    transcript = transcribe(audio_bytes, suffix=suffix)
    if not transcript:
        raise HTTPException(422, "no speech detected")

    req = create_story_request(
        db,
        RequestCreate(
            child_id=child.id,
            story_type="bedtime_calm",
            special_request=transcript,
            priority="now",
            auto_approve=True,
        ),
        source_override="voice",
    )
    return VoiceRequestOut(request_id=req.id, transcript=transcript)


@router.get("/{slug}/request/{request_id}", response_model=VoiceStoryOut)
def voice_story(slug: str, request_id: str, db: Session = Depends(get_db)):
    child = _resolve_child(db, slug)
    story = (
        db.query(Story)
        .filter(Story.child_id == child.id, Story.request_id == request_id)
        .one_or_none()
    )
    if story is None or story.status != "ready":
        return VoiceStoryOut(
            ready=False,
            story_id=None,
            title=None,
            audio_url=None,
            duration_seconds=None,
        )

    mp3 = db.query(StoryAsset).filter_by(story_id=story.id, kind="mp3").first()
    if mp3 is None:
        return VoiceStoryOut(
            ready=False,
            story_id=None,
            title=None,
            audio_url=None,
            duration_seconds=None,
        )

    return VoiceStoryOut(
        ready=True,
        story_id=story.id,
        title=story.title,
        audio_url=f"/api/child/audio/{story.id}?slug={slug}",
        duration_seconds=mp3.duration_seconds,
    )


@router.post("/{slug}/favorite", status_code=204)
def favorite_story(slug: str, body: FavoriteIn, db: Session = Depends(get_db)):
    child = _resolve_child(db, slug)
    story = db.get(Story, body.story_id)
    if story is None or story.child_id != child.id:
        raise HTTPException(404, "story not found")
    story.favorited = True
    db.commit()
    return Response(status_code=204)


def _authorize_audio(db: Session, request: Request, story: Story, slug: str | None) -> bool:
    """Audio is reachable EITHER with a valid capability slug for the story's child (the public
    child player passes ?slug=…), OR by an authenticated parent in the story's household (the
    parent app's Listen). A guessed story_id with neither → denied (household isolation)."""
    if slug:
        sc = db.query(Child).filter_by(public_slug=slug).one_or_none()
        if sc is not None and sc.id == story.child_id:
            return True
    try:
        from tellybear.core import auth
        parent = auth._parent_from_request(db, request)
        if parent is not None:
            child = db.get(Child, story.child_id)
            if child is not None and child.household_id == parent.household_id:
                return True
    except Exception:
        pass
    return False


def _mp3_path(db: Session, request: Request, story_id: str, slug: str | None) -> Path:
    story = db.get(Story, story_id)
    if story is None or story.status != "ready":
        raise HTTPException(404, "audio not found")
    if not _authorize_audio(db, request, story, slug):
        raise HTTPException(404, "audio not found")
    asset = db.query(StoryAsset).filter_by(story_id=story_id, kind="mp3").first()
    if asset is None or not Path(asset.path).is_file():
        raise HTTPException(404, "audio not found")
    return Path(asset.path)


@router.get("/audio/{story_id}")
def audio(story_id: str, request: Request, slug: str | None = None, db: Session = Depends(get_db),
          range_header: str | None = Header(default=None, alias="Range")):
    path = _mp3_path(db, request, story_id, slug)
    file_size = path.stat().st_size
    media_type = "audio/mpeg"

    if range_header and range_header.startswith("bytes="):
        try:
            rng = range_header.split("=", 1)[1]
            start_s, _, end_s = rng.partition("-")
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else file_size - 1
            else:  # suffix range "bytes=-N" = the last N bytes
                start = file_size - int(end_s)
                end = file_size - 1
        except ValueError:
            raise HTTPException(416, "invalid Range header")
        start = max(0, start)
        end = min(end, file_size - 1)
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
        length = end - start + 1

        def _iter():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        return StreamingResponse(_iter(), status_code=206, media_type=media_type, headers=headers)

    def _iter_all():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
    return StreamingResponse(_iter_all(), media_type=media_type, headers=headers)


class PlayedIn(BaseModel):
    story_id: str


@router.post("/{slug}/played", status_code=204)
def mark_played(slug: str, body: PlayedIn, db: Session = Depends(get_db)):
    """Dedicated, reliable 'played' mark: the player POSTs this once per play when
    playback first crosses 40% or ends. The `complete` marker (→ `played` boolean) is idempotent,
    but play_count increments on EVERY qualifying play — a re-listen is the strongest
    'good story' signal we have."""
    child = _resolve_child(db, slug)
    story = db.get(Story, body.story_id)
    if story is None or story.child_id != child.id:
        raise HTTPException(404, "story not found")
    already = db.query(PlaybackEvent).filter_by(
        child_id=child.id, story_id=body.story_id, event="complete").first()
    if already is None:
        db.add(PlaybackEvent(child_id=child.id, story_id=body.story_id, event="complete", percent=100.0))
    story.play_count = (story.play_count or 0) + 1
    db.commit()
    return Response(status_code=204)


@router.post("/playback", status_code=204)
def playback(body: PlaybackIn, db: Session = Depends(get_db)):
    # body.child_id is the child's public_slug
    child = _resolve_child(db, body.child_id)
    child_id = child.id
    st = db.get(Story, body.story_id)
    if st is None or st.child_id != child_id:
        raise HTTPException(404, "story not found")

    db.add(PlaybackEvent(
        child_id=child_id, story_id=body.story_id, event=body.event,
        position_seconds=body.position_seconds, percent=body.percent,
    ))

    # auto-mark played: persist a durable 'complete' marker once the threshold is met
    qualifies = body.event == "complete" or (body.percent is not None and body.percent >= PLAYED_PERCENT_THRESHOLD)
    if qualifies and body.event != "complete":
        already = db.query(PlaybackEvent).filter_by(
            child_id=child_id, story_id=body.story_id, event="complete"
        ).first()
        if already is None:
            db.add(PlaybackEvent(child_id=child_id, story_id=body.story_id,
                                 event="complete", percent=body.percent,
                                 position_seconds=body.position_seconds))
    db.commit()
    return Response(status_code=204)
