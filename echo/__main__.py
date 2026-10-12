"""Command-line tools.

    python -m echo keys                 print fresh SECRET_KEY and TOKEN_ENCRYPTION_KEY values
    python -m echo seed-demo [--users N] load synthetic listeners (safe to re-run)
    python -m echo refresh              re-import top artists for anyone not refreshed recently
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from datetime import timedelta


def _database_url() -> str:
    """Seeding only needs the database, so don't demand Spotify credentials for it."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        load_dotenv()
    return os.environ.get("DATABASE_URL", "sqlite:///./echo.db")


def cmd_keys(_: argparse.Namespace) -> None:
    from echo.security import generate_encryption_key

    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"TOKEN_ENCRYPTION_KEY={generate_encryption_key()}")


def cmd_seed_demo(args: argparse.Namespace) -> None:
    from echo.db import create_schema, make_engine, make_session_factory, session_scope
    from echo.seeding import seed_demo_listeners

    engine = make_engine(_database_url())
    create_schema(engine)
    with session_scope(make_session_factory(engine)) as session:
        count = seed_demo_listeners(session, args.users, seed=args.seed)
    print(f"Loaded {count} demo listeners.")


def cmd_refresh(args: argparse.Namespace) -> None:
    import httpx

    from echo.config import get_settings
    from echo.db import create_schema, make_engine, make_session_factory
    from echo.ingest import refresh_stale_users
    from echo.security import TokenCipher
    from echo.spotify.client import SpotifyClient

    settings = get_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    factory = make_session_factory(engine)
    older_than = timedelta(hours=args.older_than_hours or settings.refresh_after_hours)

    async def run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
            client = SpotifyClient(
                settings.spotify_client_id, settings.spotify_client_secret, settings.spotify_redirect_uri, http
            )
            return await refresh_stale_users(
                factory, client, TokenCipher(settings.token_encryption_key), older_than, args.concurrency
            )

    report = asyncio.run(run())
    print(f"Refreshed {len(report.refreshed)} listener(s); {len(report.failed)} failed.")
    for user_id, error in report.failed.items():
        print(f"  {user_id}: {error}")
    if report.failed:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m echo")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("keys", help="print new secret keys for .env").set_defaults(func=cmd_keys)

    seed = commands.add_parser("seed-demo", help="load synthetic demo listeners")
    seed.add_argument("--users", type=int, default=150)
    seed.add_argument("--seed", type=int, default=7)
    seed.set_defaults(func=cmd_seed_demo)

    refresh = commands.add_parser("refresh", help="re-import top artists for stale profiles")
    refresh.add_argument("--older-than-hours", type=int, default=None)
    refresh.add_argument("--concurrency", type=int, default=4)
    refresh.set_defaults(func=cmd_refresh)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
