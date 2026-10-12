"""Multi-tenant isolation: a parent in household A cannot read household B's
children or stories. Uses an in-memory DB + dependency overrides so it's hermetic and fast.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tellybear.core import auth
from tellybear.db import models as m
from tellybear.db.base import Base, get_db
from tellybear.main import app

# StaticPool: one shared connection so the :memory: DB persists across sessions
_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_Session = sessionmaker(bind=_engine, autoflush=False)
Base.metadata.create_all(_engine)


def _override_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _seed():
    db = _Session()
    hhA, hhB = m.Household(name="A"), m.Household(name="B")
    db.add_all([hhA, hhB]); db.flush()
    pA = m.Parent(household_id=hhA.id, display_name="A", email="a@x.com")
    pB = m.Parent(household_id=hhB.id, display_name="B", email="b@x.com")
    cA = m.Child(household_id=hhA.id, name="KidA", age_band="early")
    cB = m.Child(household_id=hhB.id, name="KidB", age_band="early")
    db.add_all([pA, pB, cA, cB]); db.flush()
    sB = m.Story(child_id=cB.id, title="B's story", age_band="early",
                 story_json=json.dumps({"segments": []}), gen_model="x", status="ready")
    db.add(sB); db.commit()
    ids = {"pA": pA.id, "cA": cA.id, "cB": cB.id, "sB": sB.id}
    db.close()
    return ids


def _as_parent(pid):
    def _dep():
        db = _Session()
        try:
            p = db.get(m.Parent, pid)
            db.expunge(p)
            return p
        finally:
            db.close()
    return _dep


def test_household_isolation():
    ids = _seed()
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[auth.current_parent] = _as_parent(ids["pA"])
    try:
        c = TestClient(app)
        # A sees only its own child
        kids = c.get("/api/parent/children").json()
        names = {k["name"] for k in kids}
        assert names == {"KidA"}, names
        # A cannot read B's child or story
        assert c.get(f"/api/parent/children/{ids['cB']}").status_code == 404
        assert c.get(f"/api/parent/stories/{ids['sB']}").status_code == 404
        # B's story is not in A's story list
        assert all(s["id"] != ids["sB"] for s in c.get("/api/parent/stories").json())
    finally:
        app.dependency_overrides.clear()
