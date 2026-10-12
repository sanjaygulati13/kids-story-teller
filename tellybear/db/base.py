"""Engine / session / Base.

Portable types only (no SQLite-specific columns) so the schema migrates cleanly to
Postgres later. SQLite gets foreign-key enforcement turned on per-connection.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from tellybear.core.config import get_settings

# Short, URL-friendly ids. 8 chars of base32 (no padding, lowercase, no ambiguous chars) is
# ~40 bits — far more than enough for a personal household-scale system (one family now, a
# handful in ~6 months). Column type is TEXT so this stays Postgres-portable; if we ever
# need global uniqueness at scale we can widen this without a type change.
_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # base31, drops 0/1/i/l/o to avoid confusion


def new_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


def new_slug() -> str:
    """Unguessable capability id for the child player URL /play/{slug}.
    token_urlsafe(12) → 16 url-safe chars / 96 bits of entropy. Rotatable to revoke a link
    (POST /api/parent/children/{id}/rotate-slug)."""
    return secrets.token_urlsafe(12)


def utcnow_iso() -> str:
    """UTC ISO-8601 timestamp (string, portable across SQLite/Postgres)."""
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.TB_DB_URL.startswith("sqlite") else {}


def _ensure_sqlite_parent(db_url: str) -> None:
    """SQLite won't create a DB in a missing directory; ensure the parent exists so the
    first connection (alembic, seed, or app) succeeds on a fresh machine."""
    if not db_url.startswith("sqlite"):
        return
    # forms: sqlite:///relative/x.db  or  sqlite:////abs/x.db  (':memory:' has no path)
    path_part = db_url.split("sqlite:///", 1)[-1]
    if not path_part or path_part == ":memory:":
        return
    from pathlib import Path
    Path(path_part).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(_settings.TB_DB_URL)

engine: Engine = create_engine(
    _settings.TB_DB_URL,
    echo=False,
    future=True,
    connect_args=_connect_args,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover - trivial
    if _settings.TB_DB_URL.startswith("sqlite"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
