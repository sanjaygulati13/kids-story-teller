# Reference stories

Four hand-written stories in the exact **Story JSON** format the generator produces. Use them to:

- **Try the whole flow before you have an LLM set up** — import them, approve them in the Review
  tab, let the worker narrate them, and play them in the child player or on the Tellybear device.
- **Check your TTS setup** — they use several speaking characters, emotion tags and a sleepy
  wind-down, so you'll hear the multi-voice narration and the pacing.
- **See what "good" looks like** — each has a single emotional core, a refrain the child can say
  along with, a small turn, and a slow ending. That's the bar the generation prompt aims for.

| File | Title | Age band | Setting |
|---|---|---|---|
| `01-the-little-tugboat.json` | Toot-Toot, Right Beside You | early (3–5) | a harbor at dusk |
| `02-the-bakers-lost-yawn.json` | Knead, Fold, and Rest | early (3–5) | a midnight bakery |
| `03-the-cloud-mapmaker.json` | Draw What You See | older (6–8) | a sky-ship mapping clouds |
| `04-the-night-train.json` | Every Track Leads Somewhere | older (6–8) | a sleeper train across the desert |

`{{child}}` is replaced with your child's name on import, so they're the hero.

## Import

```bash
.venv/bin/python scripts/import_stories.py --child Sam examples/stories/
```

They arrive in the parent app's **Review** tab. Approve one and the worker narrates it (a few
minutes on a GPU), then it shows up in the child's player. Add `--approve` to skip the Review tab.

Imported stories don't go through the LLM safety judge, so only import stories you've read.

## Story JSON

```jsonc
{
  "title": "Toot-Toot, Right Beside You",
  "age_band": "early",                 // "early" (3–5) or "older" (6–8)
  "characters": [                      // everyone who speaks or features
    { "name": "Pip", "role": "friend", "archetype": "a small, brave tugboat" }
  ],
  "moral": "One short, gentle sentence.",
  "summary": "One paragraph a parent can read in ten seconds.",
  "segments": [                        // the story, in order
    { "speaker": "narrator", "text": "…", "emotion": "cheerful" },
    { "speaker": "Pip", "text": "…", "emotion": "hopeful" }   // speaker = "narrator" or a character name
  ],
  "estimated_word_count": 390
}
```

`emotion` steers the narrator's expressiveness (Chatterbox) and should drift calmer toward the
end; the last segment or two should be `soothing` or `sleepy`. The full rules the generator follows
are in [`prompts/gen_system.txt`](../../prompts/gen_system.txt).
