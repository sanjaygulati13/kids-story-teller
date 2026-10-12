"""Generation worker.

Always-on loop: synthesize approved stories, then handle `now`-priority requests inline
(generate → judge). `batch`-priority requests wait for the evening timer, which runs a phased
drain (generate-all → GPU hand-off → judge-all) so two different local models don't swap on
every story.

Model selection is at job time: gen model = request.gen_model_override or the effective
active_gen_model; judge model = the effective active_judge_model (or Opus). See settings_store.
Watchdog re-queues requests stuck in `generating`/`synthesizing` > 15 min.
"""
from __future__ import annotations

import contextlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tellybear.core.config import PROMPTS_DIR, Settings, get_settings
from tellybear.core.llm_client import LLMClient, LLMError, get_llm_client
from tellybear.core.logging import get_logger
from tellybear.core import notify, settings_store
from tellybear.db.base import SessionLocal
from tellybear.db.models import Child, SafetyReview, Story, StoryRequest
from tellybear.safety import craft as craft_judge
from tellybear.safety import integration as _integration
from tellybear.safety.integration import review_story
from tellybear.variety import embed
from tellybear.worker import gpu, prompt_builder
from tellybear.worker.parser import ParseError, parse

log = get_logger("worker")


# --------------------------- model resolution ---------------------------
def resolve_gen_model(req: StoryRequest, eff: settings_store.Effective, s: Settings) -> str:
    return req.gen_model_override or eff.gen_model(s)


def _is_local(model: str, s: Settings) -> bool:
    """A model is 'local' (needs a llama-swap VRAM load) unless it's the Opus cloud id."""
    return not (s.TB_ENABLE_OPUS and s.TB_OPUS_MODEL and model == s.TB_OPUS_MODEL)


# --------------------------- claiming ---------------------------
def claim_next(db: Session, now_only: bool = False) -> StoryRequest | None:
    """Atomically claim the next queued request. `now_only` restricts to now-priority (the
    always-on loop uses this so batch requests are left for the phased timer)."""
    stmt = select(StoryRequest).where(StoryRequest.status == "queued")
    if now_only:
        stmt = stmt.where(StoryRequest.priority == "now")
    stmt = stmt.order_by((StoryRequest.priority == "now").desc(), StoryRequest.created_at.asc()).limit(1)
    req = db.execute(stmt).scalars().first()
    if req is None:
        return None
    res = db.execute(
        update(StoryRequest)
        .where(StoryRequest.id == req.id, StoryRequest.status == "queued")
        .values(status="generating")
    )
    db.commit()
    if res.rowcount != 1:
        return None
    db.refresh(req)
    return req


# --------------------------- generation + judging (split for phasing) ---------------------------
@lru_cache(maxsize=4)
def _prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _structure_prose(client: LLMClient, built: prompt_builder.BuiltPrompt, prose: str):
    """Pass B of two-pass gen: structure finished prose into the JSON schema at LOW temp —
    a mechanical task. Parse-failure retries Pass B only (never regenerates the prose)."""
    sys = _prompt("structure_pass.txt")
    msgs = [{"role": "system", "content": sys}, {"role": "user", "content": prose}]
    raw = client.generate(msgs, model=built.model, json_mode=True, temperature=0.3)
    try:
        return parse(raw), raw
    except ParseError as e:
        log.warning("two-pass structure invalid JSON, retrying once: %s", e)
        retry_user = _prompt("parser_retry.txt")
        raw2 = client.generate(msgs + [{"role": "assistant", "content": raw},
                                       {"role": "user", "content": retry_user}],
                               model=built.model, json_mode=True, temperature=0.2)
        return parse(raw2), raw2


def _generate_story_json(client: LLMClient, built: prompt_builder.BuiltPrompt,
                         extra_messages: list[dict] | None = None, temperature: float | None = None):
    """Generate + parse with a single corrective retry on invalid JSON. Generation runs
    hotter (TB_GEN_TEMPERATURE) for variety; the retry is cooler so it reliably fixes the JSON.
    `extra_messages` (e.g. a 'too similar — make it different' nudge) are appended to the prompt.
    `temperature` overrides the configured default (best-of-N jitters it per candidate).
    When TB_GEN_TWO_PASS, generates PROSE first (no schema pressure) then structures it."""
    s = get_settings()
    temp = temperature if temperature is not None else s.TB_GEN_TEMPERATURE
    if s.TB_GEN_TWO_PASS:
        # Pass A (creative, full temp): plain prose, same user context, same avoidance/angle nudges
        passA = [{"role": "system", "content": _prompt("gen_prose.txt")},
                 {"role": "user", "content": built.user}] + (extra_messages or [])
        prose = client.generate(passA, model=built.model, json_mode=False, temperature=temp)
        return _structure_prose(client, built, prose)  # Pass B (mechanical, low temp)
    base = built.messages + (extra_messages or [])
    raw = client.generate(base, model=built.model, json_mode=True, temperature=temp)
    try:
        return parse(raw), raw
    except ParseError as e:
        log.warning("invalid story JSON, retrying once: %s", e)
        retry_user = _prompt("parser_retry.txt")
        messages = base + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": retry_user},
        ]
        raw2 = client.generate(messages, model=built.model, json_mode=True, temperature=min(temp, 0.4))
        return parse(raw2), raw2


def _variety_avoid_turn(title: str) -> list[dict]:
    """A corrective user turn telling the model its last idea was a near-duplicate."""
    return [{"role": "user", "content": (
        f"That story is too similar to one this child already has (\"{title}\"). Write a "
        "COMPLETELY different story instead — different setting, different characters, a "
        "different little problem, and a different shape. Keep the same JSON format."
    )}]


def _generate_with_variety(db: Session, client: LLMClient, built: prompt_builder.BuiltPrompt,
                           child_id: str, s: Settings):
    """Generate a story, then guard against near-duplicate plots: embed the candidate,
    compare to this child's existing fingerprints, and regenerate (up to TB_VARIETY_MAX_RETRY)
    if cosine >= TB_VARIETY_SIM_THRESHOLD. Returns (story, raw, vec) — vec may be None when the
    guard is off / embedder unavailable. CPU-only; no GPU contention."""
    extra: list[dict] = []
    story = raw = vec = None
    attempts = (s.TB_VARIETY_MAX_RETRY + 1) if s.TB_VARIETY_GUARD else 1
    for attempt in range(attempts):
        story, raw = _generate_story_json(client, built, extra or None)
        if not s.TB_VARIETY_GUARD:
            return story, raw, None
        vec = embed.embed_text(embed.story_text(story.model_dump()), s)
        if vec is None:  # embedder unavailable → guard is a no-op, accept first candidate
            return story, raw, None
        hit = embed.nearest(db, child_id, vec)
        if hit is None or hit[0] < s.TB_VARIETY_SIM_THRESHOLD:
            if hit is not None:
                log.info("variety ok: nearest %.2f (%s)", hit[0], hit[1])
            return story, raw, vec
        log.info("variety: candidate too similar (%.2f to %s) — regenerating (attempt %d/%d)",
                 hit[0], hit[1], attempt + 1, attempts)
        extra = _variety_avoid_turn(hit[2])
    # exhausted retries: accept the last candidate (its vec) rather than fail the request
    log.info("variety: accepting candidate after %d attempt(s)", attempts)
    return story, raw, vec


# best-of-N diversity: a rotating "angle" so the N candidates explore the space rather
# than being three rolls of the same dice. Candidate 0 gets the plain prompt.
_ANGLE_HINTS = [
    "",
    "For this version, open mid-action — drop us straight into something already happening.",
    "For this version, open with a character speaking, or with a small sound or a question.",
    "For this version, pick an unexpected setting and a gentle problem you haven't used before.",
]


def _generate_candidates(db: Session, client: LLMClient, built: prompt_builder.BuiltPrompt,
                         child_id: str, s: Settings, n: int) -> list[dict]:
    """Generate N diverse candidates for one request. N=1 keeps the existing variety-
    regenerate behavior. N>1 jitters temperature ±0.1 and rotates an angle hint per candidate,
    then variety-DISCARDS (not retries) any that collide with the corpus or an already-kept
    sibling. Always returns ≥1. Each item: {dict, vec, raw}."""
    if n <= 1:
        story, raw, vec = _generate_with_variety(db, client, built, child_id, s)
        return [{"dict": story.model_dump(), "vec": vec, "raw": raw}]

    base_temp = s.TB_GEN_TEMPERATURE
    kept: list[dict] = []
    for i in range(n):
        angle = _ANGLE_HINTS[i % len(_ANGLE_HINTS)]
        extra = [{"role": "user", "content": angle}] if angle else None
        temp = max(0.5, min(1.2, base_temp + (0.1 if i % 2 else -0.1) * (i > 0)))
        try:
            story, raw = _generate_story_json(client, built, extra, temperature=temp)
        except (ParseError, LLMError) as e:
            log.warning("candidate %d/%d failed to generate: %s", i + 1, n, e)
            continue
        d = story.model_dump()
        vec = embed.embed_text(embed.story_text(d), s) if s.TB_VARIETY_GUARD else None
        # discard only candidates that duplicate the EXISTING corpus — NOT siblings of the same
        # prompt (those are meant to be similar; the craft judge picks the best, we drop the rest).
        if vec is not None:
            hit = embed.nearest(db, child_id, vec)
            if hit and hit[0] >= s.TB_VARIETY_SIM_THRESHOLD:
                log.info("candidate %d/%d discarded (corpus similarity %.2f to %s)",
                         i + 1, n, hit[0], hit[1])
                continue
        kept.append({"dict": d, "vec": vec, "raw": raw})
    if not kept:  # everything failed or was discarded → fall back to one plain generation
        story, raw, vec = _generate_with_variety(db, client, built, child_id, s)
        kept = [{"dict": story.model_dump(), "vec": vec, "raw": raw}]
    log.info("best-of-N: kept %d/%d candidates", len(kept), n)
    return kept


# --------------------------- long-form "epic" mode ---------------------------
def _extract_json(raw: str) -> dict:
    """Tolerant JSON-object extraction for the outline/chapter passes (strip fences, take the
    outermost {...})."""
    t = (raw or "").strip()
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ParseError("no JSON object in long-form output")
    return json.loads(t[i:j + 1])


def _norm_chars(lst) -> list[dict]:
    out: list[dict] = []
    for c in (lst or []):
        if isinstance(c, str) and c.strip():
            out.append({"name": c.strip(), "role": "character"})
        elif isinstance(c, dict) and c.get("name"):
            out.append({"name": str(c["name"]).strip(), "role": str(c.get("role") or "character")})
    return out


def _generate_longform(db: Session, client: LLMClient, built: prompt_builder.BuiltPrompt,
                       child: Child, s: Settings) -> list[dict]:
    """Generate a ~10-min chaptered story: outline → per-chapter expand with a continuity
    ledger → stitch into one Story dict (flattened `segments` for synth/safety + a `chapters`
    array for the player). Returns a single-candidate list (best-of-N/editor don't apply)."""
    n_ch = max(2, s.TB_LONGFORM_CHAPTERS)
    ch_words = s.TB_LONGFORM_CHAPTER_WORDS
    # 1) outline / beat sheet
    o_user = built.user + (
        f"\n\nPlan this as a LONGER story: about {s.TB_LONGFORM_TARGET_WORDS} words across {n_ch} "
        f"chapters (~{ch_words} words each). The FINAL chapter must be the calm sleepy wind-down.")
    o_raw = client.generate([{"role": "system", "content": _prompt("longform_outline.txt")},
                             {"role": "user", "content": o_user}],
                            model=built.model, json_mode=True, temperature=min(0.85, s.TB_GEN_TEMPERATURE))
    outline = _extract_json(o_raw)
    plan = (outline.get("chapters") or [])[:n_ch]
    if len(plan) < 2:
        raise ParseError("long-form outline produced too few chapters")
    title = outline.get("title") or "A Bedtime Story"
    theme = outline.get("theme") or ""
    # 2) per-chapter expansion with a running continuity ledger
    ch_sys = _prompt("longform_chapter.txt")
    segments: list[dict] = []
    chapters_out: list[dict] = []
    ledger: list[str] = []
    seen: dict[str, dict] = {c["name"].lower(): c for c in _norm_chars(outline.get("characters"))}
    total = len(plan)
    for idx, cp in enumerate(plan):
        is_last = idx == total - 1
        recap = "\n".join(f"Chapter {i + 1}: {sm}" for i, sm in enumerate(ledger)) or "(this is the first chapter)"
        cast = [{"name": c["name"], "role": c.get("role", "character")} for c in seen.values()]
        u = (f"STORY TITLE: {title}\nTHEME: {theme}\n"
             f"RECURRING CHARACTERS: {json.dumps(cast, ensure_ascii=False)}\n"
             f"WHAT HAS HAPPENED SO FAR:\n{recap}\n\n"
             f"Write CHAPTER {idx + 1} of {total} — its purpose: {cp.get('purpose') or cp.get('beat') or ''}. "
             f"Aim for about {ch_words} words.\n"
             + ("This IS the FINAL chapter — wind everything down to calm, safe sleep; resolve everything; no new problems."
                if is_last else
                "End on a soft, gentle hook (never scary) that leads into the next chapter."))
        msgs = [{"role": "system", "content": ch_sys}, {"role": "user", "content": u}]
        c_raw = client.generate(msgs, model=built.model, json_mode=True, temperature=s.TB_GEN_TEMPERATURE)
        try:
            cobj = _extract_json(c_raw)
        except (ParseError, json.JSONDecodeError) as e:
            log.warning("long-form chapter %d parse failed (%s) — retrying once", idx + 1, e)
            c_raw = client.generate(msgs + [{"role": "assistant", "content": c_raw},
                                            {"role": "user", "content": _prompt("parser_retry.txt")}],
                                    model=built.model, json_mode=True, temperature=0.3)
            cobj = _extract_json(c_raw)
        segs = [{"speaker": sg.get("speaker") or "narrator", "text": (sg.get("text") or "").strip(),
                 "emotion": sg.get("emotion") or "gentle"}
                for sg in (cobj.get("segments") or []) if (sg.get("text") or "").strip()]
        if not segs:
            continue
        segments.extend(segs)
        chapters_out.append({"index": idx + 1, "title": cobj.get("title") or f"Chapter {idx + 1}",
                             "summary": cobj.get("summary") or "", "segments": segs})
        ledger.append(cobj.get("summary") or f"(chapter {idx + 1})")
        for c in _norm_chars(cobj.get("characters")):
            seen.setdefault(c["name"].lower(), c)
    if not segments:
        raise ParseError("long-form produced no segments")
    story_dict = {
        "title": title, "age_band": child.age_band,
        "characters": list(seen.values()) or [{"name": child.name, "role": "hero"}],
        "moral": outline.get("moral") or "", "summary": outline.get("summary") or theme,
        "segments": segments, "chapters": chapters_out,
        "estimated_word_count": len(" ".join(sg["text"] for sg in segments).split()),
    }
    vec = embed.embed_text(embed.story_text(story_dict), s) if s.TB_VARIETY_GUARD else None
    log.info("long-form: %d chapters, %d segments, ~%d words", len(chapters_out), len(segments),
             story_dict["estimated_word_count"])
    return [{"dict": story_dict, "vec": vec, "raw": o_raw}]


def generate_request(db: Session, req: StoryRequest, client: LLMClient, gen_model: str,
                     s: Settings, n_candidates: int = 1) -> tuple[str | None, list[dict]]:
    """Generate N candidates for a claimed request. Creates the row UP-FRONT
    (status=generating) so the parent sees it in-progress, fills it with candidate 0 tentatively,
    and advances to `judging`. The judge phase picks the winner among the returned candidates.
    Returns (story_id, candidates); ([]/None) on failure."""
    import time as _t
    child = db.get(Child, req.child_id)
    built = prompt_builder.build(db, req, child.household_id, s, model=gen_model)

    story_row = Story(
        request_id=req.id, child_id=req.child_id, title="…", age_band=child.age_band,
        story_json="{}", gen_model=built.model, status="generating", stage="Writing the story…",
        ambient=getattr(req, "ambient", "off") or "off",
        language=getattr(child, "language", "en") or "en",
    )
    db.add(story_row)
    db.commit()
    db.refresh(story_row)

    longform = bool(getattr(req, "long_form", 0) and s.TB_LONGFORM_ENABLED)
    if longform:
        story_row.kind = "long"
        story_row.stage = "Writing chapter by chapter…"
        db.commit()
    t0 = _t.monotonic()
    try:
        candidates = (_generate_longform(db, client, built, child, s) if longform
                      else _generate_candidates(db, client, built, req.child_id, s, n_candidates))
    except (ParseError, LLMError) as e:
        log.error("generation failed for request %s: %s", req.id, e)
        story_row.status = "failed"; story_row.stage = None
        req.status = "failed"
        db.commit()
        notify.notify_delayed(child.name, f"Story generation failed: {e}", settings=s)
        return None, []

    gen_dt = _t.monotonic() - t0
    first = candidates[0]["dict"]
    story_row.title = first.get("title", "…")
    story_row.age_band = first.get("age_band", child.age_band)
    story_row.story_json = json.dumps(first, ensure_ascii=False)
    story_row.summary = first.get("summary")
    story_row.word_count = len("\n".join(seg.get("text", "") for seg in first.get("segments", [])).split())
    story_row.gen_step_seconds = gen_dt
    story_row.generation_seconds = gen_dt
    story_row.status = "judging"
    story_row.stage = "Safety check…"
    req.status = "generated"
    db.commit()
    log.info("generated %d candidate(s) for request %s (model=%s, %.1fs)",
             len(candidates), req.id, built.model, gen_dt)
    return story_row.id, candidates


def _safety_ok(result) -> bool:
    """A candidate is safety-passing when the judge actually reviewed it and flagged no CONTENT
    category, and the only possible failure is non-content (e.g. word count out of band) — the
    same rule the parent approve-gate uses."""
    return (bool(getattr(result, "judged", False))
            and result.layer in ("", "structural")
            and "concern" not in (result.categories or {}).values())


def _score_candidate(db: Session, household_id: str, cand: dict, client: LLMClient,
                     judge_model: str, s: Settings, long_form: bool = False,
                     double_check: bool = False) -> dict:
    """Run safety + craft on one candidate. Returns everything the selector needs. For long_form
    epics the word-count guard is relaxed and the length-aware craft rubric is used."""
    with (_integration.relaxed_word_bounds() if long_form else contextlib.nullcontext()):
        result, effective = review_story(cand["dict"], household_id, db, client=client,
                                         settings=s, model=judge_model, double_check=double_check)
    craft = craft_judge.judge_craft(effective, client, judge_model, long_form=long_form)
    return {"cand": cand, "result": result, "effective": effective, "craft": craft,
            "safety_ok": _safety_ok(result),
            "craft_overall": (craft or {}).get("overall", 0.0),
            "freshness": ((craft or {}).get("scores") or {}).get("freshness", 0.0)}


def _editor_pass(client: LLMClient, judge_model: str, story_dict: dict, s: Settings) -> dict | None:
    """One revision pass on the winner: concrete imagery, show-don't-tell the moral, vary
    rhythm — without changing plot/length. Runs on the LOADED judge model (avoids a gen↔judge swap;
    the 27B judge is a capable editor). Returns the edited dict, or None to keep the original."""
    system = (PROMPTS_DIR / "editor_pass.txt").read_text(encoding="utf-8")
    try:
        raw = client.generate(
            [{"role": "system", "content": system},
             {"role": "user", "content": json.dumps(story_dict, ensure_ascii=False)}],
            model=judge_model, json_mode=True, temperature=0.4)
        edited, _ = parse(raw), raw
        return edited.model_dump()
    except (ParseError, LLMError) as e:
        log.warning("editor pass failed, keeping original: %s", e)
        return None


def judge_story(db: Session, story: Story, client: LLMClient, judge_model: str, s: Settings,
                candidates: list[dict] | None = None, editor: bool = False) -> None:
    """Judge + select + (optionally) edit. With >1 candidate: safety+craft each, keep the
    highest-craft safety-passing one, log the losers. Always craft-scores the winner and applies
    household substitutions, then advances to needs_approval (or auto-approve for trusted seeds)."""
    import time as _t
    child = db.get(Child, story.child_id)
    t0 = _t.monotonic()
    is_long = story.kind == "long"
    editor = editor and not is_long  # an editor pass over a 2000-word epic is too costly
    cands = candidates or [{"dict": json.loads(story.story_json), "vec": None}]

    # safety + craft on every candidate, then select (epics: relaxed length guard + length-aware craft)
    # requests that can skip the parent gate (child voice requests, trusted seeds) get a double judge
    sr_src = db.get(StoryRequest, story.request_id) if story.request_id else None
    auto_source = bool(sr_src and sr_src.source in {"seed_auto", "voice"})
    scored = [_score_candidate(db, child.household_id, c, client, judge_model, s, long_form=is_long,
                               double_check=auto_source)
              for c in cands]
    pool = [sc for sc in scored if sc["safety_ok"]] or scored
    winner = max(pool, key=lambda sc: (sc["craft_overall"], sc["freshness"]))
    winner_idx = scored.index(winner)
    if len(scored) > 1:
        log.info("best-of-%d: picked #%d craft=%.1f (siblings %s)", len(scored), winner_idx,
                 winner["craft_overall"], [round(sc["craft_overall"], 1) for sc in scored])

    result, effective, craft = winner["result"], winner["effective"], winner["craft"]

    # editor pass on the winner only (re-validate schema + re-run safety; fall back on any failure)
    if editor:
        edited = _editor_pass(client, judge_model, effective, s)
        if edited is not None:
            er, eeff = review_story(edited, child.household_id, db, client=client,
                                    settings=s, model=judge_model, double_check=auto_source)
            if _safety_ok(er):  # keep the edit only if it stays safe
                result, effective = er, eeff
                craft = craft_judge.judge_craft(effective, client, judge_model) or craft
                log.info("editor pass applied to story %s", story.id)

    db.add(SafetyReview(
        story_id=story.id, verdict=result.verdict,
        categories=json.dumps(result.categories, ensure_ascii=False),
        reasons=result.reasons, banned_hits=json.dumps(result.lexical_hits, ensure_ascii=False),
        judge_model=result.judge_model or judge_model,
    ))
    story.title = effective.get("title", story.title)
    story.story_json = json.dumps(effective, ensure_ascii=False)
    story.summary = result.summary or effective.get("summary") or story.summary
    story.word_count = len("\n".join(seg.get("text", "") for seg in effective.get("segments", [])).split())
    story.craft_json = json.dumps(craft, ensure_ascii=False) if craft else None
    story.generation_seconds = (story.generation_seconds or 0.0) + (_t.monotonic() - t0)
    # store the WINNER's fingerprint for the variety corpus
    try:
        wvec = winner["cand"].get("vec") or embed.embed_text(embed.story_text(effective), s)
        embed.store(db, story, wvec, s, commit=False)
    except Exception as e:
        log.warning("variety: failed to store embedding for %s: %s", story.id, e)
    # series auto-grow: fold the winner's cast into the child's world bible so the next
    # "continue the series" reuses them — the parent never has to type the recurring cast. Re-fetch
    # the child here (not the one loaded above) so it's a live, attached row in this transaction.
    if story.request_id:
        try:
            sr0 = db.get(StoryRequest, story.request_id)
            if sr0 and getattr(sr0, "continue_series", 0):
                from tellybear.core import world_bible
                ch = db.get(Child, story.child_id)
                before = world_bible.load(ch.world_bible)
                bible = world_bible.merge_characters(before, effective.get("characters", []))
                ch.world_bible = world_bible.dump(bible)
                db.flush()
                log.info("series: world-bible %d->%d chars for child %s",
                         len(before["characters"]), len(bible["characters"]), ch.id)
        except Exception as e:
            log.warning("series: world-bible merge failed for %s: %s", story.id, e)
    # Trusted auto paths skip the parent gate only when the judge actually reviewed the story and
    # flagged nothing. A non-content fail (e.g. word count out of band) does NOT block auto-approval;
    # anything unjudged, lexically blocked, or failed by the judge always goes to the parent.
    auto = auto_source and _safety_ok(result)
    story.status = "approved" if auto else "needs_approval"
    story.stage = "Recording the voice…" if auto else None
    db.commit()
    if not auto:  # auto-approved seeds go straight to synthesis; no review ping
        target_date = None
        if story.request_id:
            sr2 = db.get(StoryRequest, story.request_id)
            target_date = sr2.target_date if sr2 else None
        notify.notify_ready_to_review(child.name, story.id, target_date, settings=s)
    log.info("story %s reviewed (verdict=%s, craft=%s, judge=%s, auto=%s)", story.id,
             result.verdict, winner["craft_overall"], judge_model, auto)


def _fail_judging(db: Session, sid: str, err: Exception, s: Settings) -> None:
    """Judging crashed: never leave the story parked in 'judging' — fail it and tell the parent."""
    try:
        db.rollback()
        story = db.get(Story, sid)
        if story is None:
            return
        story.status, story.stage = "failed", None
        db.commit()
        child = db.get(Child, story.child_id)
        notify.notify_delayed(child.name if child else "child", f"Safety review failed: {err}", settings=s)
    except Exception as e:
        log.warning("could not mark story %s failed after judge error: %s", sid, e)


def process_request(db: Session, req: StoryRequest, client: LLMClient | None = None) -> str | None:
    """Inline generate→judge for a single claimed request (now-path / always-on worker)."""
    s = get_settings()
    client = client or get_llm_client()
    eff = settings_store.effective(db, s)
    gen_model = resolve_gen_model(req, eff, s)
    judge_model = eff.judge_model(s)
    if _is_local(gen_model, s):
        gpu.prepare_for_llm(s)       # free the GPU TTS model so llama-swap can load the LLM (16 GB)
        gpu.load(gen_model, s)       # warm + wait until loaded (a cold model 502s mid-startup)
    sid, candidates = generate_request(db, req, client, gen_model, s, n_candidates=s.TB_GEN_CANDIDATES_NOW)
    if sid is None:
        return None
    story = db.get(Story, sid)
    # if the judge is a DIFFERENT local model, swap to it (warm + wait) before judging so the
    # cold-load swap doesn't 502 the judge call
    if judge_model != gen_model and _is_local(judge_model, s):
        gpu.load(judge_model, s)
    try:
        judge_story(db, story, client, judge_model, s, candidates=candidates, editor=s.TB_GEN_EDITOR_NOW)
    except Exception as e:
        log.exception("judging failed for story %s: %s", sid, e)
        _fail_judging(db, sid, e, s)
    return sid


# --------------------------- synthesis (on approval) ---------------------------
def claim_approved_story(db: Session) -> Story | None:
    story = db.execute(
        select(Story).where(Story.status == "approved").order_by(Story.updated_at.asc()).limit(1)
    ).scalars().first()
    if story is None:
        return None
    res = db.execute(
        update(Story).where(Story.id == story.id, Story.status == "approved")
        .values(status="synthesizing", stage="Recording the voice…")
    )
    db.commit()
    if res.rowcount != 1:
        return None
    db.refresh(story)
    return story


def synthesize_story(db: Session, story: Story) -> str | None:
    from tellybear.tts import synth  # local import; keeps worker importable without TTS deps
    s = get_settings()
    try:
        synth.synthesize(db, story, s)
        return story.id
    except Exception as e:
        log.exception("synthesis failed for story %s: %s", story.id, e)
        story.status = "failed"
        db.commit()
        child = db.get(Child, story.child_id)
        notify.notify_delayed(child.name if child else "child", f"Audio synthesis failed: {e}", settings=s)
        return None


# --------------------------- always-on loop unit ---------------------------
def process_once(client: LLMClient | None = None) -> str | None:
    """One unit of work for the always-on worker: synthesis first (turn approvals into audio
    fast), else a `now`-priority request. Batch requests are left for the evening timer."""
    db = SessionLocal()
    try:
        story = claim_approved_story(db)
        if story is not None:
            return synthesize_story(db, story)
        req = claim_next(db, now_only=True)
        if req is None:
            return None
        return process_request(db, req, client)
    finally:
        db.close()


# --------------------------- phased batch (evening timer) ---------------------------
def run_batch(client: LLMClient | None = None) -> int:
    """Phased drain of ALL queued requests: generate-all (grouped by model, one load each) →
    GPU hand-off → judge-all (one judge load, or Opus). Avoids per-story model swaps."""
    client = client or get_llm_client()
    s = get_settings()
    db = SessionLocal()
    try:
        eff = settings_store.effective(db, s)
        # claim everything queued
        claimed: list[StoryRequest] = []
        while True:
            req = claim_next(db, now_only=False)
            if req is None:
                break
            claimed.append(req)
        if not claimed:
            return 0

        # group by resolved gen model so each model loads exactly once
        by_model: dict[str, list[StoryRequest]] = defaultdict(list)
        for req in claimed:
            by_model[resolve_gen_model(req, eff, s)].append(req)

        # ---- phase 1: generate-all ----
        if any(_is_local(m, s) for m in by_model):
            gpu.prepare_for_llm(s)  # free the GPU TTS model before loading any LLM
        # candidates survive the gen→judge GPU hand-off in memory (same worker process)
        story_ids: list[str] = []
        cand_by_sid: dict[str, list[dict]] = {}
        for gen_model, reqs in by_model.items():
            if _is_local(gen_model, s):
                gpu.load(gen_model, s)
            for req in reqs:
                try:
                    sid, cands = generate_request(db, req, client, gen_model, s,
                                                  n_candidates=s.TB_GEN_CANDIDATES)
                except Exception as e:  # one bad request must not abort the whole batch
                    log.exception("batch generation failed for request %s: %s", req.id, e)
                    db.rollback()
                    r0 = db.get(StoryRequest, req.id)
                    if r0 is not None:
                        r0.status = "failed"
                        db.commit()
                    continue
                if sid:
                    story_ids.append(sid)
                    cand_by_sid[sid] = cands

        if not story_ids:
            return 0

        # ---- GPU hand-off ----
        if any(_is_local(m, s) for m in by_model):
            gpu.unload_llm(s)
            gpu.wait_free(9000, s)

        # ---- phase 2: judge-all (single judge model): select best-of-N + craft + editor ----
        judge_model = eff.judge_model(s)
        if _is_local(judge_model, s):
            gpu.load(judge_model, s)
        for sid in story_ids:
            story = db.get(Story, sid)
            if story is None:
                continue
            try:
                judge_story(db, story, client, judge_model, s,
                            candidates=cand_by_sid.get(sid), editor=s.TB_GEN_EDITOR)
            except Exception as e:
                log.exception("batch judge failed for story %s: %s", sid, e)
                _fail_judging(db, sid, e, s)
        log.info("batch done: generated+judged %d stor(ies)", len(story_ids))
        # one end-of-batch summary ping instead of silence when all goes well
        try:
            finished = [db.get(Story, sid) for sid in story_ids]
            finished = [st for st in finished if st is not None]
            awaiting = sum(1 for st in finished if st.status == "needs_approval")
            crafts = []
            for st in finished:
                if st.craft_json:
                    try:
                        crafts.append(float(json.loads(st.craft_json).get("overall")))
                    except Exception:
                        pass
            avg = f"{sum(crafts) / len(crafts):.1f}" if crafts else "n/a"
            notify.notify("Tellybear batch done",
                          f"{len(finished)} generated · {awaiting} awaiting review · craft avg {avg}",
                          settings=s)
        except Exception as e:
            log.warning("batch summary notify failed: %s", e)
        return len(story_ids)
    finally:
        db.close()


def drain(client: LLMClient | None = None) -> int:
    """Compatibility entrypoint used by the batch timer — now phased."""
    return run_batch(client)


# --------------------------- watchdog ---------------------------
# Bounded-retry budget for a wedged 'generating' request: no retry-count column exists, so age since CREATION (not just staleness since the
# last update) stands in for "already been through the requeue loop several times" — a request
# whose age exceeds ~3 stale cycles has almost certainly been requeued and gone stale repeatedly.
# Past that point stop retrying (mark failed) and alert, instead of looping the queue forever on a
# permanently broken model/GPU state.
WATCHDOG_STALE_MINUTES = 15
WATCHDOG_GIVE_UP_MINUTES = 3 * WATCHDOG_STALE_MINUTES


def _recover_gpu_after_stall(n: int, gave_up: int = 0) -> None:
    """A job was stuck long enough for the watchdog to requeue/fail it — the historically most
    common cause is the Chatterbox<->LLM VRAM hand-off wedging (root cause: `systemctl stop`/
    `/api/unload` reporting success without the ~1GB Torch CUDA context actually clearing, so the
    next LLM load OOMs/502s and the job hangs). Force a clean GPU slate before the next attempt so
    the same request doesn't immediately wedge again, and alert since this indicates a real stall
    (not routine load latency)."""
    s = get_settings()
    try:
        gpu.stop_tts_service(s)   # full stop + poll + SIGKILL-escalate on timeout (see gpu.py)
        gpu.unload_llm(s)
        gpu.wait_free(9000, s, poll_timeout=30)
    except Exception as e:
        log.warning("watchdog GPU recovery failed: %s", e)
    try:
        detail = f"{n} stuck job(s) recovered; GPU force-cleared before retry"
        if gave_up:
            detail += f" ({gave_up} gave up after repeated stalls — check the queue)"
        notify.notify("Tellybear watchdog recovered a stuck job", detail, settings=s)
    except Exception as e:
        log.warning("watchdog notify failed: %s", e)


def requeue_stale(db: Session, minutes: int = WATCHDOG_STALE_MINUTES) -> int:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=minutes)).isoformat()
    give_up_cutoff = (now - timedelta(minutes=WATCHDOG_GIVE_UP_MINUTES)).isoformat()

    # requests stuck 'generating' AND old enough to have already cycled through the requeue loop
    # a few times → give up instead of requeuing forever (bounded retry budget, see module notes).
    give_up_ids = db.execute(
        select(StoryRequest.id)
        .where(StoryRequest.status == "generating", StoryRequest.updated_at < cutoff,
               StoryRequest.created_at < give_up_cutoff)
    ).scalars().all()
    res0 = 0
    if give_up_ids:
        res0 = db.execute(
            update(StoryRequest).where(StoryRequest.id.in_(give_up_ids)).values(status="failed")
        ).rowcount

    # everything else stale gets the normal retry (requeue for a fresh attempt)
    res = db.execute(
        update(StoryRequest)
        .where(StoryRequest.status == "generating", StoryRequest.updated_at < cutoff,
               StoryRequest.created_at >= give_up_cutoff)
        .values(status="queued")
    )
    res2 = db.execute(
        update(Story)
        .where(Story.status == "synthesizing", Story.updated_at < cutoff)
        .values(status="approved", stage="Recording the voice…")
    )
    # stories stuck mid-generation/judging (worker crashed) → mark failed so they don't show
    # as in-progress forever; the request watchdog above re-queues the request for a fresh run
    res3 = db.execute(
        update(Story)
        .where(Story.status.in_(("generating", "judging")), Story.updated_at < cutoff)
        .values(status="failed", stage=None)
    )
    db.commit()
    n = res.rowcount + res2.rowcount + res3.rowcount + res0
    if n:
        _recover_gpu_after_stall(n, gave_up=len(give_up_ids))
    return n


def run_loop(poll_seconds: float = 3.0) -> None:
    log.info("worker loop starting (poll=%ss)", poll_seconds)
    while True:
        try:
            sid = process_once()
            if sid is None:
                time.sleep(poll_seconds)
        except Exception as e:  # never let the loop die
            log.exception("worker iteration error: %s", e)
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_loop()
