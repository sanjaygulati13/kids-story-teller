"""Story JSON parse + validation.

parse() returns a validated StoryJSON or raises ParseError. The runner catches ParseError,
retries the generation ONCE with prompts/parser_retry.txt, then marks the request failed.
"""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from tellybear.core.types import StoryJSON

_FENCE = re.compile(r"^```[A-Za-z]*\n?|\n?```$")


class ParseError(ValueError):
    pass


def _strip(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = _FENCE.sub("", s)
    # tolerate leading/trailing prose by extracting the outermost JSON object
    if not s.startswith("{"):
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start:end + 1]
    return s.strip()


def parse(raw: str) -> StoryJSON:
    text = _strip(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseError("top-level JSON is not an object")
    try:
        story = StoryJSON.model_validate(data)
    except ValidationError as e:
        raise ParseError(f"schema validation failed: {e}") from e

    # field rule: every segment speaker is "narrator" or a declared character name.
    names = {c.name for c in story.characters} | {"narrator"}
    unknown = sorted({seg.speaker for seg in story.segments} - names)
    if unknown:
        raise ParseError(f"segment speaker(s) not in characters: {unknown}")
    return story
