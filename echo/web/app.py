"""FastAPI app: Spotify login, your profile, your matches, and connections.

No `from __future__ import annotations` in this module: FastAPI reads the
dependency annotations at runtime, and string annotations break that.
"""

import hashlib
import logging
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from echo import __version__
from echo.config import Settings, get_settings
from echo.db import create_schema, make_engine, make_session_factory, session_scope
from echo.ingest import ensure_fresh, fetch_top_artists, import_after_login, load_tokens, store_tokens
from echo.matching import MatchEngine, affinity, fingerprint
from echo.models import Artist, User
from echo.repository import (
    artist_names,
    connection_statuses,
    feedback_for,
    load_pool,
    replace_signals,
    request_connection,
    set_feedback,
    signals_for,
    top_artist_names,
)
from echo.security import Signer, TokenCipher
from echo.seeding import ensure_demo_listeners
from echo.spotify.client import SpotifyClient, SpotifyError
from echo.spotify.oauth import build_authorize_url
from echo.tracks import can_save_tracks, fallback_info, save_track, track_for_artist
from echo.web.schemas import (
    ArtistOut,
    ConnectionOut,
    FeedbackIn,
    FeedbackOut,
    MatchOut,
    MeOut,
    MeUpdate,
    SaveTrackIn,
    SaveTrackOut,
    TrackOut,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "echo_session"
STATE_COOKIE = "echo_oauth_state"
SESSION_MAX_AGE = 30 * 24 * 3600
DEMO_SESSION_MAX_AGE = 24 * 3600
STATE_MAX_AGE = 10 * 60


# ------------------------------------------------------------ dependencies


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_spotify(request: Request) -> SpotifyClient:
    return request.app.state.spotify


SpotifyDep = Annotated[SpotifyClient, Depends(get_spotify)]


def current_user(request: Request, session: SessionDep) -> User:
    signed = request.cookies.get(SESSION_COOKIE)
    user_id = request.app.state.signer.unsign(signed, SESSION_MAX_AGE) if signed else None
    user = session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="Log in with Spotify first.")
    return user


UserDep = Annotated[User, Depends(current_user)]


def alias_for(user_id: str) -> str:
    """A stable anonymous name, shown until two people have both chosen to connect."""
    return "Listener " + hashlib.sha256(user_id.encode()).hexdigest()[:4].upper()


def public_name(user: User, status: str) -> str:
    return user.display_name if user.is_demo or status == "connected" else alias_for(user.id)


def require_real_account(user: User, action: str) -> None:
    """Demo visitors can look around but not change anything or contact anyone."""
    if user.is_demo:
        raise HTTPException(status_code=403, detail=f"You're viewing the demo, so you can't {action}.")


# ---------------------------------------------------------------------- app


def create_app(
    settings: Settings | None = None,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake Spotify transport."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    session_factory = make_session_factory(engine)
    if settings.seed_demo_listeners:
        with session_scope(session_factory) as session:
            written = ensure_demo_listeners(session, settings.seed_demo_listeners)
        if written:
            log.info("seeded %d demo listeners", written)
    cipher = TokenCipher(settings.token_encryption_key)
    cookie_options = {"httponly": True, "samesite": "lax", "secure": settings.secure_cookies}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), transport=http_transport) as http:
            app.state.spotify = SpotifyClient(
                settings.spotify_client_id,
                settings.spotify_client_secret,
                settings.spotify_redirect_uri,
                http,
            )
            yield
        engine.dispose()

    app = FastAPI(title="Echo", version=__version__, lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.signer = Signer(settings.secret_key)

    def me_out(session: Session, user: User) -> MeOut:
        return MeOut(
            id=user.id,
            display_name=user.display_name,
            is_demo=user.is_demo,
            discoverable=user.discoverable,
            can_save_tracks=can_save_tracks(user),
            last_ingested_at=user.last_ingested_at,
            top_artists=top_artist_names(session, user.id),
            fingerprint=fingerprint(affinity(signals_for(session, user.id))),
        )

    # ------------------------------------------------------------- pages

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -------------------------------------------------------------- auth

    @app.get("/login", include_in_schema=False)
    def login() -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        url = build_authorize_url(settings.spotify_client_id, settings.spotify_redirect_uri, state)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(STATE_COOKIE, app.state.signer.sign(state), max_age=STATE_MAX_AGE, **cookie_options)
        return response

    @app.get("/callback", include_in_schema=False)
    async def callback(
        request: Request,
        session: SessionDep,
        spotify: SpotifyDep,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        if error:
            return _home_with_error("spotify_denied")
        signed_state = request.cookies.get(STATE_COOKIE)
        expected = app.state.signer.unsign(signed_state, STATE_MAX_AGE) if signed_state else None
        # The state check stops another site from logging you in to their account (login CSRF).
        if not code or not state or expected is None or not secrets.compare_digest(state, expected):
            return _home_with_error("state_mismatch")
        try:
            user = await import_after_login(session, spotify, cipher, code)
            session.commit()
        except SpotifyError as exc:
            session.rollback()
            log.warning("login import failed: %s", exc)
            return _home_with_error("not_allowlisted" if exc.user_not_allowlisted else "spotify_error")

        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, app.state.signer.sign(user.id), max_age=SESSION_MAX_AGE, **cookie_options)
        response.delete_cookie(STATE_COOKIE)
        return response

    @app.post("/logout", include_in_schema=False)
    def logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/demo", include_in_schema=False)
    def demo(session: SessionDep) -> RedirectResponse:
        """Look around as a random demo listener, no Spotify login needed.

        Spotify caps development-mode apps at 5 users, so this is how anyone else
        (a recruiter following a link, say) gets to see the app working.
        """
        if not settings.allow_demo_login:
            raise HTTPException(status_code=404)
        demo_ids = list(session.scalars(select(User.id).where(User.is_demo.is_(True))))
        if not demo_ids:
            return _home_with_error("demo_unavailable")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            app.state.signer.sign(secrets.choice(demo_ids)),
            max_age=DEMO_SESSION_MAX_AGE,
            **cookie_options,
        )
        return response

    # ----------------------------------------------------------------- me

    @app.get("/api/me", response_model=MeOut)
    def get_me(user: UserDep, session: SessionDep) -> MeOut:
        return me_out(session, user)

    @app.patch("/api/me", response_model=MeOut)
    def update_me(body: MeUpdate, user: UserDep, session: SessionDep) -> MeOut:
        require_real_account(user, "change settings")
        user.discoverable = body.discoverable
        session.commit()
        return me_out(session, user)

    @app.post("/api/me/refresh", response_model=MeOut)
    async def refresh_me(user: UserDep, session: SessionDep, spotify: SpotifyDep) -> MeOut:
        require_real_account(user, "refresh artists")
        if user.token is None:
            raise HTTPException(status_code=400, detail="This profile isn't linked to Spotify.")
        tokens = load_tokens(user.token, cipher)
        try:
            fresh = await ensure_fresh(spotify, tokens)
            if fresh is not tokens:
                store_tokens(session, user, fresh, cipher)
                session.commit()
            entries = await fetch_top_artists(spotify, fresh.access_token)
        except SpotifyError as exc:
            raise HTTPException(status_code=502, detail=f"Spotify couldn't be reached: {exc}") from exc
        replace_signals(session, user, entries)
        session.commit()
        return me_out(session, user)

    # ------------------------------------------------------------ matches

    @app.get("/api/matches", response_model=list[MatchOut])
    def get_matches(user: UserDep, session: SessionDep, k: Annotated[int, Query(ge=1, le=50)] = 10) -> list[MatchOut]:
        # Rebuilt per request: cheap at this app's scale (a sparse matrix over everyone's top
        # artists). Cache it keyed on the last ingestion time if the pool grows large.
        engine_ = MatchEngine.build(load_pool(session))
        people = {u.id: u for u in session.scalars(select(User))}
        if user.is_demo:
            # Demo visitors only ever see other demo listeners, never real people.
            candidates = {uid for uid, other in people.items() if other.is_demo}
        else:
            candidates = {
                uid
                for uid, other in people.items()
                if other.discoverable and (settings.show_demo_users or not other.is_demo)
            }
        matches = engine_.matches_for(user.id, candidates=candidates, k=k)

        artist_ids = {
            a
            for m in matches
            for a in (
                *m.shared_artists,
                *m.they_could_put_you_on,
                *m.you_could_put_them_on,
                *m.because_for_you.values(),
            )
        }
        names = artist_names(session, artist_ids)
        statuses = connection_statuses(session, user.id)
        verdicts = feedback_for(session, user.id)

        def named(ids: list) -> list[ArtistOut]:
            return [ArtistOut(id=i, name=names[i], verdict=verdicts.get(i)) for i in ids if i in names]

        def suggestions(ids: list, because: dict) -> list[ArtistOut]:
            # Artists you said "not for me" to stay out of what people could put you onto.
            out = [a for a in named(ids) if a.verdict != "dislike"]
            for artist in out:
                artist.because = names.get(because.get(artist.id))
            return out

        out = []
        for m in matches:
            other = people[m.user_id]
            status = statuses.get(m.user_id, "none")
            out.append(
                MatchOut(
                    user_id=m.user_id,
                    name=public_name(other, status),
                    is_demo=other.is_demo,
                    score=round(m.score, 4),
                    overlap=round(m.overlap, 4),
                    discovery_for_you=round(m.discovery_for_you, 4),
                    discovery_for_them=round(m.discovery_for_them, 4),
                    shared_artists=named(m.shared_artists),
                    they_could_put_you_on=suggestions(m.they_could_put_you_on, m.because_for_you),
                    you_could_put_them_on=named(m.you_could_put_them_on),
                    connection=status,
                    fingerprint=fingerprint(engine_.affinities(m.user_id)),
                )
            )
        return out

    # -------------------------------------------------------------- songs

    @app.get("/api/artists/{artist_id}/track", response_model=TrackOut)
    async def artist_track(artist_id: int, user: UserDep, session: SessionDep, spotify: SpotifyDep) -> TrackOut:
        """A song to play for an artist. Open to demo visitors too: listening changes nothing."""
        artist = session.get(Artist, artist_id)
        if artist is None:
            raise HTTPException(status_code=404, detail="No such artist.")
        try:
            info = await track_for_artist(session, spotify, artist)
            session.commit()
        except SpotifyError as exc:
            # Don't fail the page: no song this time, but the Spotify link still works.
            session.rollback()
            log.warning("track lookup failed for artist %s: %s", artist_id, exc)
            info = fallback_info(artist)
        return TrackOut(
            artist_id=info.artist_id,
            artist_name=info.artist_name,
            track_id=info.track_id,
            track_name=info.track_name,
            uri=info.uri,
            spotify_url=info.spotify_url,
            verdict=feedback_for(session, user.id).get(artist_id),
        )

    @app.put("/api/feedback/{artist_id}", response_model=FeedbackOut)
    def give_feedback(artist_id: int, body: FeedbackIn, user: UserDep, session: SessionDep) -> FeedbackOut:
        """Love it / not for me on a suggested artist. A verdict of null removes it."""
        require_real_account(user, "save feedback")
        if session.get(Artist, artist_id) is None:
            raise HTTPException(status_code=404, detail="No such artist.")
        set_feedback(session, user.id, artist_id, body.verdict, body.track_id)
        session.commit()
        return FeedbackOut(artist_id=artist_id, verdict=body.verdict)

    @app.post("/api/playlist/tracks", response_model=SaveTrackOut)
    async def add_to_playlist(
        body: SaveTrackIn, user: UserDep, session: SessionDep, spotify: SpotifyDep
    ) -> SaveTrackOut:
        require_real_account(user, "save songs")
        if not can_save_tracks(user):
            raise HTTPException(
                status_code=403,
                detail="Log in with Spotify again and allow playlist access to save songs.",
            )
        try:
            url = await save_track(session, spotify, cipher, user, body.track_id)
            session.commit()
        except SpotifyError as exc:
            session.rollback()
            raise HTTPException(status_code=502, detail=f"Spotify couldn't save the song: {exc}") from exc
        return SaveTrackOut(playlist_url=url)

    # -------------------------------------------------------- connections

    @app.post("/api/connections/{other_id}", response_model=ConnectionOut)
    def connect(other_id: str, user: UserDep, session: SessionDep) -> ConnectionOut:
        require_real_account(user, "send connection requests")
        other = session.get(User, other_id)
        if other is None or other.id == user.id:
            raise HTTPException(status_code=404, detail="No such listener.")
        if other.is_demo:
            raise HTTPException(status_code=400, detail="Demo listeners are sample data and can't connect.")
        already_asked_you = connection_statuses(session, user.id).get(other.id) == "incoming"
        if not other.discoverable and not already_asked_you:
            raise HTTPException(status_code=404, detail="No such listener.")
        status = request_connection(session, user.id, other.id)
        session.commit()
        return ConnectionOut(user_id=other.id, name=public_name(other, status), status=status)

    @app.get("/api/connections", response_model=list[ConnectionOut])
    def list_connections(user: UserDep, session: SessionDep) -> list[ConnectionOut]:
        statuses = connection_statuses(session, user.id)
        people = {u.id: u for u in session.scalars(select(User).where(User.id.in_(list(statuses))))}
        return [
            ConnectionOut(user_id=uid, name=public_name(people[uid], status), status=status)
            for uid, status in sorted(statuses.items(), key=lambda kv: kv[1])
            if uid in people
        ]

    return app


def _home_with_error(code: str) -> RedirectResponse:
    return RedirectResponse(f"/?error={code}", status_code=303)
