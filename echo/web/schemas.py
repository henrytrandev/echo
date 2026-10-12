"""Response and request bodies. These also generate the interactive API docs at /docs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ConnectionStatus = Literal["none", "requested", "incoming", "connected"]
Verdict = Literal["like", "dislike"]


class MeOut(BaseModel):
    id: str
    display_name: str
    is_demo: bool
    discoverable: bool
    can_save_tracks: bool
    """True when this account can add songs to its Echo playlist (real account, playlist access granted)."""
    last_ingested_at: datetime | None
    top_artists: list[str]
    fingerprint: list[float] = Field(default_factory=list)
    """Up to 50 bars, one per artist, the strongest scaled to 1.0 (see matching.fingerprint)."""


class MeUpdate(BaseModel):
    discoverable: bool


class ArtistOut(BaseModel):
    id: int
    name: str
    verdict: Verdict | None = None
    """Your feedback on this artist, if you've given any."""
    because: str | None = None
    """For a suggestion: the artist of yours it is most often heard alongside ("because you like ...")."""


class MatchOut(BaseModel):
    user_id: str
    name: str
    """An anonymous alias until both people connect; then their display name."""
    is_demo: bool
    score: float
    overlap: float
    discovery_for_you: float
    discovery_for_them: float
    shared_artists: list[ArtistOut]
    they_could_put_you_on: list[ArtistOut]
    you_could_put_them_on: list[ArtistOut]
    connection: ConnectionStatus
    fingerprint: list[float] = Field(default_factory=list)
    """Their taste as unnamed bars, so you get a feel for it before you connect."""


class ConnectionOut(BaseModel):
    user_id: str
    name: str
    status: ConnectionStatus


class TrackOut(BaseModel):
    artist_id: int
    artist_name: str
    track_id: str | None
    track_name: str | None
    uri: str | None
    """A spotify:track: URI for the embedded player, or None when no playable song was found."""
    spotify_url: str
    verdict: Verdict | None = None


class SaveTrackIn(BaseModel):
    track_id: str = Field(pattern=r"^[0-9A-Za-z]{22}$", description="A Spotify track ID")


class SaveTrackOut(BaseModel):
    playlist_url: str


class FeedbackIn(BaseModel):
    verdict: Verdict | None
    """None removes your feedback."""
    track_id: str | None = Field(default=None, pattern=r"^[0-9A-Za-z]{22}$")


class FeedbackOut(BaseModel):
    artist_id: int
    verdict: Verdict | None
