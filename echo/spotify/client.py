"""Async Spotify Web API client with retries, pagination and token exchange."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from echo.sources import SourceArtist
from echo.spotify.oauth import TOKEN_URL, TokenSet
from echo.spotify.parsing import (
    CreatedPlaylist,
    SpotifyProfile,
    TrackChoice,
    error_reason,
    parse_artist_search,
    parse_created_playlist,
    parse_profile,
    parse_top_artists_page,
    parse_track_search,
)
from echo.spotify.retry import NETWORK_ERROR, RetryPolicy

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"

DEFAULT_MARKET = "US"
"""Search needs a country when no user token is involved, or results come back empty."""


class SpotifyError(Exception):
    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(f"Spotify returned {status}" + (f": {reason}" if reason else ""))
        self.status = status
        self.reason = reason

    @property
    def user_not_allowlisted(self) -> bool:
        """Development-mode apps reject accounts that aren't added in the developer dashboard."""
        return self.status == 403 and "registered" in self.reason.lower()


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx.AsyncClient,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._app_token: str | None = None
        self._app_token_expires = datetime.min.replace(tzinfo=UTC)
        self._app_token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ OAuth

    async def exchange_code(self, code: str) -> TokenSet:
        payload = await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        )
        return TokenSet.from_response(payload, now=self._clock())

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        payload = await self._token_request({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
        return TokenSet.from_response(payload, now=self._clock(), previous_refresh_token=tokens.refresh_token)

    async def app_token(self) -> str:
        """A token for the app itself (client credentials flow), for lookups that need no user.

        Cached until a minute before it expires. The lock means many requests arriving at
        once share one token request instead of each asking Spotify for its own.
        """
        async with self._app_token_lock:
            if self._app_token is None or self._clock() >= self._app_token_expires:
                payload = await self._token_request({"grant_type": "client_credentials"})
                self._app_token = str(payload["access_token"])
                lifetime = int(payload.get("expires_in", 3600))
                self._app_token_expires = self._clock() + timedelta(seconds=max(lifetime - 60, 0))
            return self._app_token

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        response = await self._send("POST", TOKEN_URL, data=form, headers={"Authorization": f"Basic {basic}"})
        return response.json()

    # ------------------------------------------------------------------- data

    async def get_profile(self, access_token: str) -> SpotifyProfile:
        response = await self._send("GET", f"{API_BASE}/me", headers=_bearer(access_token))
        return parse_profile(response.json())

    async def get_top_artists(self, access_token: str, time_range: str, max_items: int = 50) -> list[SourceArtist]:
        """Up to `max_items` of the user's top artists for one time range, following `next` links."""
        url: str | None = f"{API_BASE}/me/top/artists"
        params: dict[str, Any] | None = {"time_range": time_range, "limit": min(max_items, 50)}
        artists: list[SourceArtist] = []
        while url and len(artists) < max_items:
            response = await self._send("GET", url, params=params, headers=_bearer(access_token))
            page, url = parse_top_artists_page(response.json(), time_range, rank_offset=len(artists))
            if not page:
                break
            artists.extend(page)
            params = None  # `next` already carries the query string
        return artists[:max_items]

    async def find_artist_id(self, access_token: str, name: str, market: str = DEFAULT_MARKET) -> str | None:
        """Spotify ID for an artist known only by name (e.g. demo data), or None if no exact match."""
        params = {"q": name, "type": "artist", "limit": 10, "market": market}
        response = await self._send("GET", f"{API_BASE}/search", params=params, headers=_bearer(access_token))
        return parse_artist_search(response.json(), name)

    async def find_track(
        self, access_token: str, artist_name: str, artist_spotify_id: str, market: str = DEFAULT_MARKET
    ) -> TrackChoice | None:
        """A song by this artist to play. Spotify removed the "artist top tracks" endpoint for
        development-mode apps in February 2026, so this searches instead."""
        params = {"q": f'artist:"{artist_name}"', "type": "track", "limit": 10, "market": market}
        response = await self._send("GET", f"{API_BASE}/search", params=params, headers=_bearer(access_token))
        return parse_track_search(response.json(), artist_spotify_id)

    async def create_playlist(self, access_token: str, name: str, description: str) -> CreatedPlaylist:
        body = {"name": name, "description": description, "public": False}
        response = await self._send("POST", f"{API_BASE}/me/playlists", json=body, headers=_bearer(access_token))
        return parse_created_playlist(response.json())

    async def add_to_playlist(self, access_token: str, playlist_id: str, uris: list[str]) -> None:
        await self._send(
            "POST",
            f"{API_BASE}/playlists/{playlist_id}/items",
            json={"uris": uris},
            headers=_bearer(access_token),
        )

    # -------------------------------------------------------------- transport

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                delay = self._retry.delay_for(attempt, NETWORK_ERROR)
                if delay is None:
                    raise SpotifyError(NETWORK_ERROR, str(exc)) from exc
                log.warning("network error calling %s (%s); retrying in %.1fs", url, exc, delay)
                await self._sleep(delay)
                continue

            if response.is_success:
                return response

            delay = self._retry.delay_for(attempt, response.status_code, response.headers.get("Retry-After"))
            if delay is None:
                raise SpotifyError(response.status_code, error_reason(_json_or_none(response)))
            log.warning("Spotify %s on %s; retrying in %.1fs", response.status_code, url, delay)
            await self._sleep(delay)


def _bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"error": {"message": response.text[:200]}}
