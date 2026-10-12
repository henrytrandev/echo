"""Turn raw "top artists" lists into a single affinity score per artist.

A source like Spotify gives three ranked lists per user (short, medium and long
term). We collapse them into one number per artist:

    affinity(artist) = sum over lists of  time_range_weight * rank_weight(rank)

* rank_weight decays with position (1 / log2(rank + 1), the same discount used
  in DCG), so a user's #1 artist counts more than their #50, without the
  bottom of the list collapsing to zero.
* Long-term taste is weighted highest because it is the most stable signal of
  who someone is musically; short-term picks still count so profiles stay fresh.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass

TIME_RANGE_WEIGHTS: Mapping[str, float] = {
    "long_term": 0.5,
    "medium_term": 0.3,
    "short_term": 0.2,
}


@dataclass(frozen=True, slots=True)
class TasteSignal:
    """One artist at one position in one of a user's ranked lists."""

    artist: Hashable
    time_range: str
    rank: int  # 1-based position in that list


def rank_weight(rank: int) -> float:
    """DCG-style position discount: 1.0 for rank 1, ~0.63 for rank 2, ~0.18 for rank 50."""
    if rank < 1:
        raise ValueError(f"rank must be >= 1, got {rank}")
    return 1.0 / math.log2(rank + 1)


def affinity(
    signals: Iterable[TasteSignal],
    time_range_weights: Mapping[str, float] = TIME_RANGE_WEIGHTS,
) -> dict[Hashable, float]:
    """Collapse a user's ranked lists into one non-negative score per artist."""
    scores: dict[Hashable, float] = defaultdict(float)
    for signal in signals:
        weight = time_range_weights.get(signal.time_range)
        if weight is None:
            raise ValueError(f"unknown time range {signal.time_range!r}")
        scores[signal.artist] += weight * rank_weight(signal.rank)
    return dict(scores)


def fingerprint(affinities: Mapping[Hashable, float], size: int = 50) -> list[float]:
    """A small picture of someone's taste: one bar per artist, the strongest scaled to 1.0.

    Keeps the `size` strongest artists and orders them by a hash of the artist id rather
    than by score, so the bars look like a barcode and the same artists always come out
    in the same order. The bars carry no names, so they can be shown for anonymous matches.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    strongest = sorted(affinities.items(), key=lambda item: item[1], reverse=True)[:size]
    if not strongest or strongest[0][1] <= 0:
        return []
    top = strongest[0][1]
    ordered = sorted(strongest, key=lambda item: hashlib.sha1(str(item[0]).encode()).hexdigest())
    return [round(score / top, 3) for _, score in ordered]
