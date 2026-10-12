"""Regression tests for the safety gate, account and child-link hardening."""
from __future__ import annotations

import inspect
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tellybear.core import auth
from tellybear.db import models as m
from tellybear.db.base import Base, get_db
from tellybear.main import app
from tellybear.safety import safety_check as sc
from tellybear.worker.runner import _safety_ok

JUDGE_PROMPT = str(sc.Path(__file__).resolve().parents[1] / "prompts" / "judge_system.txt")


def _story(words: int) -> dict:
    return {
        "title": "t", "age_band": "early",
        "characters": [{"name": "Mira", "role": "main"}],
        "segments": [{"speaker": "narrator", "text": "Mira yawned softly. " * (words // 3)}],
    }


def _judge(reply: dict):
    calls = []

    def llm(system: str, user: str, json_mode: bool = True) -> str:
        calls.append(user)
        return json.dumps(reply)
    return llm, calls


ALL_OK = {"verdict": "pass", "categories": {c: "ok" for c in sc.CATEGORIES}, "reasons": "", "summary": ""}


# ---------------------------------------------------------------- safety gate
def test_structural_fail_still_runs_the_judge():
    bad = dict(ALL_OK, verdict="fail", categories={**ALL_OK["categories"], "violence": "concern"})
    llm, calls = _judge(bad)
    res, _ = sc.run_safety(_story(3000), [], llm, JUDGE_PROMPT)
    assert calls, "an over-length story must still be judged"
    assert res.judged and res.verdict == "fail"
    assert res.categories["violence"] == "concern"
    assert not _safety_ok(res)


def test_structural_only_fail_is_judged_and_not_a_content_concern():
    llm, _ = _judge(ALL_OK)
    res, _ = sc.run_safety(_story(3000), [], llm, JUDGE_PROMPT)
    assert res.judged and res.verdict == "fail" and res.layer == "structural"
    assert _safety_ok(res)  # word count alone shouldn't block approval


def test_judge_categories_fail_closed():
    for weird in ("Concern", "concerns", "yes", True, None, ""):
        reply = dict(ALL_OK, categories={**ALL_OK["categories"], "violence": weird})
        llm, _ = _judge(reply)
        res, _ = sc.run_safety(_story(200), [], llm, JUDGE_PROMPT)
        assert res.verdict == "fail" and res.categories["violence"] == "concern", weird
        assert not _safety_ok(res)


def test_judge_non_object_reply_fails_closed():
    def llm(system, user, json_mode=True):
        return "[1, 2, 3]"
    res, _ = sc.run_safety(_story(200), [], llm, JUDGE_PROMPT)
    assert res.verdict == "fail" and not _safety_ok(res)


def test_judge_fail_verdict_without_categories_blocks_auto():
    llm, _ = _judge(dict(ALL_OK, verdict="fail"))
    res, _ = sc.run_safety(_story(200), [], llm, JUDGE_PROMPT)
    assert res.verdict == "fail" and res.layer == "judge"
    assert not _safety_ok(res)


def test_unjudged_result_is_never_safe():
    res = sc.SafetyResult("fail", "structural")  # e.g. a result built without a judge call
    assert not _safety_ok(res)


def test_kokoro_accepts_language():
    from tellybear.tts.engine_kokoro import KokoroEngine
    assert "language" in inspect.signature(KokoroEngine.speak).parameters


# ---------------------------------------------------------------- API hardening
_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_Session = sessionmaker(bind=_engine, autoflush=False)
Base.metadata.create_all(_engine)


def _db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _seed():
    db = _Session()
    hh = m.Household(name="H")
    db.add(hh); db.flush()
    p = m.Parent(household_id=hh.id, display_name="P", email=f"p-{hh.id}@x.com",
                 password_hash=auth.hash_password("correct horse"))
    c = m.Child(household_id=hh.id, name="Kid", age_band="early")
    db.add_all([p, c]); db.flush()
    req = m.StoryRequest(child_id=c.id, story_type="bedtime_calm", character_ids="[]")
    db.add(req); db.flush()
    s = m.Story(child_id=c.id, request_id=req.id, title="s", age_band="early",
                story_json=json.dumps({"segments": []}), gen_model="x", status="ready")
    db.add(s); db.commit()
    ids = {"p": p.id, "c": c.id, "slug": c.public_slug, "s": s.id}
    db.close()
    return ids


def _as(pid):
    def dep():
        db = _Session()
        try:
            p = db.get(m.Parent, pid); db.expunge(p); return p
        finally:
            db.close()
    return dep


def test_signup_is_closed_when_invite_only():
    app.dependency_overrides[get_db] = _db
    try:
        from tellybear.core.config import get_settings
        admin = get_settings().TB_ADMIN_EMAIL
        r = TestClient(app).post("/api/auth/signup", json={"email": admin, "password": "x" * 12})
        assert r.status_code == 403, r.text  # even the admin email can't self-register
        assert "admin_claim_pending" not in TestClient(app).get("/api/auth/config").json()
    finally:
        app.dependency_overrides.clear()


def test_child_api_is_slug_only_and_delete_child_works():
    ids = _seed()
    app.dependency_overrides[get_db] = _db
    try:
        c = TestClient(app)
        assert c.get(f"/api/child/profile/{ids['slug']}").json() == {"name": "Kid"}
        assert c.get(f"/api/child/profile/{ids['c']}").status_code == 404  # internal id rejected

        app.dependency_overrides[auth.current_parent] = _as(ids["p"])
        new = c.post(f"/api/parent/children/{ids['c']}/rotate-slug").json()["public_slug"]
        assert new != ids["slug"]
        assert c.get(f"/api/child/profile/{ids['slug']}").status_code == 404  # old link revoked

        r = c.delete(f"/api/parent/children/{ids['c']}")
        assert r.status_code == 204, r.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- TTS URLs from the Admin tab
def test_tts_urls_configurable_from_admin(monkeypatch):
    from tellybear.core import settings_store
    from tellybear.db import base as db_base
    ids = _seed()
    monkeypatch.setattr(db_base, "SessionLocal", _Session)  # tts_url() opens its own session
    settings_store._url_cache.clear()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth.current_parent] = _as(ids["p"])
    app.dependency_overrides[auth.require_admin] = _as(ids["p"])
    try:
        c = TestClient(app)
        env_default = c.get("/api/parent/settings").json()["kokoro_url_default"]
        assert settings_store.tts_url("kokoro") == env_default.rstrip("/")

        r = c.put("/api/parent/settings", json={"kokoro_url": "http://tts-box:8880/"})
        assert r.status_code == 200 and r.json()["kokoro_url"] == "http://tts-box:8880"
        assert settings_store.tts_url("kokoro") == "http://tts-box:8880"
        from tellybear.tts.engine_kokoro import KokoroEngine
        assert KokoroEngine().base == "http://tts-box:8880"

        assert c.put("/api/parent/settings", json={"chatterbox_url": "file:///etc/passwd"}).status_code == 422

        c.put("/api/parent/settings", json={"kokoro_url": ""})          # clear = back to .env
        assert settings_store.tts_url("kokoro") == env_default.rstrip("/")
    finally:
        app.dependency_overrides.clear()
        settings_store._url_cache.clear()


def test_child_notes_reach_the_prompt():
    from tellybear.worker.prompt_builder import _notes_line
    from tellybear.core.config import PROMPTS_DIR
    assert "{child_notes}" in (PROMPTS_DIR / "gen_user_template.txt").read_text()
    assert _notes_line(None) == "(nothing extra)"
    line = _notes_line("loves trains,\n scared of thunder")
    assert "loves trains, scared of thunder" in line and "fear" in line
    assert len(_notes_line("x" * 2000)) < 700  # capped
