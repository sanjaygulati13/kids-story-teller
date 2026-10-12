"""Creative breadth palette.

The variety guard (embeddings) stops near-DUPLICATES, but the small model still gravitates to one
"small world" — cozy nature at night (ponds, gardens, lanterns, the moon). To widen the scope we
inject a random sample of very DIFFERENT settings + an optional "spark" into every generation, so
the model is pulled out of its default gravity well toward a genuinely varied world.

These are bedtime-SAFE but deliberately span many kinds of places, cultures, and registers.
"""
from __future__ import annotations

import random

SETTINGS = [
    # nature, but varied
    "a misty bamboo forest at dawn", "a field of giant sunflowers", "a quiet desert oasis under the stars",
    "a mossy waterfall grotto", "an autumn orchard heavy with fruit", "a warm green jungle in soft rain",
    # water & coast
    "a coral reef glowing softly", "a tide pool full of tiny creatures", "a lighthouse on a foggy coast",
    "an underwater kelp forest", "a houseboat drifting down a calm river", "a frozen lake dotted with skaters",
    # sky & space
    "a little town built in the clouds", "a hot-air-balloon festival", "a cozy moon base",
    "the broad gentle back of a flying whale", "a workshop where stars are polished",
    # cities, towns, cultures
    "a lantern-lit night market in an old harbor town", "a snowy mountain village with steamy teahouses",
    "a bustling spice bazaar at dusk", "a canal city with humming gondoliers", "a desert caravan camp at night",
    "a great river covered in floating kites", "a town tucked inside a giant seashell",
    "a hillside town of crooked, colorful houses", "a paper-lantern festival on a warm evening",
    # transport
    "a sleeper train winding through the mountains", "a tram climbing a steep seaside town",
    "a small, slow airship", "a tiny world inside a rain puddle",
    # indoor / workshops / cozy
    "a clockmaker's workshop full of soft ticking", "a grand library where the books nap",
    "a bakery that never quite sleeps", "an old toy-repair shop", "a greenhouse lit by fireflies",
    "a music shop where the instruments hum", "an attic full of forgotten treasures",
    "a teahouse on a rainy afternoon",
    # gently fantastical
    "a village where everyone is no bigger than a thumb", "a land where it rains flower petals",
    "an island that drifts slowly across the sea", "a forest where the trees trade whispered secrets",
    "a town painted entirely in moonlight", "a mountain that hums lullabies",
    "a market that only appears in dreams", "a meadow where tired shadows go to rest",
]

SPARKS = [
    "a map that quietly redraws itself", "a homesick little raindrop", "a shy thundercloud learning to rumble softly",
    "a lantern that hums lullabies", "a clockwork sparrow that has lost its song",
    "a kite that whispers the wind's secrets", "a snow globe whose tiny town waves back",
    "a paper boat carrying one very brave ant", "a key that can't remember which door is home",
    "a star that fell asleep too early", "a teapot that pours a different weather each time",
    "a seashell that remembers every wave it ever heard", "a candle whose flame tells little stories",
    "a bridge that only appears for someone kind", "a compass that points toward whoever needs help",
    "a bell that rings only when someone is brave", "a garden that grows best in the dark",
    "a small robot who collects lost sounds", "a moon that needs help finding its way home",
    "a violin that plays the colors of the evening sky", "a door in an old tree that leads somewhere cozy",
    "a cloud carrying someone's forgotten dream", "a pair of shoes that only ever want to dance",
]


def fresh_inspiration_block(rng: random.Random, n_settings: int = 7, n_sparks: int = 2) -> str:
    """A sampled block of diverse settings + a spark, to widen the story's world each generation."""
    sets = rng.sample(SETTINGS, min(n_settings, len(SETTINGS)))
    sparks = rng.sample(SPARKS, min(n_sparks, len(SPARKS)))
    lines = ["FRESH WORLDS — if the request above already names a specific setting, USE THAT and ignore "
             "this list. Otherwise range WIDE (avoid the cozy-nature-at-night rut): set the story somewhere "
             "clearly different from the recent ones — consider one of these, or invent something equally "
             "far-flung and bedtime-safe:"]
    lines += [f"- {x}" for x in sets]
    lines.append("Optional little spark to build around:")
    lines += [f"- {x}" for x in sparks]
    return "\n".join(lines)
