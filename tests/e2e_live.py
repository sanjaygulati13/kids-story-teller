#!/usr/bin/env python3
"""End-to-end acceptance check against live LLM + TTS services.

Runs the REAL pipeline against live llama-swap + Kokoro:
  now request -> generate -> safety verdict -> approve -> wav+mp3 -> catalog ->
  playback auto-marks played at >=40%.

Run from repo root:  .venv/bin/python tests/e2e_live.py
Requires a seeded DB and reachable services.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from tellybear.core import auth
from tellybear.core.config import get_settings
from tellybear.db.base import SessionLocal
from tellybear.db.models import StoryAsset
from tellybear.main import app
from tellybear.worker import runner

# Authed endpoints now require a parent. For the in-process E2E, override the auth
# dependency to act as the seeded admin (the test exercises the pipeline, not auth — auth has
# its own isolation test in tests/test_isolation.py).
def _e2e_admin():
    db = SessionLocal()
    try:
        return auth.lookup_parent_by_email(db, get_settings().TB_ADMIN_EMAIL)
    finally:
        db.close()


app.dependency_overrides[auth.current_parent] = _e2e_admin

client = TestClient(app)
P = "/api/parent"
C = "/api/child"


def fail(msg: str):
    print(f"  ✗ {msg}")
    sys.exit(1)


def ok(msg: str):
    print(f"  ✓ {msg}")


def main():
    print("== E2E ==")

    kids = client.get(f"{P}/children").json()
    if not kids:
        fail("no children — run scripts/seed.py first")
    child = kids[0]
    kid, slug, name = child["id"], child["public_slug"], child["name"]
    ok(f"child {name} = {kid}")

    # pronunciation override for the name (so we exercise the IPA inject path)
    ipa = client.post(f"{P}/pronunciations", json={"surface": name, "ipa": "sæm", "source": "manual"})
    assert ipa.status_code == 201, ipa.text
    ok(f"pronunciation override saved for {name}")

    # a gentle character
    ch = client.post(f"{P}/children/{kid}/characters",
                     json={"name": "Pip", "archetype": "curious firefly"})
    assert ch.status_code == 201, ch.text
    char_id = ch.json()["id"]

    # 1) submit a NOW request
    req = client.post(f"{P}/requests", json={
        "child_id": kid, "story_type": "friendship", "character_ids": [char_id],
        "special_request": "a story about sharing", "priority": "now",
    })
    assert req.status_code == 201, req.text
    ok(f"now request queued: {req.json()['id']}")

    # 2) worker generates (one claim+process)
    print("  … generating (local model, may take a minute) …")
    story_id = runner.process_once()
    if not story_id:
        fail("generation produced no story")
    ok(f"story generated: {story_id}")

    detail = client.get(f"{P}/stories/{story_id}").json()
    if detail["status"] != "needs_approval":
        fail(f"expected needs_approval, got {detail['status']}")
    if not detail.get("safety"):
        fail("no safety verdict attached")
    ok(f"status=needs_approval, safety verdict={detail['safety']['verdict']}, "
       f"categories={detail['safety']['categories']}")
    ok(f"word_count={detail['word_count']}, title={detail['title']!r}")

    # 3) approve (respect a fail verdict by overriding only if needed for the run)
    override = detail["safety"]["verdict"] == "fail"
    if override:
        print("  ! verdict was fail; approving with override_fail to continue the E2E")
    appr = client.post(f"{P}/stories/{story_id}/approve", json={"override_fail": override})
    assert appr.status_code == 200, appr.text
    ok("approved")

    # 4) worker synthesizes
    print("  … synthesizing audio (Kokoro) …")
    synth_id = runner.process_once()
    if synth_id != story_id:
        fail(f"synthesis claimed {synth_id}, expected {story_id}")
    detail = client.get(f"{P}/stories/{story_id}").json()
    if detail["status"] != "ready":
        fail(f"expected ready, got {detail['status']}")
    kinds = {a["kind"] for a in detail["assets"]}
    if not {"wav", "mp3"} <= kinds:
        fail(f"expected wav+mp3 assets, got {kinds}")
    dur = next(a["duration_seconds"] for a in detail["assets"] if a["kind"] == "mp3")
    ok(f"status=ready, assets={sorted(kinds)}, duration={dur:.1f}s")

    # verify files exist on disk
    db = SessionLocal()
    try:
        for a in db.query(StoryAsset).filter_by(story_id=story_id).all():
            if not Path(a.path).is_file():
                fail(f"asset file missing: {a.path}")
        ok("wav + mp3 files exist on disk")
    finally:
        db.close()

    # 5) catalog shows it
    cat = client.get(f"{C}/catalog/{slug}").json()
    entry = next((c for c in cat if c["story_id"] == story_id), None)
    if entry is None:
        fail("story not in child catalog")
    if entry["played"]:
        fail("story should be unplayed initially")
    ok(f"catalog shows story (played={entry['played']}, dur={entry['duration_seconds']})")

    # 6) playback: a progress<40 does NOT mark played; >=40 DOES
    client.post(f"{C}/playback", json={"child_id": slug, "story_id": story_id,
                                       "event": "progress", "percent": 20.0})
    cat = client.get(f"{C}/catalog/{slug}").json()
    e = next(c for c in cat if c["story_id"] == story_id)
    if e["played"]:
        fail("20% progress should NOT mark played")
    ok("20% progress -> not played (correct)")

    client.post(f"{C}/playback", json={"child_id": slug, "story_id": story_id,
                                       "event": "progress", "percent": 45.0})
    cat = client.get(f"{C}/catalog/{slug}").json()
    e = next(c for c in cat if c["story_id"] == story_id)
    if not e["played"]:
        fail("45% progress SHOULD mark played")
    ok("45% progress -> played (correct)")


    print("\n✅ E2E PASSED (now→generate→verdict→approve→wav+mp3→catalog→played)")


if __name__ == "__main__":
    main()
