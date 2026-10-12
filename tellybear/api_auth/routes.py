"""Auth API. Mounted at /api/auth — NOT behind current_parent (these create the session).

signup: only when TB_OPEN_SIGNUP=true; a new email creates its own fresh household. The admin
account is created (with its password) by scripts/seed.py, and co-parents are added by a signed-in
parent from the app — so there is never an unclaimed account anyone could take over.
login/logout/me are standard.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_PASSWORD_BYTES = 72  # bcrypt ignores anything past 72 bytes


def check_password_rules(pw: str) -> None:
    if len(pw) < MIN_PASSWORD:
        raise HTTPException(422, f"password must be at least {MIN_PASSWORD} characters")
    if len(pw.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(422, f"password must be at most {MAX_PASSWORD_BYTES} bytes")

from tellybear.core import auth
from tellybear.core.config import get_settings
from tellybear.db.base import get_db
from tellybear.db.models import Household, Parent
from tellybear.db.models import Session as SessionRow

router = APIRouter()


class SignupIn(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class ParentOut(BaseModel):
    id: str
    email: str | None
    display_name: str
    is_admin: bool
    household_id: str


def _out(p: Parent) -> ParentOut:
    return ParentOut(id=p.id, email=p.email, display_name=p.display_name,
                     is_admin=bool(p.is_admin), household_id=p.household_id)


@router.post("/signup", response_model=ParentOut, status_code=201)
def signup(body: SignupIn, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    email = str(body.email).strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, "enter a valid email address")
    if not s.TB_OPEN_SIGNUP:
        raise HTTPException(403, "new accounts are invite-only — ask your family admin to add you")
    check_password_rules(body.password)
    if auth.lookup_parent_by_email(db, email) is not None:
        raise HTTPException(409, "an account with that email already exists — please log in")
    # brand-new account → its own household
    name = (body.display_name or email.split("@")[0]).strip() or "Parent"
    hh = Household(name=f"{name}'s family")
    db.add(hh); db.flush()
    p = Parent(household_id=hh.id, display_name=name, email=email,
               password_hash=auth.hash_password(body.password),
               is_admin=0)
    db.add(p); db.commit(); db.refresh(p)
    auth.issue_session(db, p, response)
    return _out(p)


@router.post("/login", response_model=ParentOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)):
    p = auth.lookup_parent_by_email(db, str(body.email))
    if p is None or not p.password_hash:
        auth.verify_password(body.password, auth.DUMMY_HASH)  # equalize timing for unknown emails
        raise HTTPException(401, "wrong email or password")
    if not auth.verify_password(body.password, p.password_hash):
        raise HTTPException(401, "wrong email or password")
    auth.issue_session(db, p, response)
    return _out(p)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password", status_code=204)
def change_password(body: ChangePasswordIn, response: Response, db: Session = Depends(get_db),
                    parent: Parent = Depends(auth.current_parent)):
    """Self-serve password change for the signed-in parent. Requires the current password. Signs
    out every other session (a stolen cookie must not survive a password change)."""
    if not auth.verify_password(body.current_password, parent.password_hash):
        raise HTTPException(401, "current password is incorrect")
    check_password_rules(body.new_password)
    parent.password_hash = auth.hash_password(body.new_password)
    db.query(SessionRow).filter_by(parent_id=parent.id).delete()
    db.commit()
    auth.issue_session(db, parent, response)
    response.status_code = 204
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.clear_session(db, request, response)
    return Response(status_code=204)


@router.get("/me", response_model=ParentOut)
def me(parent: Parent = Depends(auth.current_parent)):
    return _out(parent)


@router.get("/config")
def auth_config(db: Session = Depends(get_db)):
    """Public: tells the login screen whether open signup is allowed."""
    return {"open_signup": bool(get_settings().TB_OPEN_SIGNUP)}
