"""SpotifyClient against a fake Spotify (httpx.MockTransport): retries, pagination, token exchange."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from echo.spotify.client import SpotifyClient, SpotifyError
from echo.spotify.oauth import TokenSet
from echo.spotify.retry import RetryPolicy

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def make_client(handler, sleeps=None, max_attempts=4, clock=lambda: NOW):
    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SpotifyClient(
        "cid",
        "secret",
        "http://127.0.0.1:8000/callback",
        http,
        retry=RetryPolicy(max_attempts=max_attempts, random_fn=lambda: 1.0),
        sleep=fake_sleep,
        clock=clock,
    )


def run(coro):
    return asyncio.run(coro)


def artist_page(start, count, next_url=None):
    items = [{"id": f"id{i}", "name": f"Artist {i}"} for i in range(start, start + count)]
    return {"items": items, "next": next_url}


def test_retries_429_using_retry_after_then_succeeds():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"status": 429}})
        return httpx.Response(200, json=artist_page(1, 3))

    client = make_client(handler, sleeps)
    artists = run(client.get_top_artists("token", "short_term"))
    assert len(calls) == 2
    assert sleeps == [2.0]
    assert [a.rank for a in artists] == [1, 2, 3]
    assert calls[0].headers["Authorization"] == "Bearer token"


def test_follows_next_links_and_continues_ranks():
    def handler(request):
        if "offset" in str(request.url):
            return httpx.Response(200, json=artist_page(3, 2))
        assert request.url.params["time_range"] == "long_term"
        return httpx.Response(200, json=artist_page(1, 2, "https://api.spotify.com/v1/me/top/artists?offset=2&limit=2"))

    artists = run(make_client(handler).get_top_artists("t", "long_term"))
    assert [(a.name, a.rank) for a in artists] == [
        ("Artist 1", 1),
        ("Artist 2", 2),
        ("Artist 3", 3),
        ("Artist 4", 4),
    ]


def test_stops_at_max_items():
    def handler(request):
        return httpx.Response(200, json=artist_page(1, 10, "https://api.spotify.com/v1/me/top/artists?offset=10"))

    artists = run(make_client(handler).get_top_artists("t", "long_term", max_items=10))
    assert len(artists) == 10


def test_client_errors_raise_without_retrying():
    calls = []

    def handler(request):
        calls.append(request)
        error = {"status": 403, "message": "User not registered in the Developer Dashboard"}
        return httpx.Response(403, json={"error": error})

    with pytest.raises(SpotifyError) as excinfo:
        run(make_client(handler).get_profile("t"))
    assert len(calls) == 1
    assert excinfo.value.user_not_allowlisted


def test_gives_up_after_max_attempts_on_server_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(SpotifyError) as excinfo:
        run(make_client(handler, max_attempts=3).get_profile("t"))
    assert len(calls) == 3
    assert excinfo.value.status == 503


def test_network_errors_are_retried():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"id": "u", "account_id": "acct", "display_name": "Sam"})

    profile = run(make_client(handler).get_profile("t"))
    assert profile.account_id == "acct"


def test_exchange_code_sends_basic_auth_and_parses_tokens():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = request.content.decode()
        body = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "user-top-read"}
        return httpx.Response(200, json=body)

    tokens = run(make_client(handler).exchange_code("the-code"))
    assert seen["auth"].startswith("Basic ")
    assert "grant_type=authorization_code" in seen["body"] and "code=the-code" in seen["body"]
    assert tokens.refresh_token == "rt" and tokens.scope == "user-top-read"


def test_refresh_keeps_refresh_token_when_spotify_omits_it():
    def handler(request):
        return httpx.Response(200, json={"access_token": "new-at", "expires_in": 3600})

    old = TokenSet("old-at", "old-rt", NOW)
    fresh = run(make_client(handler).refresh(old))
    assert fresh.access_token == "new-at"
    assert fresh.refresh_token == "old-rt"


def test_app_token_is_cached_until_it_expires():
    token_requests = []
    now = [NOW]

    def handler(request):
        token_requests.append(request.content.decode())
        return httpx.Response(200, json={"access_token": f"app{len(token_requests)}", "expires_in": 3600})

    client = make_client(handler, clock=lambda: now[0])

    async def scenario():
        first = await client.app_token()
        again = await client.app_token()
        now[0] = NOW + timedelta(hours=2)
        later = await client.app_token()
        return first, again, later

    assert run(scenario()) == ("app1", "app1", "app2")
    assert token_requests[0] == "grant_type=client_credentials"


def test_find_track_searches_by_artist_with_a_market():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        item = {"id": "t1", "name": "Song", "uri": "spotify:track:t1", "artists": [{"id": "a1"}]}
        return httpx.Response(200, json={"tracks": {"items": [item]}})

    track = run(make_client(handler).find_track("tok", "Sade", "a1"))
    assert track.track_id == "t1"
    assert seen["q"] == 'artist:"Sade"' and seen["type"] == "track" and seen["market"] == "US"


def test_playlists_are_created_private_and_filled_by_uri():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(201, json={"id": "p1", "external_urls": {"spotify": "https://x/p1"}})
        return httpx.Response(201, json={"snapshot_id": "s"})

    client = make_client(handler)

    async def scenario():
        created = await client.create_playlist("tok", "Echo finds", "desc")
        await client.add_to_playlist("tok", created.playlist_id, ["spotify:track:t1"])
        return created

    assert run(scenario()).url == "https://x/p1"
    assert requests[0] == ("POST", "/v1/me/playlists", {"name": "Echo finds", "description": "desc", "public": False})
    assert requests[1] == ("POST", "/v1/playlists/p1/items", {"uris": ["spotify:track:t1"]})
