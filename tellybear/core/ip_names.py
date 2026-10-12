"""Deterministic copyrighted/branded character-name check.

A small, unambiguous blocklist so the parent API can 422 an obvious IP name at creation
time. This is a backstop; the generator prompt and the judge (ip_characters category) are the
real defenses. Matching is whole-word, case-insensitive.
"""
from __future__ import annotations

import re

# Common franchise/character names children's stories drift toward. Extend freely.
KNOWN_IP = {
    "elsa", "anna", "olaf", "bluey", "bingo", "spider-man", "spiderman", "batman",
    "superman", "peppa", "peppa pig", "paw patrol", "chase", "marshall", "skye",
    "pokemon", "pokémon", "pikachu", "mickey", "mickey mouse", "minnie", "donald duck",
    "moana", "elsa", "frozen", "cocomelon", "paddington", "winnie the pooh", "tigger",
    "dora", "thomas the tank engine", "sonic", "mario", "luigi", "yoshi", "barbie",
    "hello kitty", "shrek", "buzz lightyear", "woody", "simba", "nemo", "dory",
    "harry potter", "hermione", "gabby", "bibble",
}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def is_known_ip(name: str) -> bool:
    n = _norm(name)
    if n in KNOWN_IP:
        return True
    # also catch a single-token match against multi-word entries' first token where unambiguous
    return any(n == _norm(ip) for ip in KNOWN_IP)
