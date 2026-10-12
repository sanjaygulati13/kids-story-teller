# Safety check

Layered, conservative, fail-closed review for AI-generated bedtime stories.

## Files
- `safety_check.py` — the implementation. `run_safety(story, banned_terms, llm_generate, judge_prompt_path, ...)` returns `(SafetyResult, effective_story)`. Use `effective_story` (household substitutions applied) for TTS when the result passes.
- `../prompts/judge_system.txt` — the judge instructions (8 categories + calibration examples).
- `redteam.jsonl` — deliberately good/bad stories with the categories each should trip.
- `eval_judge.py` — runs the judge over the red-team set and reports detection / false-alarm rates.

## Layers
1. **L0 structural** — well-formed JSON, in-band word count, valid speakers, no empty segments. Catches truncated/garbage output. A structural miss is recorded but does **not** skip the content layers below: the story is still lexically checked and judged.
2. **L1 lexical (backstop)** — household banned terms (substituted first) + a tiny `HARD_BLOCK` list. Deliberately crude; a false positive only forces a human look, a false negative could reach a child, so it biases toward blocking. NOT the real check.
3. **L2 LLM judge (the real check)** — semantic review across: violence, scary, distressing, inappropriate, unsafe_behavior, vocab_age_fit, ending_calm, ip_characters. Anything other than an exact `"ok"` for a category counts as a concern → fail. `SafetyResult.judged` records whether the judge actually ran.
4. **L3 human gate** — the parent approval in the app (not in this module).

## Rules that matter
- **Generator ≠ judge.** Pass an `llm_generate` that targets a *different/stronger* model than the one that wrote the story.
- **Fail closed.** Unparseable or incomplete judge output is treated as unsafe.
- **Conservative.** When unsure, the judge flags; a `fail` cannot be one-tapped past in the UI.
- **Skipping the human gate.** Only child voice requests (and trusted seed batches) can skip parent approval. They always run with `double_check=True` (two independent judge passes) and are auto-approved only if the judge actually ran and flagged nothing; a lexical block, a judge `fail`, or an unparseable judge reply always goes to the parent. Run the eval below before you rely on this.

## Validate before you trust it
```bash
export TB_JUDGE_URL=http://localhost:8080/v1
export TB_JUDGE_MODEL=<your judge model id>
python eval_judge.py        # exits non-zero if ANY unsafe story slips through
```
Run it several times. A judge that ever *misses* an unsafe story is not ready for an unattended path; a judge with occasional *false alarms* on the safe stories is fine (a human just confirms). Treat consistency across runs as a quality signal, and add your own red-team cases as you think of failure modes.
