"""Encryption for stored OAuth tokens and signing for cookies."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class TokenCipher:
    """Encrypts tokens at rest, so a leaked database doesn't leak Spotify access."""

    def __init__(self, key: str) -> None:
        # Accept standard base64 too (e.g. a host's generated 256-bit secret); Fernet wants url-safe.
        self._fernet = Fernet(key.strip().replace("+", "-").replace("/", "_").encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("token could not be decrypted (wrong TOKEN_ENCRYPTION_KEY?)") from exc


class Signer:
    """Tamper-proof, expiring values for the session cookie and the OAuth `state` check."""

    def __init__(self, secret_key: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt="echo")

    def sign(self, value: str) -> str:
        return self._serializer.dumps(value)

    def unsign(self, signed: str, max_age_seconds: int) -> str | None:
        try:
            return self._serializer.loads(signed, max_age=max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()
