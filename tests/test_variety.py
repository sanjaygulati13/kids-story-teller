"""Variety guard: cosine math, story-text fingerprint, and graceful no-op when the
embedder is unavailable (the dev box may have no fastembed wheel)."""
from __future__ import annotations

import math

from tellybear.variety import embed


def test_cosine_identicals_and_orthogonal():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert math.isclose(embed.cosine(a, a), 1.0)
    assert math.isclose(embed.cosine(a, b), 0.0)
    # mismatched dims never raise — they score 0 (different embedding model in corpus)
    assert embed.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_story_text_combines_title_summary_body():
    d = {"title": "The Blue Boat", "summary": "a calm sail",
         "segments": [{"text": "Once there was a boat."}, {"text": "It floated home."}]}
    text = embed.story_text(d)
    assert "The Blue Boat" in text
    assert "calm sail" in text
    assert "floated home" in text


def test_embed_text_none_when_unavailable():
    """If fastembed isn't installed, embed_text returns None and callers treat the guard as off
    — generation must never crash because the embedder is missing."""
    if embed.available("BAAI/bge-small-en-v1.5"):
        import types
        s = types.SimpleNamespace(TB_EMBED_MODEL="BAAI/bge-small-en-v1.5")
        vec = embed.embed_text("a small calm bedtime story about a boat", s)
        assert vec is not None and len(vec) > 0
        assert math.isclose(sum(x * x for x in vec), 1.0, rel_tol=1e-3)  # L2-normalized
    else:
        import types
        s = types.SimpleNamespace(TB_EMBED_MODEL="BAAI/bge-small-en-v1.5")
        assert embed.embed_text("anything", s) is None
