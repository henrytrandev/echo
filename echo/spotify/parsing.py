"""Turn Spotify JSON into our own types. Defensive about missing fields, since the API changes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from echo.names import normalize_artist_name
from echo.sources import SourceArtist


@dataclass(frozen=True)
class SpotifyProfile:
    account_id: str
    display_name: str


def parse_profile(payload: Mapping[str, Any]) -> SpotifyProfile:
    # `account_id` (added May 2026) is guaranteed never to change; fall back to `id` for older responses.
    account_id = payload.get("account_id") or payload.get("id")
    if not account_id:
        raise ValueError("profile response has neither account_id nor id")
    display_name = payload.get("display_name") or payload.get("id") or "Spotify listener"
    return SpotifyProfile(account_id=str(account_id), display_name=str(display_name))


def parse_top_artists_page(
    payload: Mapping[str, Any],
    time_range: str,
    rank_offset: int,
) -> tuple[list[SourceArtist], str | None]:
    """One page of /me/top/artists. Ranks continue from `rank_offset` across pages."""
    artists: list[SourceArtist] = []
    for position, item in enumerate(payload.get("items") or [], start=1):
        if not isinstance(item, Mapping) or not item.get("name"):
            continue  # skip malformed entries rather than failing the whole import
        artists.append(
            SourceArtist(
                name=str(item["name"]),
                time_range=time_range,
                rank=rank_offset + position,
                spotify_id=item.get("id"),
            )
        )
    next_url = payload.get("next")
    return artists, str(next_url) if next_url else None


@dataclass(frozen=True)
class TrackChoice:
    """One playable song chosen to represent an artist."""

    track_id: str
    name: str
    uri: str


@dataclass(frozen=True)
class CreatedPlaylist:
    playlist_id: str
    url: str


def parse_artist_search(payload: Mapping[str, Any], name: str) -> str | None:
    """Spotify ID of the search result whose name matches `name`, or None.

    Only an exact name match (after normalization) counts: picking the top result
    blindly would link an obscure artist to whoever Spotify ranks first.
    """
    wanted = normalize_artist_name(name)
    for item in (payload.get("artists") or {}).get("items") or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        if normalize_artist_name(str(item.get("name", ""))) == wanted:
            return str(item["id"])
    return None


def parse_track_search(payload: Mapping[str, Any], artist_spotify_id: str) -> TrackChoice | None:
    """The first search result actually credited to this artist (search also returns covers and features)."""
    for item in (payload.get("tracks") or {}).get("items") or []:
        if not isinstance(item, Mapping) or not item.get("id") or not item.get("uri"):
            continue
        credited = {a.get("id") for a in item.get("artists") or [] if isinstance(a, Mapping)}
        if artist_spotify_id in credited:
            return TrackChoice(track_id=str(item["id"]), name=str(item.get("name") or "Untitled"), uri=str(item["uri"]))
    return None


def parse_created_playlist(payload: Mapping[str, Any]) -> CreatedPlaylist:
    playlist_id = payload.get("id")
    if not playlist_id:
        raise ValueError("playlist response has no id")
    url = (payload.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{playlist_id}"
    return CreatedPlaylist(playlist_id=str(playlist_id), url=str(url))


def error_reason(payload: Any) -> str:
    """Best-effort human-readable reason from a Spotify error body."""
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            return str(error.get("reason") or error.get("message") or "")
        if isinstance(error, str):
            return str(payload.get("error_description") or error)
    return ""
