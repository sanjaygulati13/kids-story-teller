"""
Tellybear safety check — layered, conservative, fail-closed.

Layers (ALL must pass; first failure short-circuits):
  L0 structural  — Story JSON well-formed and plausibly within band.
  L1 lexical     — hard-block list + household banned terms (a BACKSTOP, not the
                   real check). Household terms with a replacement are substituted
                   first, so a parent "gun -> water blaster" rule clears the hard block.
  L2 LLM judge   — semantic review across child-safety categories. THIS is the real
                   check. Must run on a DIFFERENT call than the generator, ideally a
                   stronger model (Opus when enabled, else the local 27B).
Optional:
  double_check   — for an unattended/auto-publish path, require two independent judge
                   passes. The parent approval gate (L3) lives in the app, not here.

Wiring: pass an `llm_generate(system, user, json_mode)` callable. A reference
implementation against an OpenAI-compatible endpoint (llama-swap) is in eval_judge.py.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

# --- band structural bounds (gross-violation / truncation guard; the judge handles
#     fine-grained age fit via vocab_age_fit) -----------------------------------------
BAND_WORDS = {"early": (400, 850), "older": (600, 1300)}

# --- L1 lexical backstop -------------------------------------------------------------
# Intentionally SMALL and unambiguous. This is a backstop for catastrophic content, not
# the safety net — the LLM judge does the context-aware work. A false positive here only
# forces a human look; a false negative could reach a child, so we bias toward blocking.
# Tune to taste. Consider loading a slur list from a private file and extending HARD_BLOCK.
HARD_BLOCK = {
    "sex", "sexual", "naked", "porn",
    "kill", "killed", "murder", "stab", "gun", "gunshot", "shoot",
    "cocaine", "heroin", "cigarette", "beer", "wine", "drunk",
}

CATEGORIES = [
    "violence", "scary", "distressing", "inappropriate",
    "unsafe_behavior", "vocab_age_fit", "ending_calm", "ip_characters",
]
AGES = {"early": "3-5", "older": "6-8"}


def _word_re(term: str) -> re.Pattern:
    # whole-word, case-insensitive, no partial matches inside other words
    return re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", re.I)


_HARD_RE = [(t, _word_re(t)) for t in HARD_BLOCK]


@dataclass
class SafetyResult:
    verdict: str                       # "pass" | "fail"
    layer: str                         # which layer decided ("" if pass)
    categories: dict = field(default_factory=lambda: {c: "ok" for c in CATEGORIES})
    reasons: str = ""
    summary: str = ""
    lexical_hits: list = field(default_factory=list)
    judge_model: str = ""
    judged: bool = False               # True only if the LLM judge actually reviewed the content

    def as_dict(self) -> dict:
        return asdict(self)


def _story_text(story: dict) -> str:
    return "\n".join(s.get("text", "") for s in story.get("segments", []))


# --- L0 structural -------------------------------------------------------------------
def structural_check(story: dict) -> tuple[bool, str]:
    segs = story.get("segments")
    if not isinstance(segs, list) or not segs:
        return False, "no segments"
    if any(not (s.get("text") or "").strip() for s in segs):
        return False, "empty segment present"
    band = story.get("age_band")
    lo, hi = BAND_WORDS.get(band, (1, 100_000))
    wc = len(_story_text(story).split())
    if not (lo <= wc <= hi):
        return False, f"word count {wc} outside {band} bounds {lo}-{hi}"
    names = {c.get("name") for c in story.get("characters", [])} | {"narrator"}
    unknown = {s.get("speaker") for s in segs} - names
    if unknown:
        return False, f"unknown speaker(s): {sorted(unknown)}"
    return True, ""


# --- L1 lexical ----------------------------------------------------------------------
def lexical_check(story: dict, banned_terms: list[dict]) -> tuple[bool, list, dict]:
    """banned_terms: [{"term": str, "replacement": str | None}].
    Returns (ok, hits, effective_story). Substitutes household replacements FIRST so they
    clear any hard-block hit, then scans the post-substitution text for hard-block terms."""
    mutated = json.loads(json.dumps(story))  # deep copy
    hits: list[dict] = []
    fail = False

    for bt in banned_terms or []:
        term = bt["term"]
        rep = bt.get("replacement")
        rgx = _word_re(term)
        if any(rgx.search(s.get("text", "")) for s in mutated["segments"]):
            if rep:
                for s in mutated["segments"]:
                    s["text"] = rgx.sub(rep, s["text"])
                hits.append({"term": term, "kind": "replaced", "with": rep})
            else:
                hits.append({"term": term, "kind": "banned_no_replacement"})
                fail = True

    text = _story_text(mutated)
    for term, rgx in _HARD_RE:
        if rgx.search(text):
            hits.append({"term": term, "kind": "hard_block"})
            fail = True

    return (not fail), hits, mutated


# --- L2 judge ------------------------------------------------------------------------
def _parse_json(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    try:
        data = json.loads(s)
        if not isinstance(data, dict):
            raise ValueError("judge output is not a JSON object")
        return data
    except Exception:
        # fail closed: unparseable judge output is treated as unsafe
        return {
            "verdict": "fail",
            "categories": {c: "concern" for c in CATEGORIES},
            "reasons": "judge returned unparseable output; failing closed",
            "summary": "",
        }


def judge(
    story: dict,
    age_band: str,
    llm_generate: Callable[..., str],
    judge_prompt_path: str,
    judge_model_name: str = "",
) -> SafetyResult:
    system = Path(judge_prompt_path).read_text(encoding="utf-8")
    user = (
        f"Target age band: {age_band} (ages {AGES.get(age_band, 'unknown')}).\n\n"
        f"STORY (JSON):\n{json.dumps(story, ensure_ascii=False)}"
    )
    data = _parse_json(llm_generate(system=system, user=user, json_mode=True))
    raw_cats = data.get("categories")
    raw_cats = raw_cats if isinstance(raw_cats, dict) else {}
    # fail closed: anything other than an exact "ok" (missing, "Concern", "yes", true, ...) is a concern
    cats = {c: "ok" if str(raw_cats.get(c, "")).strip().lower() == "ok" else "concern"
            for c in CATEGORIES}
    verdict = "pass" if (str(data.get("verdict", "")).strip().lower() == "pass"
                         and "concern" not in cats.values()) else "fail"
    return SafetyResult(
        verdict=verdict,
        layer="judge" if verdict == "fail" else "",
        categories=cats,
        reasons=data.get("reasons", ""),
        summary=data.get("summary", ""),
        judge_model=judge_model_name,
        judged=True,
    )


# --- orchestrator --------------------------------------------------------------------
def run_safety(
    story: dict,
    banned_terms: list[dict],
    llm_generate: Callable[..., str],
    judge_prompt_path: str,
    judge_model_name: str = "",
    double_check: bool = False,
) -> tuple[SafetyResult, dict]:
    """Returns (result, effective_story). effective_story has household substitutions
    applied and is what should be sent to TTS when the result passes."""
    age_band = story.get("age_band", "early")

    # A structural miss (e.g. word count out of band) is NOT a content verdict: the content still
    # goes through the lexical backstop and the judge, and the structural reason is folded in.
    struct_ok, struct_why = structural_check(story)

    ok, hits, story2 = lexical_check(story, banned_terms)
    if not ok:
        return (
            SafetyResult("fail", "lexical",
                         categories={c: "concern" if c == "inappropriate" else "ok" for c in CATEGORIES},
                         reasons=f"lexical backstop blocked: {hits}", lexical_hits=hits,
                         judge_model=judge_model_name),
            story2,
        )

    res = judge(story2, age_band, llm_generate, judge_prompt_path, judge_model_name)
    res.lexical_hits = hits

    if double_check and res.verdict == "pass":
        res2 = judge(story2, age_band, llm_generate, judge_prompt_path, judge_model_name)
        if res2.verdict != "pass":
            res2.lexical_hits = hits
            res = res2

    if not struct_ok:
        res.reasons = f"structural: {struct_why}" + (f"; {res.reasons}" if res.reasons else "")
        if res.verdict == "pass":
            res.verdict, res.layer = "fail", "structural"
    return res, story2


if __name__ == "__main__":
    # tiny smoke test of the deterministic layers (no LLM needed)
    demo = {
        "title": "t", "age_band": "early",
        "characters": [{"name": "Mira", "role": "main"}],
        "segments": [{"speaker": "narrator", "text": "Mira hugged her water blaster and yawned. " * 80}],
    }
    print("structural:", structural_check(demo))
    print("lexical:", lexical_check(demo, [{"term": "gun", "replacement": "water blaster"}])[:2])
