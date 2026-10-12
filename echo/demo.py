"""Synthetic listeners so you can see matching work before your friends sign up.

Each demo listener has a primary scene, often a neighbouring secondary scene,
and maybe a few superstars that everyone listens to. Within a scene, earlier
artists are more popular, so profiles overlap realistically: everyone in a
scene shares the big names, and the niche picks are what set people apart.

Demo listeners are flagged `is_demo` in the database. They can be shown as
sample matches (SHOW_DEMO_USERS=true) but can never be contacted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from echo.sources import SourceArtist

SCENES: dict[str, list[str]] = {
    "indie": [
        "Radiohead", "Arctic Monkeys", "The Strokes", "The National", "Pixies", "Interpol",
        "Yeah Yeah Yeahs", "Modest Mouse", "Pavement", "Big Thief", "Alvvays", "Car Seat Headrest",
    ],
    "hip_hop": [
        "Kendrick Lamar", "J. Cole", "Tyler, The Creator", "Mac Miller", "MF DOOM", "A Tribe Called Quest",
        "Denzel Curry", "JID", "Freddie Gibbs", "Earl Sweatshirt", "Little Simz", "Joey Bada$$",
    ],
    "rnb": [
        "Frank Ocean", "SZA", "Daniel Caesar", "Steve Lacy", "Brent Faiyaz", "Sade",
        "D'Angelo", "Erykah Badu", "Solange", "Jorja Smith", "Kali Uchis", "Snoh Aalegra",
    ],
    "electronic": [
        "Fred again..", "Disclosure", "Kaytranada", "Jamie xx", "Four Tet", "Caribou",
        "Bonobo", "Aphex Twin", "Burial", "Floating Points", "Bicep", "Jon Hopkins",
    ],
    "jazz": [
        "Miles Davis", "John Coltrane", "Kamasi Washington", "Bill Evans", "Thelonious Monk", "Herbie Hancock",
        "BADBADNOTGOOD", "Robert Glasper", "Alice Coltrane", "Charles Mingus", "Nubya Garcia", "Esperanza Spalding",
    ],
    "latin": [
        "Bad Bunny", "Rosalía", "Karol G", "Feid", "Rauw Alejandro", "J Balvin",
        "Peso Pluma", "Ozuna", "Natalia Lafourcade", "Bomba Estéreo", "Arca", "Ela Minus",
    ],
    "metal": [
        "Metallica", "Deftones", "Tool", "System of a Down", "Black Sabbath", "Mastodon",
        "Gojira", "Sleep Token", "Opeth", "Megadeth", "Converge", "Slayer",
    ],
}

NEIGHBOURS: dict[str, list[str]] = {
    "indie": ["electronic", "metal", "rnb"],
    "hip_hop": ["rnb", "jazz", "electronic"],
    "rnb": ["hip_hop", "latin", "electronic"],
    "electronic": ["indie", "rnb", "hip_hop"],
    "jazz": ["hip_hop", "rnb", "electronic"],
    "latin": ["rnb", "hip_hop", "electronic"],
    "metal": ["indie", "electronic", "hip_hop"],
}

SUPERSTARS = ["Taylor Swift", "Drake", "The Weeknd", "Billie Eilish", "Doja Cat", "Harry Styles"]


@dataclass(frozen=True)
class DemoListener:
    name: str
    primary_scene: str
    secondary_scene: str | None
    artists: list[SourceArtist]


def _weighted_sample(rng: random.Random, items: list[str], k: int, skew: float = 0.8) -> list[str]:
    """Sample without replacement, favouring earlier (more popular) items.

    Efraimidis-Spirakis: give each item the key u ** (1 / weight) and keep the k largest.
    With weight = 1 / (position + 1) ** skew, 1 / weight is just (position + 1) ** skew.
    """
    keyed = [(rng.random() ** ((i + 1) ** skew), item) for i, item in enumerate(items)]
    keyed.sort(reverse=True)
    return [item for _, item in keyed[:k]]


def _perturb(rng: random.Random, ranking: list[str], keep: float) -> list[str]:
    """A noisier, shorter version of a ranking, for the medium and short-term lists."""
    kept = [artist for artist in ranking if rng.random() < keep]
    for i in range(len(kept) - 1):
        if rng.random() < 0.35:
            kept[i], kept[i + 1] = kept[i + 1], kept[i]
    return kept or ranking[:1]


def generate_listeners(count: int = 150, seed: int = 7) -> list[DemoListener]:
    rng = random.Random(seed)
    scenes = list(SCENES)
    listeners: list[DemoListener] = []
    width = len(str(count))
    for n in range(1, count + 1):
        primary = rng.choice(scenes)
        secondary = rng.choice(NEIGHBOURS[primary]) if rng.random() < 0.6 else None

        picks = _weighted_sample(rng, SCENES[primary], rng.randint(6, 10))
        if secondary:
            picks += _weighted_sample(rng, SCENES[secondary], rng.randint(2, 5))
        picks += rng.sample(SUPERSTARS, rng.randint(0, 3))

        # Long-term ranking: mostly primary scene first, with some shuffling.
        long_term = sorted(picks, key=lambda a: (a not in SCENES[primary]) + rng.random() * 1.2)
        medium_term = _perturb(rng, long_term, keep=0.8)
        short_term = _perturb(rng, long_term, keep=0.6)

        artists = [
            SourceArtist(name=artist, time_range=time_range, rank=rank)
            for time_range, ranking in (
                ("long_term", long_term),
                ("medium_term", medium_term),
                ("short_term", short_term),
            )
            for rank, artist in enumerate(ranking, start=1)
        ]
        listeners.append(
            DemoListener(
                name=f"Demo listener {n:0{width}d}",
                primary_scene=primary,
                secondary_scene=secondary,
                artists=artists,
            )
        )
    return listeners
