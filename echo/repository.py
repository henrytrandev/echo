"""All database reads and writes. Routes and jobs call these instead of writing queries inline."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from echo.matching import TasteSignal
from echo.models import Artist, ConnectionRequest, SongFeedback, TasteSignalRow, User, as_utc, utcnow
from echo.names import normalize_artist_name
from echo.sources import SourceArtist

__all__ = [
    "ArtistResolver",
    "SourceArtist",
    "artist_names",
    "VERDICTS",
    "connection_status",
    "connection_statuses",
    "feedback_for",
    "is_stale",
    "load_pool",
    "replace_signals",
    "request_connection",
    "set_feedback",
    "top_artist_names",
    "upsert_user",
    "users_needing_refresh",
]

# --------------------------------------------------------------------- users


def upsert_user(
    session: Session,
    *,
    source: str,
    external_id: str,
    display_name: str,
    is_demo: bool = False,
) -> User:
    user = session.scalar(select(User).where(User.source == source, User.external_id == external_id))
    if user is None:
        user = User(source=source, external_id=external_id, display_name=display_name, is_demo=is_demo)
        session.add(user)
        session.flush()  # assigns the id
    else:
        user.display_name = display_name
    return user


def users_needing_refresh(session: Session, *, older_than: timedelta, source: str = "spotify") -> list[User]:
    cutoff = utcnow() - older_than
    stmt = select(User).where(
        User.source == source,
        or_(User.last_ingested_at.is_(None), User.last_ingested_at < cutoff),
    )
    return list(session.scalars(stmt))


# ------------------------------------------------------- artist resolution


class ArtistResolver:
    """Matches incoming artists to existing rows: Spotify ID, then MusicBrainz ID, then name.

    Name matches are only accepted when they don't contradict an ID we already have
    (two different Spotify IDs with the same name are two different artists). When a
    name match succeeds, missing IDs are backfilled so the next lookup is exact.
    Results are cached for the lifetime of the resolver to avoid repeated queries.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cache: dict[tuple[str | None, str | None, str], Artist] = {}

    def resolve(self, name: str, spotify_id: str | None = None, mbid: str | None = None) -> Artist:
        name_key = normalize_artist_name(name)
        cache_key = (spotify_id, mbid, name_key)
        if (cached := self._cache.get(cache_key)) is not None:
            return cached

        artist = self._find(name_key, spotify_id, mbid)
        if artist is None:
            artist = Artist(name=name, name_key=name_key, spotify_id=spotify_id, mbid=mbid)
            self._session.add(artist)
            self._session.flush()
        else:
            if spotify_id and artist.spotify_id is None:
                artist.spotify_id = spotify_id
            if mbid and artist.mbid is None:
                artist.mbid = mbid
        self._cache[cache_key] = artist
        return artist

    def _find(self, name_key: str, spotify_id: str | None, mbid: str | None) -> Artist | None:
        if spotify_id:
            found = self._session.scalar(select(Artist).where(Artist.spotify_id == spotify_id))
            if found:
                return found
        if mbid:
            found = self._session.scalar(select(Artist).where(Artist.mbid == mbid))
            if found:
                return found
        for candidate in self._session.scalars(select(Artist).where(Artist.name_key == name_key)):
            conflicting_spotify = spotify_id and candidate.spotify_id and candidate.spotify_id != spotify_id
            conflicting_mbid = mbid and candidate.mbid and candidate.mbid != mbid
            if not conflicting_spotify and not conflicting_mbid:
                return candidate
        return None


# ------------------------------------------------------------ taste signals


def replace_signals(
    session: Session,
    user: User,
    entries: Sequence[SourceArtist],
    resolver: ArtistResolver | None = None,
) -> int:
    """Store a fresh snapshot of a user's top artists. Idempotent: running it twice is safe.

    Only the time ranges present in `entries` are replaced, so a partial fetch
    (say, just short_term) doesn't wipe the other lists. Pass a shared `resolver`
    when importing many users at once, so each artist is looked up only once.
    """
    resolver = resolver or ArtistResolver(session)
    incoming: dict[tuple[int, str], int] = {}  # (artist_id, time_range) -> rank
    for entry in entries:
        artist = resolver.resolve(entry.name, spotify_id=entry.spotify_id, mbid=entry.mbid)
        key = (artist.id, entry.time_range)
        # The same artist can arrive twice in one list after resolution merges two names.
        incoming[key] = min(entry.rank, incoming.get(key, entry.rank))

    now = utcnow()
    time_ranges = sorted({entry.time_range for entry in entries})
    existing = {
        (row.artist_id, row.time_range): row
        for row in session.scalars(
            select(TasteSignalRow).where(
                TasteSignalRow.user_id == user.id,
                TasteSignalRow.time_range.in_(time_ranges),
            )
        )
    }
    # Diff against what's stored: update matches, add new rows, delete rows that fell off the list.
    for (artist_id, time_range), rank in incoming.items():
        row = existing.pop((artist_id, time_range), None)
        if row is None:
            session.add(
                TasteSignalRow(
                    user_id=user.id, artist_id=artist_id, time_range=time_range, rank=rank, observed_at=now
                )
            )
        else:
            row.rank = rank
            row.observed_at = now
    for stale in existing.values():
        session.delete(stale)

    user.last_ingested_at = now
    session.flush()
    return len(incoming)


def load_pool(session: Session) -> dict[str, list[TasteSignal]]:
    """Every user's signals, keyed by user id, in one query. Users with no signals are included."""
    pool: dict[str, list[TasteSignal]] = {user_id: [] for user_id in session.scalars(select(User.id))}
    rows = session.execute(
        select(TasteSignalRow.user_id, TasteSignalRow.artist_id, TasteSignalRow.time_range, TasteSignalRow.rank)
    )
    for user_id, artist_id, time_range, rank in rows:
        pool.setdefault(user_id, []).append(TasteSignal(artist_id, time_range, rank))
    return pool


def signals_for(session: Session, user_id: str) -> list[TasteSignal]:
    """One user's signals, for drawing their taste fingerprint."""
    rows = session.execute(
        select(TasteSignalRow.artist_id, TasteSignalRow.time_range, TasteSignalRow.rank).where(
            TasteSignalRow.user_id == user_id
        )
    )
    return [TasteSignal(artist_id, time_range, rank) for artist_id, time_range, rank in rows]


def artist_names(session: Session, artist_ids: Iterable[int]) -> dict[int, str]:
    ids = set(artist_ids)
    if not ids:
        return {}
    return dict(session.execute(select(Artist.id, Artist.name).where(Artist.id.in_(ids))).all())


def top_artist_names(session: Session, user_id: str, limit: int = 10) -> list[str]:
    stmt = (
        select(Artist.name)
        .join(TasteSignalRow, TasteSignalRow.artist_id == Artist.id)
        .where(TasteSignalRow.user_id == user_id, TasteSignalRow.time_range == "long_term")
        .order_by(TasteSignalRow.rank)
        .limit(limit)
    )
    return list(session.scalars(stmt))


# --------------------------------------------------------------- connections


def request_connection(session: Session, from_user_id: str, to_user_id: str) -> str:
    """Record that `from_user` wants to connect. Returns the resulting status."""
    if from_user_id == to_user_id:
        raise ValueError("cannot connect with yourself")
    if session.get(User, to_user_id) is None:
        raise LookupError("no such user")
    if session.get(ConnectionRequest, (from_user_id, to_user_id)) is None:
        session.add(ConnectionRequest(from_user_id=from_user_id, to_user_id=to_user_id))
        session.flush()
    return connection_status(session, from_user_id, to_user_id)


def connection_status(session: Session, user_id: str, other_id: str) -> str:
    """'connected', 'requested' (you asked), 'incoming' (they asked) or 'none'."""
    outgoing = session.get(ConnectionRequest, (user_id, other_id)) is not None
    incoming = session.get(ConnectionRequest, (other_id, user_id)) is not None
    if outgoing and incoming:
        return "connected"
    if outgoing:
        return "requested"
    if incoming:
        return "incoming"
    return "none"


def connection_statuses(session: Session, user_id: str) -> dict[str, str]:
    """Status with everyone this user has any request with, in two queries."""
    outgoing = set(
        session.scalars(select(ConnectionRequest.to_user_id).where(ConnectionRequest.from_user_id == user_id))
    )
    incoming = set(
        session.scalars(select(ConnectionRequest.from_user_id).where(ConnectionRequest.to_user_id == user_id))
    )
    statuses: dict[str, str] = {}
    for other in outgoing | incoming:
        if other in outgoing and other in incoming:
            statuses[other] = "connected"
        elif other in outgoing:
            statuses[other] = "requested"
        else:
            statuses[other] = "incoming"
    return statuses


def is_stale(user: User, older_than: timedelta, now: datetime | None = None) -> bool:
    if user.last_ingested_at is None:
        return True
    return as_utc(user.last_ingested_at) < (now or utcnow()) - older_than


# ----------------------------------------------------------------- feedback

VERDICTS = ("like", "dislike")


def set_feedback(
    session: Session,
    user_id: str,
    artist_id: int,
    verdict: str | None,
    track_id: str | None = None,
) -> None:
    """Record, change or (with verdict=None) remove a user's verdict on an artist."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS} or None")
    row = session.get(SongFeedback, (user_id, artist_id))
    if verdict is None:
        if row is not None:
            session.delete(row)
    else:
        if row is None:
            row = SongFeedback(user_id=user_id, artist_id=artist_id)
            session.add(row)
        row.verdict = verdict
        row.track_id = track_id
        row.updated_at = utcnow()
    session.flush()


def feedback_for(session: Session, user_id: str) -> dict[int, str]:
    """{artist_id: verdict} for everything this user has rated."""
    rows = session.execute(
        select(SongFeedback.artist_id, SongFeedback.verdict).where(SongFeedback.user_id == user_id)
    )
    return dict(rows.all())
