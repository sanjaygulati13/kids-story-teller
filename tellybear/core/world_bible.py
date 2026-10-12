"""Per-child series "world bible": the recurring cast + ongoing facts that make
"another story about Pip the firefly" possible. Stored as JSON on child.world_bible:

    {"characters": [{"name": str, "description": str}], "facts": [str]}

The bible is injected into the prompt when a request is a series continuation, and it auto-grows
from each series story's cast so the parent only has to tick "continue the series" — no manual
data entry required (they can still curate via the editor).
"""
from __future__ import annotations

import json

MAX_CHARACTERS = 8   # keep the injected block small; oldest recurring cast wins, parent prunes
MAX_FACTS = 8


def load(raw: str | None) -> dict:
    """Parse a child's stored bible into a normalized {characters, facts} dict (never raises)."""
    if not raw:
        return {"characters": [], "facts": []}
    try:
        d = json.loads(raw)
    except Exception:
        return {"characters": [], "facts": []}
    chars = []
    for c in (d.get("characters") or []):
        if isinstance(c, dict) and c.get("name"):
            chars.append({"name": str(c["name"]).strip(),
                          "description": str(c.get("description") or "").strip()})
    facts = [str(f).strip() for f in (d.get("facts") or []) if str(f).strip()]
    return {"characters": chars[:MAX_CHARACTERS], "facts": facts[:MAX_FACTS]}


def dump(bible: dict) -> str:
    return json.dumps({"characters": bible.get("characters", [])[:MAX_CHARACTERS],
                       "facts": bible.get("facts", [])[:MAX_FACTS]}, ensure_ascii=False)


def is_empty(bible: dict) -> bool:
    return not bible.get("characters") and not bible.get("facts")


def prompt_block(bible: dict, child_name: str) -> str:
    """The SERIES paragraph injected into the user prompt. Empty string when there's nothing to
    continue (e.g. the very first story in a series bootstraps the cast)."""
    if is_empty(bible):
        return ""
    lines = [f"This is part of an ONGOING SERIES for {child_name}. Reuse these recurring "
             "characters consistently (same names, personalities, and looks) — the child knows "
             "and loves them. You may add at most one new minor character, but center the story "
             "on the recurring cast:"]
    for c in bible["characters"]:
        lines.append(f"- {c['name']}" + (f" — {c['description']}" if c.get("description") else ""))
    if bible.get("facts"):
        lines.append("Keep these facts true to the series:")
        for f in bible["facts"]:
            lines.append(f"- {f}")
    return "\n".join(lines)


def merge_characters(bible: dict, story_characters: list[dict]) -> dict:
    """Fold a freshly-written story's named cast into the bible (auto-grow). Dedupe by
    lower-cased name; keep the first description seen; cap the list so the prompt stays small."""
    existing = {c["name"].lower(): c for c in bible.get("characters", [])}
    for c in (story_characters or []):
        name = str(c.get("name") or "").strip()
        if not name or name.lower() in existing:
            continue
        desc = str(c.get("archetype") or c.get("role") or "").strip()
        existing[name.lower()] = {"name": name, "description": desc}
    chars = list(existing.values())[:MAX_CHARACTERS]
    return {"characters": chars, "facts": bible.get("facts", [])[:MAX_FACTS]}
