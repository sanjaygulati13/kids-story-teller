"""IPA injection pass before TTS.

inject(text, household_id, db) wraps each known name with Kokoro's inline override syntax
`[surface](/ipa/)`. Matching is whole-word, case-insensitive, and LONGEST-FIRST so that a
shorter name that is a prefix of a longer one never partially clobbers it. The visible label
keeps the text's original casing; only entries that have an IPA are wrapped.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from tellybear.db.models import Pronunciation


def _word_re(term: str) -> re.Pattern:
    # whole-word, case-insensitive, no partial matches inside other words
    return re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", re.IGNORECASE)


def inject_with_lexicon(text: str, lexicon: list[tuple[str, str]]) -> str:
    """lexicon: list of (surface, ipa). Pure function for easy testing."""
    # longest surface first so "Ariana" is handled before "Ari"
    entries = sorted(((s, i) for s, i in lexicon if i), key=lambda e: len(e[0]), reverse=True)
    out = text
    for surface, ipa in entries:
        rgx = _word_re(surface)

        def _repl(m: re.Match) -> str:
            return f"[{m.group(0)}](/{ipa}/)"

        out = rgx.sub(_repl, out)
    return out


def inject(text: str, household_id: str, db: Session) -> str:
    rows: list[Pronunciation] = (
        db.query(Pronunciation).filter_by(household_id=household_id).all()
    )
    lexicon = [(r.surface, r.ipa) for r in rows if r.ipa]
    return inject_with_lexicon(text, lexicon)
