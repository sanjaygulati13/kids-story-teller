"""Unit tests: played-state thresholding (complete OR percent>=40)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tellybear.api_child.routes import PLAYED_PERCENT_THRESHOLD, _is_played
from tellybear.db.base import Base
from tellybear.db.models import PlaybackEvent


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_threshold_is_40():
    assert PLAYED_PERCENT_THRESHOLD == 40.0


def test_no_events_not_played():
    db = _session()
    assert _is_played(db, "c1", "s1") is False


def test_progress_below_threshold_not_played():
    db = _session()
    db.add(PlaybackEvent(child_id="c1", story_id="s1", event="progress", percent=20.0))
    db.commit()
    assert _is_played(db, "c1", "s1") is False


def test_progress_at_threshold_played():
    db = _session()
    db.add(PlaybackEvent(child_id="c1", story_id="s1", event="progress", percent=40.0))
    db.commit()
    assert _is_played(db, "c1", "s1") is True


def test_progress_above_threshold_played():
    db = _session()
    db.add(PlaybackEvent(child_id="c1", story_id="s1", event="progress", percent=45.0))
    db.commit()
    assert _is_played(db, "c1", "s1") is True


def test_complete_event_played_regardless_of_percent():
    db = _session()
    db.add(PlaybackEvent(child_id="c1", story_id="s1", event="complete", percent=None))
    db.commit()
    assert _is_played(db, "c1", "s1") is True


def test_scoped_per_child_and_story():
    db = _session()
    db.add(PlaybackEvent(child_id="c1", story_id="s1", event="complete"))
    db.commit()
    assert _is_played(db, "c2", "s1") is False
    assert _is_played(db, "c1", "s2") is False
