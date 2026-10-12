"""IPA lexicon management.

CRUD over the `pronunciation` table (household-scoped) plus `baseline_ipa()` which asks the
active TTS engine to phonemize a surface form. `hindi_g2p` is an optional espeak-ng pass and
is best-effort: if espeak-ng is not installed it raises NotImplementedError.
"""
from __future__ import annotations

import shutil
import subprocess

from sqlalchemy.orm import Session

from tellybear.db.base import utcnow_iso
from tellybear.db.models import Pronunciation


def list_for(db: Session, household_id: str) -> list[Pronunciation]:
    return db.query(Pronunciation).filter_by(household_id=household_id).order_by(Pronunciation.surface).all()


def get(db: Session, household_id: str, surface: str) -> Pronunciation | None:
    return db.query(Pronunciation).filter_by(household_id=household_id, surface=surface).one_or_none()


def upsert(db: Session, household_id: str, surface: str, ipa: str | None,
           source: str = "manual", respelling: str | None = None) -> Pronunciation:
    row = get(db, household_id, surface)
    if row is None:
        row = Pronunciation(household_id=household_id, surface=surface, ipa=ipa,
                            source=source, respelling=respelling)
        db.add(row)
    else:
        row.ipa = ipa
        row.source = source
        if respelling is not None:
            row.respelling = respelling
        row.updated_at = utcnow_iso()
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, household_id: str, pron_id: str) -> bool:
    row = db.query(Pronunciation).filter_by(id=pron_id, household_id=household_id).one_or_none()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def baseline_ipa(surface: str) -> str | None:
    """Ask the active TTS engine for baseline IPA (Kokoro /dev/phonemize)."""
    from tellybear.tts.engine import get_engine  # local import to avoid cycles
    return get_engine().phonemize(surface)


def hindi_g2p(surface: str) -> str:
    """Optional Indian-English / Hindi IPA via espeak-ng (source='hindi_g2p')."""
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise NotImplementedError("espeak-ng not installed; hindi_g2p unavailable")
    out = subprocess.run(
        [exe, "-v", "en-in", "--ipa", "-q", surface],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()
