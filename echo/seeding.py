"""Writing demo listeners to the database."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from echo.demo import generate_listeners
from echo.models import User
from echo.repository import ArtistResolver, replace_signals, upsert_user


def seed_demo_listeners(session: Session, count: int, seed: int = 7) -> int:
    """Create or update `count` demo listeners. Safe to re-run: same names, same data."""
    listeners = generate_listeners(count, seed=seed)
    resolver = ArtistResolver(session)  # shared, so each artist is looked up once, not once per listener
    for listener in listeners:
        user = upsert_user(
            session,
            source="demo",
            external_id=listener.name,
            display_name=listener.name,
            is_demo=True,
        )
        replace_signals(session, user, listener.artists, resolver=resolver)
    return len(listeners)


def ensure_demo_listeners(session: Session, count: int, seed: int = 7) -> int:
    """Seed only if fewer than `count` demo listeners exist. Returns how many were written."""
    existing = session.scalar(select(func.count()).select_from(User).where(User.is_demo.is_(True))) or 0
    if existing >= count:
        return 0
    return seed_demo_listeners(session, count, seed)
