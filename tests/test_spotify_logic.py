"""Spotify logic that needs no network: retry policy, OAuth helpers, response parsing."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from echo.spotify.oauth import PLAYLIST_SCOPES, TokenSet, build_authorize_url, has_scopes
from echo.spotify.parsing import (
    error_reason,
    parse_artist_search,
    parse_created_playlist,
    parse_profile,
    parse_top_artists_page,
    parse_track_search,
)
from echo.spotify.retry import NETWORK_ERROR, RetryPolicy

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


# -------------------------------------------------------------------- retry


def policy(**kwargs) -> RetryPolicy:
    return RetryPolicy(random_fn=lambda: 1.0, **kwargs)  # no jitter, so delays are predictable


def test_429_honours_retry_after():
    assert policy().delay_for(1, 429, "3") == 3.0


def test_429_with_huge_retry_after_gives_up_instead_of_hanging():
    assert policy(max_delay=30).delay_for(1, 429, "3600") is None


def test_server_errors_back_off_exponentially_up_to_a_cap():
    p = policy(base_delay=1.0, max_delay=5.0)
    assert [p.delay_for(n, 503) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]


def test_network_errors_are_retried():
    assert policy().delay_for(1, NETWORK_ERROR) is not None


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    assert policy().delay_for(1, status) is None


def test_stops_after_max_attempts():
    assert policy(max_attempts=3).delay_for(3, 503) is None


def test_jitter_stays_within_bounds():
    p = RetryPolicy(base_delay=1.0, random_fn=lambda: 0.25)
    assert p.delay_for(3, 500) == 1.0  # 25% of the 4s ceiling


# -------------------------------------------------------------------- oauth


def test_authorize_url_contains_required_params():
    url = build_authorize_url("cid", "http://127.0.0.1:8000/callback", "st4te", scopes=["a", "b"])
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["cid"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8000/callback"]
    assert query["state"] == ["st4te"]
    assert query["scope"] == ["a b"]


def test_token_response_parsing_and_expiry():
    tokens = TokenSet.from_response({"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, now=NOW)
    assert tokens.expires_at == NOW + timedelta(hours=1)
    assert not tokens.needs_refresh(NOW)
    assert tokens.needs_refresh(NOW + timedelta(minutes=59, seconds=30))  # refreshes a bit early


def test_refresh_response_without_refresh_token_keeps_the_old_one():
    tokens = TokenSet.from_response({"access_token": "new"}, now=NOW, previous_refresh_token="old-rt")
    assert tokens.refresh_token == "old-rt"


def test_token_response_without_any_refresh_token_is_rejected():
    with pytest.raises(ValueError):
        TokenSet.from_response({"access_token": "at"}, now=NOW)


# ------------------------------------------------------------------ parsing


def test_profile_prefers_stable_account_id():
    profile = parse_profile({"id": "username", "account_id": "acct-123", "display_name": "Sam"})
    assert profile.account_id == "acct-123"
    assert profile.display_name == "Sam"


def test_profile_falls_back_to_id_and_handles_missing_name():
    profile = parse_profile({"id": "username", "display_name": None})
    assert profile.account_id == "username"
    assert profile.display_name == "username"


def test_top_artists_page_ranks_continue_across_pages_and_skip_junk():
    payload = {
        "items": [{"id": "a1", "name": "First"}, {"id": "bad"}, {"id": "a2", "name": "Second"}],
        "next": "https://api.spotify.com/v1/me/top/artists?offset=50",
    }
    artists, next_url = parse_top_artists_page(payload, "long_term", rank_offset=50)
    # Ranks keep Spotify's positions, so the skipped entry leaves a gap rather than shifting others up.
    assert [(a.name, a.rank, a.spotify_id) for a in artists] == [("First", 51, "a1"), ("Second", 53, "a2")]
    assert next_url.endswith("offset=50")


def test_last_page_has_no_next_url():
    artists, next_url = parse_top_artists_page({"items": [], "next": None}, "short_term", rank_offset=0)
    assert artists == [] and next_url is None


def test_error_reason_handles_spotify_error_shapes():
    assert error_reason({"error": {"status": 429, "message": "Too many requests", "reason": "QUOTA_EXCEEDED"}}) == (
        "QUOTA_EXCEEDED"
    )
    assert error_reason({"error": "invalid_grant", "error_description": "Refresh token revoked"}) == (
        "Refresh token revoked"
    )
    assert error_reason("not json") == ""


def test_has_scopes_requires_every_scope():
    assert has_scopes("user-top-read playlist-modify-private playlist-modify-public", PLAYLIST_SCOPES)
    assert not has_scopes("user-top-read playlist-modify-private", PLAYLIST_SCOPES)
    assert not has_scopes("", PLAYLIST_SCOPES)


def test_artist_search_only_accepts_an_exact_name_match():
    payload = {"artists": {"items": [{"id": "x1", "name": "Bjork Tribute Band"}, {"id": "x2", "name": "Björk"}]}}
    assert parse_artist_search(payload, "Bjork") == "x2"
    assert parse_artist_search(payload, "Portishead") is None
    assert parse_artist_search({}, "Bjork") is None


def test_track_search_skips_songs_not_credited_to_the_artist():
    payload = {
        "tracks": {
            "items": [
                {"id": "cover", "name": "Glory Box", "uri": "spotify:track:cover", "artists": [{"id": "other"}]},
                {"id": "real", "name": "Glory Box", "uri": "spotify:track:real", "artists": [{"id": "portishead"}]},
            ]
        }
    }
    track = parse_track_search(payload, "portishead")
    assert (track.track_id, track.name, track.uri) == ("real", "Glory Box", "spotify:track:real")
    assert parse_track_search(payload, "nobody") is None


def test_created_playlist_parsing_has_a_url_fallback():
    assert parse_created_playlist({"id": "p1", "external_urls": {"spotify": "https://x/p1"}}).url == "https://x/p1"
    assert parse_created_playlist({"id": "p2"}).url == "https://open.spotify.com/playlist/p2"
    with pytest.raises(ValueError):
        parse_created_playlist({})
