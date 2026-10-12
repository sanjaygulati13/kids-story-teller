#!/usr/bin/env python3
"""Idempotent first-run seed.

Creates one household, its admin parent (email = TB_ADMIN_EMAIL) with a password, and a first
child profile so there is somewhere for stories to go. Safe to re-run (the Docker image runs it on
every start): it never duplicates anything, and once the family has a child it adds no more.
Add co-parents and more children from the app.

The admin password comes from --password, else TB_ADMIN_PASSWORD, else a random one is generated
and printed ONCE. It is only set when the admin has no password yet (never overwritten).

Usage:
  python scripts/seed.py
  python scripts/seed.py --household "The Smiths" --parent "Alex" --child "Sam" --band early
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tellybear.core import auth
from tellybear.core.config import get_settings
from tellybear.db.base import SessionLocal
from tellybear.db.models import Child, Household, Parent


def seed(household: str, parent_name: str, child_name: str, band: str, password: str | None) -> None:
    db = SessionLocal()
    try:
        admin_email = get_settings().TB_ADMIN_EMAIL.strip().lower()
        # Find things by the admin's email, not by name: names can be edited in the app, and this
        # script runs on every container start.
        parent = db.query(Parent).filter_by(email=admin_email).one_or_none()
        if parent is None:
            hh = Household(name=household)
            db.add(hh)
            db.flush()
            print(f"+ household {hh.id} ({household})")
            parent = Parent(household_id=hh.id, display_name=parent_name, email=admin_email, is_admin=1)
            db.add(parent)
            db.flush()
            print(f"+ parent {parent.id} ({parent_name}, admin={admin_email})")
        else:
            hh = db.get(Household, parent.household_id)
            print(f"= admin {admin_email} exists (household {hh.id})")
        if not parent.password_hash:
            pw = password or os.environ.get("TB_ADMIN_PASSWORD") or ""
            generated = not pw
            if generated:
                pw = secrets.token_urlsafe(12)
            if len(pw) < 8:
                sys.exit("error: admin password must be at least 8 characters")
            parent.password_hash = auth.hash_password(pw)
            if generated:
                print(f"\n  Admin login: {admin_email}\n  Password:    {pw}\n"
                      "  (shown once — change it from the 🔑 button at the top of the parent app)\n")

        # first child only — once the family has any child, manage children in the Kids tab
        if db.query(Child).filter_by(household_id=hh.id).count() == 0:
            child = Child(household_id=hh.id, name=child_name, age_band=band, active=1)
            db.add(child)
            db.flush()
            print(f"+ child {child.id} ({child_name}, {band})")

        db.commit()
        print("seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--household", default="Home")
    ap.add_argument("--parent", default="Parent", help="admin parent's display name")
    ap.add_argument("--child", default="Sam", help="first child's name")
    ap.add_argument("--band", default="early", choices=["early", "older"],
                    help="age band: early (3-5) or older (6-8)")
    ap.add_argument("--password", help="admin password (default: TB_ADMIN_PASSWORD or generated)")
    a = ap.parse_args()
    seed(a.household, a.parent, a.child, a.band, a.password)
