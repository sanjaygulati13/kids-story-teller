"""Semantic embeddings for the story variety guard.

Goal: reject near-duplicate PLOTS that the prompt-level anti-repetition (recent titles +
openings) can't catch. We embed each story's title+body into a small vector and, at
generation time, compare a candidate against that child's existing vectors with cosine
similarity; too-similar candidates are regenerated.

Design constraints:
  * NO GPU / NO VRAM — the box is tight and the LLM + Chatterbox own the card. The embedder
    runs on CPU via ONNX (`fastembed`), inside the worker process. No llama-swap entry, no
    GPU hand-off. Embedding a ~380-word story is a few ms.
  * Graceful degradation — `fastembed` may be absent (e.g. the dev Mac's Python has no wheel).
    Import is lazy; if it fails the guard becomes a logged no-op and generation proceeds.
  * No vector DB — a child has at most a few hundred stories; brute-force cosine in pure
    Python is instant. Vectors are L2-normalized and stored as JSON in story_embedding.vec.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache

from sqlalchemy.orm import Session

from tellybear.core.config import Settings
from tellybear.core.logging import get_logger
from tellybear.db.models import Story, StoryEmbedding

log = get_logger("variety")

_UNAVAILABLE = False  # set once if fastembed can't be loaded, to avoid log spam


@lru_cache(maxsize=2)
def _model(model_id: str):
    """Lazily construct the fastembed model (downloads to ~/.cache/fastembed on first use).
    Returns None if fastembed isn't installed — callers treat that as 'guard disabled'."""
    global _UNAVAILABLE
    try:
        from fastembed import TextEmbedding
    except Exception as e:  # noqa: BLE001 — any import/runtime issue → disable, don't crash gen
        if not _UNAVAILABLE:
            log.warning("variety guard disabled: fastembed unavailable (%s)", e)
            _UNAVAILABLE = True
        return None
    log.info("loading embedding model %s (CPU/ONNX)", model_id)
    return TextEmbedding(model_name=model_id)


def available(model_id: str) -> bool:
    return _model(model_id) is not None


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n > 0 else vec


def embed_text(text: str, settings: Settings) -> list[float] | None:
    """Return an L2-normalized embedding for `text`, or None if the embedder is unavailable."""
    m = _model(settings.TB_EMBED_MODEL)
    if m is None:
        return None
    vec = next(iter(m.embed([text])))  # numpy array
    return _normalize([float(x) for x in vec.tolist()])


def story_text(story_dict: dict) -> str:
    """The text we fingerprint: title + summary + full body. bge-small truncates at ~512
    tokens (~380 words) — our early-band stories fit; longer ones lose only the tail, which
    is fine for a plot fingerprint."""
    title = story_dict.get("title", "")
    summary = story_dict.get("summary", "") or ""
    body = "\n".join(seg.get("text", "") for seg in story_dict.get("segments", []))
    return f"{title}\n{summary}\n{body}".strip()


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Inputs are L2-normalized, so this is just the dot product."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def nearest(db: Session, child_id: str, vec: list[float],
            exclude_story_id: str | None = None) -> tuple[float, str, str] | None:
    """Highest-cosine existing story for this child. Returns (score, story_id, title) or None
    when the corpus is empty. Brute force — fine for hundreds of vectors per child."""
    rows = db.query(StoryEmbedding).filter(StoryEmbedding.child_id == child_id).all()
    best: tuple[float, str, str] | None = None
    for row in rows:
        if exclude_story_id and row.story_id == exclude_story_id:
            continue
        if row.dim != len(vec):  # different embedding model in the corpus — skip, don't compare
            continue
        score = cosine(vec, json.loads(row.vec))
        if best is None or score > best[0]:
            sid = row.story_id
            st = db.get(Story, sid)
            best = (score, sid, st.title if st else "")
    return best


def store(db: Session, story: Story, vec: list[float], settings: Settings,
          commit: bool = True) -> None:
    """Upsert the fingerprint for a story. No-op-safe if vec is None."""
    if vec is None:
        return
    existing = db.get(StoryEmbedding, story.id)
    payload = json.dumps(vec)
    if existing is None:
        db.add(StoryEmbedding(story_id=story.id, child_id=story.child_id,
                              model=settings.TB_EMBED_MODEL, dim=len(vec), vec=payload))
    else:
        existing.vec = payload
        existing.dim = len(vec)
        existing.model = settings.TB_EMBED_MODEL
        existing.child_id = story.child_id
    if commit:
        db.commit()
