"""Getting listening data into the database.

Two entry points:

* `import_after_login`: runs once in the OAuth callback for a single user.
* `refresh_stale_users`: the scheduled job. It fetches from Spotify concurrently
  (network is the slow part) but writes to the database one user at a time, each
  in its own transaction, so one failing user never blocks or corrupts the rest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from echo.db import session_scope
from echo.models import OAuthToken, User, as_utc
from echo.repository import replace_signals, upsert_user, users_needing_refresh
from echo.security import TokenCipher
from echo.sources import SourceArtist
from echo.spotify.client import SpotifyClient
from echo.spotify.oauth import TokenSet

log = logging.getLogger(__name__)

TIME_RANGES = ("short_term", "medium_term", "long_term")


# ------------------------------------------------------------------ tokens


def store_tokens(session: Session, user: User, tokens: TokenSet, cipher: TokenCipher) -> None:
    row = session.get(OAuthToken, user.id)
    if row is None:
        row = OAuthToken(user_id=user.id)
        session.add(row)
    row.access_token_enc = cipher.encrypt(tokens.access_token)
    row.refresh_token_enc = cipher.encrypt(tokens.refresh_token)
    row.expires_at = tokens.expires_at
    row.scope = tokens.scope


def load_tokens(row: OAuthToken, cipher: TokenCipher) -> TokenSet:
    return TokenSet(
        access_token=cipher.decrypt(row.access_token_enc),
        refresh_token=cipher.decrypt(row.refresh_token_enc),
        expires_at=as_utc(row.expires_at),
        scope=row.scope,
    )


async def ensure_fresh(client: SpotifyClient, tokens: TokenSet, now: datetime | None = None) -> TokenSet:
    if tokens.needs_refresh(now or datetime.now(UTC)):
        return await client.refresh(tokens)
    return tokens


# -------------------------------------------------------------------- fetch


async def fetch_top_artists(client: SpotifyClient, access_token: str, per_range: int = 50) -> list[SourceArtist]:
    """All three time ranges, fetched concurrently."""
    batches = await asyncio.gather(
        *(client.get_top_artists(access_token, time_range, max_items=per_range) for time_range in TIME_RANGES)
    )
    return [artist for batch in batches for artist in batch]


# ------------------------------------------------------------- login import


async def import_after_login(session: Session, client: SpotifyClient, cipher: TokenCipher, code: str) -> User:
    """Exchange the OAuth code, create or update the user, and import their top artists."""
    tokens = await client.exchange_code(code)
    profile = await client.get_profile(tokens.access_token)
    entries = await fetch_top_artists(client, tokens.access_token)

    user = upsert_user(
        session,
        source="spotify",
        external_id=profile.account_id,
        display_name=profile.display_name,
    )
    store_tokens(session, user, tokens, cipher)
    replace_signals(session, user, entries)
    return user


# ------------------------------------------------------------ scheduled job


@dataclass
class RefreshReport:
    refreshed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


async def refresh_stale_users(
    factory: sessionmaker[Session],
    client: SpotifyClient,
    cipher: TokenCipher,
    older_than: timedelta,
    concurrency: int = 4,
) -> RefreshReport:
    with session_scope(factory) as session:
        due = [
            (user.id, load_tokens(user.token, cipher))
            for user in users_needing_refresh(session, older_than=older_than)
            if user.token is not None
        ]

    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(user_id: str, tokens: TokenSet) -> _Fetched:
        async with semaphore:
            try:
                fresh = await ensure_fresh(client, tokens)
            except Exception as exc:
                return _Fetched(user_id, new_tokens=None, entries=None, error=exc)
            # Spotify may rotate the refresh token, so a refreshed token must be saved
            # even if the data fetch below fails, or the user would have to log in again.
            new_tokens = fresh if fresh is not tokens else None
            try:
                entries = await fetch_top_artists(client, fresh.access_token)
            except Exception as exc:
                return _Fetched(user_id, new_tokens=new_tokens, entries=None, error=exc)
            return _Fetched(user_id, new_tokens=new_tokens, entries=entries, error=None)

    results = await asyncio.gather(*(fetch(uid, tok) for uid, tok in due))

    report = RefreshReport()
    for result in results:
        try:
            with session_scope(factory) as session:
                user = session.get(User, result.user_id)
                if user is None:  # deleted while we were fetching
                    continue
                if result.new_tokens is not None:
                    store_tokens(session, user, result.new_tokens, cipher)
                if result.entries is not None:
                    replace_signals(session, user, result.entries)
        except Exception as exc:  # keep going; one bad row shouldn't stop the batch
            log.exception("saving refresh failed for %s", result.user_id)
            report.failed[result.user_id] = str(exc)
            continue
        if result.error is not None:
            log.error("refresh failed for %s: %s", result.user_id, result.error)
            report.failed[result.user_id] = str(result.error)
        else:
            report.refreshed.append(result.user_id)
    return report


@dataclass(frozen=True)
class _Fetched:
    user_id: str
    new_tokens: TokenSet | None
    entries: list[SourceArtist] | None
    error: Exception | None
