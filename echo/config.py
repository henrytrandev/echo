"""Settings, read from environment variables (and a .env file if python-dotenv is installed)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    secret_key: str
    """Signs session cookies and OAuth state. Any long random string."""
    token_encryption_key: str
    """Encrypts stored Spotify tokens. Generate one with `python -m echo keys`."""
    database_url: str = "sqlite:///./echo.db"
    spotify_redirect_uri: str = "http://127.0.0.1:8000/callback"
    """Spotify only allows plain http for loopback IPs, so use 127.0.0.1, not localhost."""
    show_demo_users: bool = True
    """Show demo listeners as matches to real users. Turn off before inviting real people."""
    allow_demo_login: bool = True
    """Let visitors look around as a demo listener without logging in ("Try the demo")."""
    seed_demo_listeners: int = 0
    """On startup, make sure this many demo listeners exist (0 = don't seed). Useful on hosts
    where you can't run `python -m echo seed-demo` yourself."""
    refresh_after_hours: int = 24
    secure_cookies: bool = False
    """Set to true when serving over https."""


_REQUIRED = {
    "SPOTIFY_CLIENT_ID": "spotify_client_id",
    "SPOTIFY_CLIENT_SECRET": "spotify_client_secret",
    "SECRET_KEY": "secret_key",
    "TOKEN_ENCRYPTION_KEY": "token_encryption_key",
}


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_database_url(url: str) -> str:
    """Hosted Postgres providers hand out `postgres://` or `postgresql://` URLs.

    SQLAlchemy needs to be told which driver to use, so point those at psycopg 3.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@lru_cache
def get_settings() -> Settings:
    try:
        from dotenv import load_dotenv
    except ImportError:  # optional dependency
        pass
    else:
        load_dotenv()

    missing = [env for env in _REQUIRED if not os.environ.get(env)]
    if missing:
        raise ConfigError(
            "Missing required settings: " + ", ".join(missing) + ". Copy .env.example to .env and fill them in."
        )

    values: dict[str, object] = {field: os.environ[env] for env, field in _REQUIRED.items()}
    if url := os.environ.get("DATABASE_URL"):
        values["database_url"] = normalize_database_url(url)
    if uri := os.environ.get("SPOTIFY_REDIRECT_URI"):
        values["spotify_redirect_uri"] = uri
    for env, field in (
        ("SHOW_DEMO_USERS", "show_demo_users"),
        ("ALLOW_DEMO_LOGIN", "allow_demo_login"),
        ("SECURE_COOKIES", "secure_cookies"),
    ):
        if (raw := os.environ.get(env)) is not None:
            values[field] = _flag(raw)
    for env, field in (
        ("SEED_DEMO_LISTENERS", "seed_demo_listeners"),
        ("REFRESH_AFTER_HOURS", "refresh_after_hours"),
    ):
        if raw := os.environ.get(env):
            values[field] = int(raw)
    return Settings(**values)  # type: ignore[arg-type]
