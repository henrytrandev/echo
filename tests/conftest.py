import pytest

from echo.security import generate_encryption_key


@pytest.fixture
def engine():
    from echo.db import create_schema, make_engine

    engine = make_engine("sqlite://")
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    from echo.db import make_session_factory

    factory = make_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture
def settings():
    from echo.config import Settings

    return Settings(
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        secret_key="test-secret-key",
        token_encryption_key=generate_encryption_key(),
        database_url="sqlite://",
    )
