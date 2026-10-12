"""Database schema.

    users ──< taste_signals >── artists ── artist_tracks (1:1, cached song to play)
      │
      ├── oauth_tokens            (1:1, encrypted)
      ├── user_playlists          (1:1, the "Echo finds" playlist on Spotify)
      ├──< song_feedback >── artists  (like / dislike on suggested artists)
      └──< connection_requests    (from_user -> to_user; mutual = connected)

taste_signals is a snapshot table: each ingestion replaces a user's rows for
the time ranges it fetched, so re-running ingestion is always safe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes even for timezone=True columns; treat them as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_user_source_external"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String(16))
    """Where the profile comes from: "spotify" or "demo" for now."""
    external_id: Mapped[str] = mapped_column(String(128))
    """For Spotify this is `account_id`, which Spotify guarantees never changes."""
    display_name: Mapped[str] = mapped_column(String(200))
    discoverable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    token: Mapped[OAuthToken | None] = relationship(back_populates="user", cascade="all, delete-orphan")


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(String(500), default="")

    user: Mapped[User] = relationship(back_populates="token")


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300))
    name_key: Mapped[str] = mapped_column(String(300), index=True)
    """normalize_artist_name(name): the fallback key for matching across sources."""
    spotify_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    mbid: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    """MusicBrainz ID, for ListenBrainz/Last.fm data later."""


class TasteSignalRow(Base):
    __tablename__ = "taste_signals"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    time_range: Mapped[str] = mapped_column(String(16), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectionRequest(Base):
    __tablename__ = "connection_requests"

    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    to_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtistTrack(Base):
    """Cache of the song chosen to represent an artist, so Spotify is searched once per artist.

    A row with no `track_id` means "searched, nothing playable found"; it's retried after a week.
    """

    __tablename__ = "artist_tracks"

    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    track_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    track_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserPlaylist(Base):
    """The "Echo finds" playlist Echo created in a user's Spotify account."""

    __tablename__ = "user_playlists"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    playlist_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SongFeedback(Base):
    """A listener's verdict on an artist Echo suggested: "like" or "dislike".

    One row per (user, artist): rating again replaces the verdict, and undoing deletes the row.
    Kept as data for measuring how good suggestions are, and used to hide disliked artists.
    """

    __tablename__ = "song_feedback"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(8))
    track_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The song they were hearing when they rated, if any."""
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
