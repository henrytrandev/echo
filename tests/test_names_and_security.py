import base64

import pytest

from echo.config import normalize_database_url
from echo.names import normalize_artist_name
from echo.security import Signer, TokenCipher, generate_encryption_key


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Beyoncé", "beyonce"),
        ("Florence + The Machine", "florence and the machine"),
        ("Simon & Garfunkel", "Simon and Garfunkel"),
        ("  Tyler,   The Creator ", "tyler the creator"),
        ("Sigur Rós", "SIGUR ROS"),
    ],
)
def test_equivalent_names_normalize_the_same(a, b):
    assert normalize_artist_name(a) == normalize_artist_name(b)


def test_normalization_keeps_distinguishing_words():
    assert normalize_artist_name("The National") != normalize_artist_name("National")


def test_symbol_only_names_do_not_collapse_to_empty():
    assert normalize_artist_name("!!!") == "!!!"


def test_token_cipher_round_trips_and_hides_plaintext():
    cipher = TokenCipher(generate_encryption_key())
    encrypted = cipher.encrypt("secret-refresh-token")
    assert "secret-refresh-token" not in encrypted
    assert cipher.decrypt(encrypted) == "secret-refresh-token"


def test_token_cipher_rejects_wrong_key():
    encrypted = TokenCipher(generate_encryption_key()).encrypt("x")
    with pytest.raises(ValueError):
        TokenCipher(generate_encryption_key()).decrypt(encrypted)


def test_signer_detects_tampering_and_wrong_key():
    signer = Signer("key-one")
    signed = signer.sign("user-123")
    assert signer.unsign(signed, max_age_seconds=60) == "user-123"
    assert signer.unsign(signed + "x", max_age_seconds=60) is None
    assert Signer("key-two").unsign(signed, max_age_seconds=60) is None


def test_token_cipher_accepts_standard_base64_keys():
    key = base64.b64encode(bytes([0xFB, 0xFF] * 16)).decode()  # contains "+" and "/"
    assert "+" in key and "/" in key
    cipher = TokenCipher(key)
    assert cipher.decrypt(cipher.encrypt("x")) == "x"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("sqlite:///./echo.db", "sqlite:///./echo.db"),
    ],
)
def test_database_urls_get_a_driver(given, expected):
    assert normalize_database_url(given) == expected
