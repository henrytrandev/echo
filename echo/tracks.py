"""Songs: finding one to play for an artist, and saving songs to the user's Echo playlist."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from echo.ingest import ensure_fresh, load_tokens, store_tokens
from echo.models import Artist, ArtistTrack, User, UserPlaylist, as_utc, utcnow
from echo.security import TokenCipher
from echo.spotify.client import SpotifyClient, SpotifyError
from echo.spotify.oauth import PLAYLIST_SCOPES, has_scopes

log = logging.getLogger(__name__)

PLAYLIST_NAME = "Echo finds"
PLAYLIST_DESCRIPTION = "Songs found through people with taste like yours, on Echo."
RETRY_MISSING_AFTER = timedelta(days=7)
"""How long to remember "no playable song found" before searching again."""


@dataclass(frozen=True)
class ArtistTrackInfo:
    artist_id: int
    artist_name: str
    track_id: str | None
    track_name: str | None
    spotify_url: str
    """Where "Open in Spotify" goes: the song if there is one, else the artist, else a search."""

    @property
    def uri(self) -> str | None:
        return f"spotify:track:{self.track_id}" if self.track_id else None


def spotify_url(artist_name: str, artist_spotify_id: str | None, track_id: str | None) -> str:
    if track_id:
        return f"https://open.spotify.com/track/{track_id}"
    if artist_spotify_id:
        return f"https://open.spotify.com/artist/{artist_spotify_id}"
    return f"https://open.spotify.com/search/{quote(artist_name)}"


def can_save_tracks(user: User) -> bool:
    """Real Spotify accounts whose login granted the playlist scopes."""
    return not user.is_demo and user.token is not None and has_scopes(user.token.scope, PLAYLIST_SCOPES)


# ------------------------------------------------------------- track lookup


async def track_for_artist(session: Session, spotify: SpotifyClient, artist: Artist) -> ArtistTrackInfo:
    """A song to play for `artist`, searching Spotify only when the cache has no answer.

    Uses the app's own token (client credentials), so it works for demo visitors too.
    Demo artists arrive without Spotify IDs; the first lookup fills theirs in.
    """
    cached = session.get(ArtistTrack, artist.id)
    fresh_enough = cached is not None and (
        cached.track_id is not None or as_utc(cached.fetched_at) > utcnow() - RETRY_MISSING_AFTER
    )
    if cached is not None and fresh_enough:
        return _info(artist, artist.spotify_id, cached.track_id, cached.track_name)

    token = await spotify.app_token()
    artist_spotify_id = artist.spotify_id
    if artist_spotify_id is None:
        artist_spotify_id = await spotify.find_artist_id(token, artist.name)
        if artist_spotify_id is not None:
            taken = session.scalar(select(Artist).where(Artist.spotify_id == artist_spotify_id))
            if taken is None:  # spotify_id is unique; never steal it from another row
                artist.spotify_id = artist_spotify_id

    track = await spotify.find_track(token, artist.name, artist_spotify_id) if artist_spotify_id else None
    if cached is None:
        cached = ArtistTrack(artist_id=artist.id)
        session.add(cached)
    cached.track_id = track.track_id if track else None
    cached.track_name = track.name if track else None
    cached.fetched_at = utcnow()
    session.flush()
    return _info(artist, artist_spotify_id, cached.track_id, cached.track_name)


def fallback_info(artist: Artist) -> ArtistTrackInfo:
    """What to return when Spotify can't be reached: no song, but a link that still works."""
    return _info(artist, artist.spotify_id, None, None)


def _info(
    artist: Artist, artist_spotify_id: str | None, track_id: str | None, track_name: str | None
) -> ArtistTrackInfo:
    return ArtistTrackInfo(
        artist_id=artist.id,
        artist_name=artist.name,
        track_id=track_id,
        track_name=track_name,
        spotify_url=spotify_url(artist.name, artist_spotify_id, track_id),
    )


# ------------------------------------------------------------ saving songs


async def save_track(
    session: Session,
    spotify: SpotifyClient,
    cipher: TokenCipher,
    user: User,
    track_id: str,
) -> str:
    """Add a song to the user's "Echo finds" playlist, creating it the first time. Returns its URL.

    If the user deleted the playlist in Spotify, adding returns 404; a new one is made then.
    """
    if user.token is None:
        raise ValueError("user has no Spotify token")
    tokens = load_tokens(user.token, cipher)
    fresh = await ensure_fresh(spotify, tokens)
    if fresh is not tokens:
        store_tokens(session, user, fresh, cipher)

    uri = f"spotify:track:{track_id}"
    playlist = session.get(UserPlaylist, user.id)
    if playlist is None:
        created = await spotify.create_playlist(fresh.access_token, PLAYLIST_NAME, PLAYLIST_DESCRIPTION)
        playlist = UserPlaylist(user_id=user.id, playlist_id=created.playlist_id, url=created.url)
        session.add(playlist)
    try:
        await spotify.add_to_playlist(fresh.access_token, playlist.playlist_id, [uri])
    except SpotifyError as exc:
        if exc.status != 404:
            raise
        log.info("playlist %s is gone for user %s; creating a new one", playlist.playlist_id, user.id)
        created = await spotify.create_playlist(fresh.access_token, PLAYLIST_NAME, PLAYLIST_DESCRIPTION)
        playlist.playlist_id, playlist.url = created.playlist_id, created.url
        await spotify.add_to_playlist(fresh.access_token, playlist.playlist_id, [uri])
    session.flush()
    return playlist.url
