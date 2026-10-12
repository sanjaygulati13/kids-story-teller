"""Unit tests: banned filter (replace vs block), via the verbatim safety module."""
from tellybear.safety.safety_check import lexical_check


def _story(text: str) -> dict:
    return {
        "title": "t", "age_band": "early",
        "characters": [{"name": "Mira", "role": "main"}],
        "segments": [{"speaker": "narrator", "text": text}],
    }


def test_replacement_substitutes_and_passes():
    story = _story("Mira held her gun and smiled.")
    ok, hits, mutated = lexical_check(story, [{"term": "gun", "replacement": "water blaster"}])
    assert ok is True
    assert "water blaster" in mutated["segments"][0]["text"]
    assert "gun" not in mutated["segments"][0]["text"].lower().replace("water blaster", "")
    assert any(h["term"] == "gun" and h["kind"] == "replaced" for h in hits)


def test_no_replacement_blocks():
    story = _story("Mira held her gun and smiled.")
    ok, hits, _ = lexical_check(story, [{"term": "gun", "replacement": None}])
    assert ok is False
    assert any(h["term"] == "gun" and h["kind"] == "banned_no_replacement" for h in hits)


def test_household_replacement_clears_hard_block():
    # "gun" is in HARD_BLOCK; a household replacement is applied FIRST so it clears the block.
    story = _story("Mira held her gun and smiled.")
    ok, hits, mutated = lexical_check(story, [{"term": "gun", "replacement": "water blaster"}])
    assert ok is True
    assert "gun" not in mutated["segments"][0]["text"].lower().replace("water blaster", "")


def test_hard_block_without_replacement_fails():
    story = _story("There was a kill in the night.")
    ok, hits, _ = lexical_check(story, [])
    assert ok is False
    assert any(h.get("kind") == "hard_block" for h in hits)


def test_whole_word_only_no_partial_hit():
    # "gun" must not trip inside "begun"
    story = _story("The sleepy day had begun softly.")
    ok, hits, _ = lexical_check(story, [{"term": "gun", "replacement": None}])
    assert ok is True
    assert hits == []
