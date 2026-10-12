#!/usr/bin/env python3
"""Import hand-written stories (Story JSON) for a child — e.g. the reference stories in
examples/stories/ — so you can try the review → approve → narrate → play flow without an LLM.

Each file holds one Story JSON object (or a JSON array of them). Every "{{child}}" in the story
is replaced with the child's name. Stories are validated against the Story JSON schema and
inserted as `needs_approval`, so they appear in the parent app's Review tab like any generated
story; approve one and the worker narrates it. With --approve they go straight to narration.

Note: imported stories don't pass through the LLM safety judge — only import stories you wrote
or have read yourself.

Usage:
  python scripts/import_stories.py --child Sam examples/stories/
  python scripts/import_stories.py --child Sam --approve examples/stories/01-the-little-tugboat.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tellybear.db.base import SessionLocal
from tellybear.db.models import Child, Story
from tellybear.worker.parser import ParseError, parse

PLACEHOLDER = "{{child}}"


def _story_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    return out


def _find_child(db, key: str) -> Child | None:
    child = db.get(Child, key)
    if child is None:
        matches = db.query(Child).filter_by(name=key).all()
        if len(matches) > 1:
            sys.exit(f"error: more than one child named {key!r}; pass the child id instead")
        child = matches[0] if matches else None
    return child


def import_stories(child_key: str, paths: list[str], approve: bool) -> int:
    db = SessionLocal()
    try:
        child = _find_child(db, child_key)
        if child is None:
            sys.exit(f"error: no child with id or name {child_key!r} (run scripts/seed.py first?)")

        inserted = 0
        for path in _story_files(paths):
            raw = path.read_text(encoding="utf-8").replace(PLACEHOLDER, child.name)
            data = json.loads(raw)
            for i, obj in enumerate(data if isinstance(data, list) else [data]):
                try:
                    story = parse(json.dumps(obj))
                except ParseError as e:
                    print(f"  ! skipping {path.name}[{i}]: {e}")
                    continue
                db.add(Story(
                    request_id=None,
                    child_id=child.id,
                    title=story.title,
                    age_band=story.age_band,
                    story_json=json.dumps(story.model_dump(), ensure_ascii=False),
                    summary=story.summary,
                    word_count=story.word_count(),
                    gen_model="imported",
                    status="approved" if approve else "needs_approval",
                ))
                inserted += 1
                print(f"  + {story.title} ({story.age_band}, {story.word_count()} words)")
        db.commit()
        where = "queued for narration" if approve else "waiting in the Review tab"
        print(f"imported {inserted} stor{'y' if inserted == 1 else 'ies'} for {child.name} — {where}")
        return inserted
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--child", required=True, help="child id or name")
    ap.add_argument("--approve", action="store_true", help="skip the Review tab; narrate right away")
    ap.add_argument("paths", nargs="+", help="story .json files or directories of them")
    a = ap.parse_args()
    import_stories(a.child, a.paths, a.approve)
