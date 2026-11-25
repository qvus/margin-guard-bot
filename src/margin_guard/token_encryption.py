"""Fernet encryption for Wildberries API tokens."""

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionError(RuntimeError):
    """The configured key is invalid or stored ciphertext cannot be decrypted."""


class TokenCipher:
    """Encrypt and decrypt API tokens using a key supplied outside the repository."""

    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (TypeError, ValueError) as exc:
            raise TokenEncryptionError(
                "TOKEN_ENCRYPTION_KEY is invalid; generate a Fernet key and check .env"
            ) from exc

    def encrypt(self, token: str) -> str:
        if not token:
            raise ValueError("Token must not be empty")
        return self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise TokenEncryptionError(
                "Saved token could not be decrypted; reconnect the WB token"
            ) from exc
