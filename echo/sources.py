"""Shared types for data arriving from any listening source (Spotify now; Last.fm or exports later).

Every source adapter converts its own format into SourceArtist rows. Everything
downstream (storage, entity resolution, matching) only ever sees this type,
so adding a new source never touches the matching code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceArtist:
    """An artist as a source reports it, before it's matched to our own artist table."""

    name: str
    time_range: str
    rank: int
    spotify_id: str | None = None
    mbid: str | None = None
