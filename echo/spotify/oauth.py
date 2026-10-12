"""OAuth 2.0 authorization-code flow helpers (no network code here)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

PLAYLIST_SCOPES: tuple[str, ...] = ("playlist-modify-private", "playlist-modify-public")
"""Needed to create the user's "Echo finds" playlist and add songs to it."""

SCOPES: tuple[str, ...] = ("user-top-read", *PLAYLIST_SCOPES)
"""Ask only for what the app uses: reading top artists, and managing the one playlist Echo makes."""


def has_scopes(granted: str, needed: Sequence[str]) -> bool:
    """Whether a token's space-separated scope string covers every scope in `needed`.

    People who logged in before a feature existed hold tokens without its scopes;
    they have to log in again (and approve the new access) before it works.
    """
    return set(needed) <= set(granted.split())

EXPIRY_SKEW = timedelta(seconds=60)
"""Refresh a little early so a token never expires mid-request."""


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Sequence[str] = SCOPES,
) -> str:
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(scopes),
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str = ""

    def needs_refresh(self, now: datetime) -> bool:
        return now >= self.expires_at - EXPIRY_SKEW

    @classmethod
    def from_response(
        cls,
        payload: Mapping[str, object],
        now: datetime,
        previous_refresh_token: str | None = None,
    ) -> TokenSet:
        """Parse a token response. Refresh responses may omit refresh_token; keep the old one then."""
        refresh_token = payload.get("refresh_token") or previous_refresh_token
        if not refresh_token:
            raise ValueError("token response has no refresh_token")
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=str(refresh_token),
            expires_at=now + timedelta(seconds=int(payload.get("expires_in", 3600))),
            scope=str(payload.get("scope", "")),
        )
