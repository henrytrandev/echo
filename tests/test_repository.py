from datetime import timedelta

import pytest

from echo.models import Artist, TasteSignalRow, User
from echo.repository import (
    ArtistResolver,
    SourceArtist,
    connection_status,
    connection_statuses,
    load_pool,
    replace_signals,
    request_connection,
    upsert_user,
    users_needing_refresh,
)
from echo.seeding import ensure_demo_listeners


def make_user(session, name="Alice", external_id=None):
    return upsert_user(session, source="spotify", external_id=external_id or name.lower(), display_name=name)


def test_upsert_user_is_idempotent_and_updates_name(session):
    first = make_user(session, "Alice", "acct-1")
    second = upsert_user(session, source="spotify", external_id="acct-1", display_name="Alice B")
    assert first.id == second.id
    assert second.display_name == "Alice B"


# ------------------------------------------------------------------ resolver


def test_resolver_prefers_spotify_id(session):
    resolver = ArtistResolver(session)
    a = resolver.resolve("Radiohead", spotify_id="sp1")
    b = ArtistResolver(session).resolve("Radiohead (official)", spotify_id="sp1")
    assert a.id == b.id


def test_resolver_links_by_name_and_backfills_id(session):
    demo = ArtistResolver(session).resolve("Beyoncé")  # arrives without an ID
    real = ArtistResolver(session).resolve("Beyonce", spotify_id="sp-bey")
    assert demo.id == real.id
    assert session.get(Artist, demo.id).spotify_id == "sp-bey"


def test_resolver_keeps_homonyms_with_different_ids_apart(session):
    first = ArtistResolver(session).resolve("Nirvana", spotify_id="sp-us")
    second = ArtistResolver(session).resolve("Nirvana", spotify_id="sp-uk")
    assert first.id != second.id


# ------------------------------------------------------------- taste signals


def snapshot(*names, time_range="long_term"):
    return [SourceArtist(name=n, time_range=time_range, rank=r, spotify_id=f"sp-{n}") for r, n in enumerate(names, 1)]


def test_replace_signals_is_idempotent(session):
    user = make_user(session)
    replace_signals(session, user, snapshot("A", "B", "C"))
    replace_signals(session, user, snapshot("A", "B", "C"))
    session.commit()
    assert session.query(TasteSignalRow).count() == 3


def test_replace_signals_only_replaces_fetched_time_ranges(session):
    user = make_user(session)
    replace_signals(session, user, snapshot("A", "B") + snapshot("C", time_range="short_term"))
    replace_signals(session, user, snapshot("D", time_range="short_term"))
    session.commit()
    pool = load_pool(session)[user.id]
    assert {(s.time_range, s.rank) for s in pool} == {("long_term", 1), ("long_term", 2), ("short_term", 1)}
    assert user.last_ingested_at is not None


def test_replace_signals_keeps_best_rank_when_names_merge(session):
    user = make_user(session)
    entries = [
        SourceArtist("Beyoncé", "long_term", 1),
        SourceArtist("Beyonce", "long_term", 4),
    ]
    assert replace_signals(session, user, entries) == 1
    session.commit()
    assert [s.rank for s in load_pool(session)[user.id]] == [1]


def test_load_pool_includes_users_without_signals(session):
    user = make_user(session)
    session.commit()
    assert load_pool(session) == {user.id: []}


def test_users_needing_refresh(session):
    fresh = make_user(session, "Fresh")
    stale = make_user(session, "Stale")
    replace_signals(session, fresh, snapshot("A"))
    session.commit()
    due = {u.id for u in users_needing_refresh(session, older_than=timedelta(hours=1))}
    assert due == {stale.id}


# --------------------------------------------------------------- connections


def test_connection_becomes_mutual(session):
    alice, bob = make_user(session, "Alice"), make_user(session, "Bob")
    assert request_connection(session, alice.id, bob.id) == "requested"
    assert connection_status(session, bob.id, alice.id) == "incoming"
    assert request_connection(session, bob.id, alice.id) == "connected"
    assert connection_statuses(session, alice.id) == {bob.id: "connected"}


def test_connection_request_is_idempotent(session):
    alice, bob = make_user(session, "Alice"), make_user(session, "Bob")
    request_connection(session, alice.id, bob.id)
    assert request_connection(session, alice.id, bob.id) == "requested"


def test_cannot_connect_with_yourself_or_nobody(session):
    alice = make_user(session)
    with pytest.raises(ValueError):
        request_connection(session, alice.id, alice.id)
    with pytest.raises(LookupError):
        request_connection(session, alice.id, "does-not-exist")


# ------------------------------------------------------------------- seeding


def test_ensure_demo_listeners_only_seeds_once(session):
    assert ensure_demo_listeners(session, 20) == 20
    assert ensure_demo_listeners(session, 20) == 0
    session.commit()
    assert session.query(User).filter(User.is_demo.is_(True)).count() == 20


# ----------------------------------------------------------------- feedback


def test_feedback_can_be_set_changed_and_removed(session):
    from echo.repository import feedback_for, set_feedback

    user = make_user(session)
    artist = ArtistResolver(session).resolve("Sade", spotify_id="sp-sade")
    set_feedback(session, user.id, artist.id, "like")
    set_feedback(session, user.id, artist.id, "dislike")
    assert feedback_for(session, user.id) == {artist.id: "dislike"}
    set_feedback(session, user.id, artist.id, None)
    assert feedback_for(session, user.id) == {}
    with pytest.raises(ValueError):
        set_feedback(session, user.id, artist.id, "meh")
