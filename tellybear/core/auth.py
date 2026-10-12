"""Built-in accounts & sessions.

Email+password identity for parents. Only the bcrypt hash is stored — never the plaintext.
Sessions are server-side random tokens (httpOnly cookie holds the token; the DB stores only its
SHA-256 hash), so they're revocable and survive restarts. `current_household()` resolves from the
logged-in parent; every child-scoped query filters on it.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from tellybear.core.config import get_settings
from tellybear.db.base import get_db
from tellybear.db.models import Household, Parent, Session

COOKIE = "sg_session"


# ---- password hashing ----
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


# a real bcrypt hash of a random string, checked when an email is unknown so login takes the same
# time whether or not the account exists
DUMMY_HASH = bcrypt.hashpw(b"tellybear-timing-equalizer", bcrypt.gensalt()).decode("ascii")


def verify_password(pw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


# ---- sessions ----
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(db: DbSession, parent: Parent, response: Response) -> str:
    """Create a session row + set the httpOnly cookie. Returns the raw token."""
    s = get_settings()
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=s.TB_SESSION_TTL_DAYS)
    db.add(Session(parent_id=parent.id, token_hash=_hash_token(token), expires_at=expires.isoformat()))
    db.commit()
    response.set_cookie(
        COOKIE, token, max_age=s.TB_SESSION_TTL_DAYS * 86400, httponly=True,
        samesite="lax", secure=s.TB_COOKIE_SECURE, path="/",  # Secure=true behind HTTPS/Cloudflare
    )
    return token


def clear_session(db: DbSession, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        row = db.query(Session).filter_by(token_hash=_hash_token(token)).one_or_none()
        if row:
            db.delete(row); db.commit()
    response.delete_cookie(COOKIE, path="/")


def _parent_from_request(db: DbSession, request: Request) -> Parent | None:
    # service token (headless scripts/cron): X-API-Token → the admin parent. Constant-time compare.
    api_tok = request.headers.get("X-API-Token")
    if api_tok:
        cfg_tok = get_settings().TB_API_TOKEN
        if cfg_tok and secrets.compare_digest(api_tok, cfg_tok):
            admin = lookup_parent_by_email(db, get_settings().TB_ADMIN_EMAIL)
            if admin is not None:
                return admin
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.query(Session).filter_by(token_hash=_hash_token(token)).one_or_none()
    if row is None:
        return None
    try:
        if datetime.fromisoformat(row.expires_at) < datetime.now(timezone.utc):
            db.delete(row); db.commit()
            return None
    except Exception:
        return None
    return db.get(Parent, row.parent_id)


# ---- FastAPI dependencies ----
def current_parent(request: Request, db: DbSession = Depends(get_db)) -> Parent:
    parent = _parent_from_request(db, request)
    if parent is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return parent


def current_household(parent: Parent = Depends(current_parent),
                      db: DbSession = Depends(get_db)) -> Household:
    """The caller's household — resolved from the session. Every child-scoped
    query already filters by this id, so isolation is automatic across households."""
    hh = db.get(Household, parent.household_id)
    if hh is None:
        raise HTTPException(status_code=500, detail="parent has no household")
    return hh


def require_admin(parent: Parent = Depends(current_parent)) -> Parent:
    if not parent.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return parent


def lookup_parent_by_email(db: DbSession, email: str) -> Parent | None:
    email = (email or "").strip().lower()
    return db.execute(select(Parent).where(Parent.email == email)).scalars().first()
