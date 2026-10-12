"""Unit tests: parser (valid / invalid Story JSON)."""
import json

import pytest

from tellybear.worker.parser import ParseError, parse

VALID = {
    "title": "The Sleepy Lighthouse",
    "age_band": "early",
    "characters": [{"name": "Maya", "role": "main"}, {"name": "Pip", "role": "friend", "archetype": "firefly"}],
    "moral": "Quiet courage is real courage.",
    "summary": "Maya helps a lighthouse rest.",
    "segments": [
        {"speaker": "narrator", "text": "Once on a green island..."},
        {"speaker": "Maya", "text": "Don't be afraid, little light."},
        {"speaker": "narrator", "text": "And it grew calm and sleepy."},
    ],
    "estimated_word_count": 12,
}


def test_parse_valid():
    story = parse(json.dumps(VALID))
    assert story.title == "The Sleepy Lighthouse"
    assert story.age_band == "early"
    assert len(story.segments) == 3
    assert story.word_count() > 0


def test_parse_strips_code_fences_and_prose():
    raw = "Here is your story:\n```json\n" + json.dumps(VALID) + "\n```\nThanks!"
    story = parse(raw)
    assert story.title == VALID["title"]


def test_parse_invalid_not_json():
    with pytest.raises(ParseError):
        parse("this is not json at all")


def test_parse_invalid_missing_segments():
    bad = dict(VALID)
    bad.pop("segments")
    with pytest.raises(ParseError):
        parse(json.dumps(bad))


def test_parse_invalid_empty_segments():
    bad = json.loads(json.dumps(VALID))
    bad["segments"] = []
    with pytest.raises(ParseError):
        parse(json.dumps(bad))


def test_parse_invalid_bad_age_band():
    bad = json.loads(json.dumps(VALID))
    bad["age_band"] = "teen"
    with pytest.raises(ParseError):
        parse(json.dumps(bad))


def test_parse_invalid_unknown_speaker():
    bad = json.loads(json.dumps(VALID))
    bad["segments"].append({"speaker": "Dragon", "text": "Roar"})
    with pytest.raises(ParseError):
        parse(json.dumps(bad))
