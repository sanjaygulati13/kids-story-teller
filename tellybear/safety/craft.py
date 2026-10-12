"""Craft judge — scores story QUALITY, not safety.

A second judge call (same judge model, same loaded weights, same batch phase as safety) that
returns 0–10 craft subscores + an `overall`. Used to (a) select the best of N candidates and
(b) populate a quality dashboard. Calibrate the rubric against parent ratings as they accumulate
: if `overall` stops correlating with stars, fix the rubric, not the threshold.
"""
from __future__ import annotations

import json

from tellybear.core.config import PROMPTS_DIR
from tellybear.core.llm_client import LLMClient, LLMError
from tellybear.core.logging import get_logger

log = get_logger("craft")

_SUBSCORES = ("specificity", "arc", "dialogue", "freshness", "moral_subtlety", "bedtime_fit", "language")
# long-form epics use a length-aware rubric + an extra continuity dimension
_SUBSCORES_LONG = _SUBSCORES + ("continuity",)


def _story_text(story_dict: dict) -> str:
    title = story_dict.get("title", "")
    body = "\n".join(
        (f"{s.get('speaker','narrator')}: " if s.get("speaker") not in (None, "narrator") else "") + s.get("text", "")
        for s in story_dict.get("segments", [])
    )
    return f"TITLE: {title}\nAGE BAND: {story_dict.get('age_band','')}\n\n{body}".strip()


def _parse(raw: str, subscores: tuple[str, ...] = _SUBSCORES) -> dict | None:
    """Tolerant parse of the judge's JSON (strip fences / prose around it)."""
    try:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return None
        obj = json.loads(raw[start:end + 1])
        scores = {k: float(obj.get("scores", {}).get(k, 0) or 0) for k in subscores}
        overall = obj.get("overall")
        overall = float(overall) if overall is not None else round(sum(scores.values()) / len(scores), 1)
        return {"scores": scores, "overall": overall, "one_line": str(obj.get("one_line", ""))[:200]}
    except (ValueError, TypeError):
        return None


def judge_craft(story_dict: dict, client: LLMClient, model: str, long_form: bool = False) -> dict | None:
    """Return {scores{...}, overall, one_line} or None on failure (never raises — a craft miss
    must not block the pipeline; the story just ships without a craft score). For long_form epics,
    use the length-aware rubric so a ~2000-word chaptered story isn't penalized for not being short."""
    prompt_file = "craft_judge_long.txt" if long_form else "craft_judge.txt"
    subscores = _SUBSCORES_LONG if long_form else _SUBSCORES
    system = (PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")
    try:
        raw = client.generate(
            [{"role": "system", "content": system},
             {"role": "user", "content": _story_text(story_dict)}],
            model=model, json_mode=True, temperature=0.2,
        )
    except LLMError as e:
        log.warning("craft judge call failed: %s", e)
        return None
    verdict = _parse(raw, subscores)
    if verdict is None:
        log.warning("craft judge returned unparseable output")
    return verdict
