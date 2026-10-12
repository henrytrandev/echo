"""End-to-end API tests: the real app, an in-memory database, and a fake Spotify."""

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from echo.models import Artist
from echo.repository import SourceArtist, replace_signals, upsert_user
from echo.spotify.oauth import SCOPES
from echo.web.app import SESSION_COOKIE, alias_for, create_app

ALL_SCOPES = " ".join(SCOPES)


def track_id_for(name):
    """A valid-looking 22-character Spotify track ID, derived from the artist name.

    Spotify IDs are plain ASCII letters and digits, so accented letters ("Rosalía") are dropped.
    """
    return ("T" + "".join(ch for ch in name if ch.isascii() and ch.isalnum()))[:22].ljust(22, "x")


class FakeSpotify:
    """Plays Spotify's token, profile, top-artists, search and playlist endpoints, and records calls."""

    def __init__(self, profile=None, top=None, profile_status=200, scope=ALL_SCOPES, search_status=200):
        self.profile = profile or {"id": "sam", "account_id": "acct-sam", "display_name": "Sam"}
        self.top = top or {"long_term": ["Radiohead", "Bjork", "Portishead"]}
        self.profile_status = profile_status
        self.scope = scope
        self.search_status = search_status
        self.calls = []
        self.deleted_playlists = set()
        self.playlists_created = 0

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.host == "accounts.spotify.com":
            if b"client_credentials" in request.content:
                return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
            body = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": self.scope}
            return httpx.Response(200, json=body)
        if request.url.path == "/v1/search":
            if self.search_status != 200:
                return httpx.Response(self.search_status, json={"error": {"status": self.search_status}})
            q, kind = request.url.params["q"], request.url.params["type"]
            if kind == "artist":
                return httpx.Response(200, json={"artists": {"items": [{"id": f"sp-{q}", "name": q}]}})
            name = q.removeprefix('artist:"').removesuffix('"')
            tid = track_id_for(name)
            track = {
                "id": tid,
                "name": f"Best of {name}",
                "uri": f"spotify:track:{tid}",
                "artists": [{"id": f"sp-{name}", "name": name}],
            }
            return httpx.Response(200, json={"tracks": {"items": [track]}})
        if request.method == "POST" and request.url.path == "/v1/me/playlists":
            self.playlists_created += 1
            pid = f"pl{self.playlists_created}"
            url = f"https://open.spotify.com/playlist/{pid}"
            return httpx.Response(201, json={"id": pid, "external_urls": {"spotify": url}})
        if request.method == "POST" and request.url.path.startswith("/v1/playlists/"):
            pid = request.url.path.split("/")[3]
            if pid in self.deleted_playlists:
                return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})
            return httpx.Response(201, json={"snapshot_id": "snap"})
        return self.handle_profile(request)

    def handle_profile(self, request: httpx.Request) -> httpx.Response:
        profile_status, profile, top = self.profile_status, self.profile, self.top
        if request.url.path == "/v1/me":
            if profile_status != 200:
                error = {"status": 403, "message": "User not registered in the Developer Dashboard"}
                return httpx.Response(profile_status, json={"error": error})
            return httpx.Response(200, json=profile)
        if request.url.path == "/v1/me/top/artists":
            names = top.get(request.url.params["time_range"], [])
            return httpx.Response(200, json={"items": [{"id": f"sp-{n}", "name": n} for n in names], "next": None})
        return httpx.Response(404)

    def count(self, method, path_prefix, **params):
        return sum(
            1
            for m, path, query in self.calls
            if m == method and path.startswith(path_prefix) and all(query.get(k) == v for k, v in params.items())
        )


def fake_spotify(**kwargs):
    return FakeSpotify(**kwargs).transport()


@pytest.fixture
def make_client(settings):
    def _make(transport=None, **overrides):
        app = create_app(replace(settings, **overrides), http_transport=transport or fake_spotify())
        return TestClient(app, base_url="http://127.0.0.1:8000")

    return _make


def log_in(client):
    login = client.get("/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    return client.get(f"/callback?code=abc&state={state}", follow_redirects=False)


def seed_user(client, name, artists, demo=False, with_ids=True):
    factory = client.app.state.session_factory
    with factory() as session:
        source = "demo" if demo else "spotify"
        user = upsert_user(session, source=source, external_id=name, display_name=name, is_demo=demo)
        entries = [
            SourceArtist(a, "long_term", r, spotify_id=f"sp-{a}" if with_ids else None)
            for r, a in enumerate(artists, 1)
        ]
        replace_signals(session, user, entries)
        session.commit()
        return user.id


def act_as(client, user_id):
    """Switch the test client's session cookie to another user."""
    client.cookies.set(SESSION_COOKIE, client.app.state.signer.sign(user_id))
    return client


# --------------------------------------------------------------------- auth


def test_api_requires_login(make_client):
    with make_client() as client:
        assert client.get("/api/me").status_code == 401


def test_login_redirects_to_spotify_with_state(make_client):
    with make_client() as client:
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302
        url = urlparse(response.headers["location"])
        assert url.netloc == "accounts.spotify.com"
        assert parse_qs(url.query)["scope"] == [" ".join(SCOPES)]


def test_callback_rejects_a_forged_state(make_client):
    with make_client() as client:
        client.get("/login", follow_redirects=False)
        response = client.get("/callback?code=abc&state=forged", follow_redirects=False)
        assert response.headers["location"] == "/?error=state_mismatch"


def test_full_login_imports_top_artists(make_client):
    with make_client() as client:
        response = log_in(client)
        assert response.status_code == 303 and response.headers["location"] == "/"
        me = client.get("/api/me").json()
        assert me["display_name"] == "Sam"
        assert me["top_artists"] == ["Radiohead", "Bjork", "Portishead"]


def test_logging_in_twice_does_not_duplicate_anything(make_client):
    with make_client() as client:
        log_in(client)
        log_in(client)
        assert client.get("/api/me").json()["top_artists"] == ["Radiohead", "Bjork", "Portishead"]


def test_account_not_on_allowlist_gets_a_clear_error(make_client):
    with make_client(fake_spotify(profile_status=403)) as client:
        assert log_in(client).headers["location"] == "/?error=not_allowlisted"


# ------------------------------------------------------------------ matches


def test_matches_are_anonymous_until_both_connect(make_client):
    with make_client() as client:
        # Background listeners give the co-occurrence stats something to learn from.
        for n, artists in enumerate([["A", "D", "E"], ["B", "D"], ["C", "E", "A"], ["A", "B", "D", "E"]]):
            seed_user(client, f"bg{n}", artists)
        me = seed_user(client, "me", ["A", "B", "C"])
        them = seed_user(client, "them", ["A", "B", "D", "E"])

        matches = act_as(client, me).get("/api/matches").json()
        match = next(m for m in matches if m["user_id"] == them)
        assert match["name"] == alias_for(them)
        assert {a["name"] for a in match["shared_artists"]} == {"A", "B"}

        first = act_as(client, me).post(f"/api/connections/{them}").json()
        assert first["status"] == "requested"
        second = act_as(client, them).post(f"/api/connections/{me}").json()
        assert second == {"user_id": me, "name": "me", "status": "connected"}

        connections = act_as(client, me).get("/api/connections").json()
        assert connections == [{"user_id": them, "name": "them", "status": "connected"}]


def test_hidden_users_do_not_appear_as_matches(make_client):
    with make_client() as client:
        me = seed_user(client, "me", ["A", "B", "C"])
        hidden = seed_user(client, "hidden", ["A", "B", "D"])
        act_as(client, hidden).patch("/api/me", json={"discoverable": False})
        ids = {m["user_id"] for m in act_as(client, me).get("/api/matches").json()}
        assert hidden not in ids


def test_demo_listeners_cannot_be_contacted(make_client):
    with make_client() as client:
        me = seed_user(client, "me", ["A", "B"])
        demo = seed_user(client, "demo", ["A", "C"], demo=True)
        response = act_as(client, me).post(f"/api/connections/{demo}")
        assert response.status_code == 400


# --------------------------------------------------------------------- demo


def test_demo_visitors_see_only_sample_listeners_and_cannot_change_anything(make_client):
    # Enough listeners that whichever one /demo picks at random has someone in their scene.
    with make_client(seed_demo_listeners=80) as client:
        real = seed_user(client, "real", ["Frank Ocean", "SZA", "Sade", "Steve Lacy"])

        response = client.get("/demo", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"

        me = client.get("/api/me").json()
        assert me["is_demo"] is True
        matches = client.get("/api/matches").json()
        assert matches and all(m["is_demo"] for m in matches)
        assert real not in {m["user_id"] for m in matches}

        assert client.patch("/api/me", json={"discoverable": False}).status_code == 403
        assert client.post("/api/me/refresh").status_code == 403
        assert client.post(f"/api/connections/{real}").status_code == 403


def test_demo_without_sample_listeners_explains_itself(make_client):
    with make_client() as client:
        assert client.get("/demo", follow_redirects=False).headers["location"] == "/?error=demo_unavailable"


def test_demo_can_be_switched_off(make_client):
    with make_client(seed_demo_listeners=5, allow_demo_login=False) as client:
        assert client.get("/demo", follow_redirects=False).status_code == 404


def test_real_users_do_not_see_sample_listeners_when_hidden(make_client):
    with make_client(seed_demo_listeners=30, show_demo_users=False) as client:
        me = seed_user(client, "me", ["Frank Ocean", "SZA", "Sade"])
        other = seed_user(client, "other", ["Frank Ocean", "SZA", "Solange"])
        matches = act_as(client, me).get("/api/matches").json()
        assert {m["user_id"] for m in matches} == {other}


# -------------------------------------------------------------------- songs


def artist_id(client, name):
    with client.app.state.session_factory() as session:
        return session.query(Artist).filter(Artist.name == name).one().id


def test_track_lookup_fills_in_missing_spotify_ids_and_is_cached(make_client):
    spotify = FakeSpotify()
    with make_client(spotify.transport()) as client:
        seed_user(client, "demo", ["Portishead"], demo=True, with_ids=False)
        act_as(client, seed_user(client, "me", ["Radiohead"]))
        aid = artist_id(client, "Portishead")

        first = client.get(f"/api/artists/{aid}/track").json()
        assert first["uri"] == f"spotify:track:{track_id_for('Portishead')}"
        assert first["spotify_url"].endswith(track_id_for("Portishead"))
        second = client.get(f"/api/artists/{aid}/track").json()
        assert second == first

        assert spotify.count("GET", "/v1/search", type="artist") == 1
        assert spotify.count("GET", "/v1/search", type="track") == 1
        assert spotify.count("POST", "/api/token") == 1  # the app token is reused
        with client.app.state.session_factory() as session:
            assert session.get(Artist, aid).spotify_id == "sp-Portishead"


def test_track_lookup_falls_back_to_a_link_when_spotify_fails(make_client):
    with make_client(FakeSpotify(search_status=403).transport()) as client:
        act_as(client, seed_user(client, "me", ["Radiohead"], with_ids=False))
        track = client.get(f"/api/artists/{artist_id(client, 'Radiohead')}/track").json()
        assert track["uri"] is None
        assert track["spotify_url"] == "https://open.spotify.com/search/Radiohead"


def test_songs_are_saved_to_one_echo_playlist(make_client):
    spotify = FakeSpotify()
    with make_client(spotify.transport()) as client:
        log_in(client)
        assert client.get("/api/me").json()["can_save_tracks"] is True
        first = client.post("/api/playlist/tracks", json={"track_id": track_id_for("Portishead")})
        second = client.post("/api/playlist/tracks", json={"track_id": track_id_for("Bjork")})
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json() == {"playlist_url": "https://open.spotify.com/playlist/pl1"}
        assert spotify.playlists_created == 1
        assert spotify.count("POST", "/v1/playlists/pl1/items") == 2


def test_a_deleted_playlist_is_recreated(make_client):
    spotify = FakeSpotify()
    with make_client(spotify.transport()) as client:
        log_in(client)
        client.post("/api/playlist/tracks", json={"track_id": track_id_for("Portishead")})
        spotify.deleted_playlists.add("pl1")  # the user deletes it in Spotify
        response = client.post("/api/playlist/tracks", json={"track_id": track_id_for("Bjork")})
        assert response.json() == {"playlist_url": "https://open.spotify.com/playlist/pl2"}


def test_old_logins_without_playlist_access_must_reconnect(make_client):
    with make_client(FakeSpotify(scope="user-top-read").transport()) as client:
        log_in(client)
        assert client.get("/api/me").json()["can_save_tracks"] is False
        response = client.post("/api/playlist/tracks", json={"track_id": track_id_for("Bjork")})
        assert response.status_code == 403


def test_demo_visitors_can_listen_but_not_save(make_client):
    with make_client(seed_demo_listeners=5) as client:
        client.get("/demo", follow_redirects=False)
        me = client.get("/api/me").json()
        assert me["can_save_tracks"] is False
        top = me["top_artists"][0]
        assert client.get(f"/api/artists/{artist_id(client, top)}/track").status_code == 200
        response = client.post("/api/playlist/tracks", json={"track_id": track_id_for(top)})
        assert response.status_code == 403


def test_save_rejects_malformed_track_ids(make_client):
    with make_client() as client:
        log_in(client)
        assert client.post("/api/playlist/tracks", json={"track_id": "../../me"}).status_code == 422


# ----------------------------------------------------------------- feedback


def matched_suggestions(client, me, them):
    matches = act_as(client, me).get("/api/matches").json()
    return next(m for m in matches if m["user_id"] == them)["they_could_put_you_on"]


def seed_suggestion_world(client):
    for n, artists in enumerate([["A", "D", "E"], ["B", "D"], ["C", "E", "A"], ["A", "B", "D", "E"]]):
        seed_user(client, f"bg{n}", artists)
    return seed_user(client, "me", ["A", "B", "C"]), seed_user(client, "them", ["A", "B", "D", "E"])


def test_suggestions_say_why_and_matches_carry_a_fingerprint(make_client):
    with make_client() as client:
        me, them = seed_suggestion_world(client)
        suggestions = matched_suggestions(client, me, them)
        assert suggestions and all(s["because"] in {"A", "B", "C"} for s in suggestions)

        match = next(m for m in act_as(client, me).get("/api/matches").json() if m["user_id"] == them)
        assert len(match["fingerprint"]) == 4 and max(match["fingerprint"]) == 1.0
        assert len(client.get("/api/me").json()["fingerprint"]) == 3


def test_not_for_me_hides_a_suggestion_until_undone(make_client):
    with make_client() as client:
        me, them = seed_suggestion_world(client)
        suggestion = matched_suggestions(client, me, them)[0]

        response = client.put(f"/api/feedback/{suggestion['id']}", json={"verdict": "dislike"})
        assert response.json() == {"artist_id": suggestion["id"], "verdict": "dislike"}
        assert suggestion["id"] not in {a["id"] for a in matched_suggestions(client, me, them)}

        client.put(f"/api/feedback/{suggestion['id']}", json={"verdict": None})
        assert suggestion["id"] in {a["id"] for a in matched_suggestions(client, me, them)}


def test_loved_artists_are_marked_everywhere(make_client):
    with make_client() as client:
        me, them = seed_suggestion_world(client)
        suggestion = matched_suggestions(client, me, them)[0]
        client.put(f"/api/feedback/{suggestion['id']}", json={"verdict": "like", "track_id": track_id_for("D")})

        marked = {a["id"]: a["verdict"] for a in matched_suggestions(client, me, them)}
        assert marked[suggestion["id"]] == "like"
        assert client.get(f"/api/artists/{suggestion['id']}/track").json()["verdict"] == "like"


def test_feedback_is_validated_and_needs_a_real_account(make_client):
    with make_client(seed_demo_listeners=5) as client:
        me, _ = seed_suggestion_world(client)
        aid = artist_id(client, "D")
        assert act_as(client, me).put(f"/api/feedback/{aid}", json={"verdict": "meh"}).status_code == 422
        assert client.put("/api/feedback/999999", json={"verdict": "like"}).status_code == 404

        client.get("/demo", follow_redirects=False)
        assert client.put(f"/api/feedback/{aid}", json={"verdict": "like"}).status_code == 403
