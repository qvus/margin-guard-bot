"""Context-bound MultiFernet encryption for seller API tokens."""

from __future__ import annotations

import json
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from margin_guard.domain.exceptions import (
    InvalidEncryptionKeyError,
    TokenDecryptionError,
)
from margin_guard.domain.value_objects import MAX_WB_TOKEN_LENGTH

_ENVELOPE_VERSION = 1
_MAX_CIPHERTEXT_LENGTH = 16_384


class FernetTokenCipher:
    """Encrypt and decrypt seller tokens with key-rotation and subject binding.

    ``raw_keys`` accepts either a single Fernet key or a comma-separated keyring
    (``NEW_KEY,OLD_KEY``). New ciphertexts are encrypted with the first key while
    older keys remain valid for decryption during zero-downtime rotation.

    Each token is wrapped in an HMAC-authenticated envelope bound to ``subject``
    (the seller's Telegram ID), preventing cross-row ciphertext transplant attacks
    if database records are ever swapped or tampered with.
    """

    def __init__(
        self,
        raw_keys: str | bytes,
        *,
        allow_legacy_unbound: bool = False,
    ) -> None:
        text = (
            raw_keys.decode("utf-8", errors="strict")
            if isinstance(raw_keys, bytes)
            else raw_keys
        )
        key_parts = [part.strip() for part in text.split(",") if part.strip()]
        if not key_parts:
            raise InvalidEncryptionKeyError(
                "TOKEN_ENCRYPTION_KEY is empty; generate a Fernet key and check .env"
            )
        try:
            fernets = [Fernet(part.encode("ascii")) for part in key_parts]
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise InvalidEncryptionKeyError(
                "TOKEN_ENCRYPTION_KEY is invalid; generate a Fernet key and check .env"
            ) from exc

        self._cipher = MultiFernet(fernets)
        self._allow_legacy_unbound = allow_legacy_unbound

    def encrypt(self, plaintext: str, *, subject: str) -> str:
        if not plaintext or len(plaintext) > MAX_WB_TOKEN_LENGTH:
            raise ValueError("Token length is invalid")
        if not subject or not subject.strip():
            raise ValueError("Encryption subject must not be empty")

        envelope = json.dumps(
            {"v": _ENVELOPE_VERSION, "sub": subject.strip(), "tok": plaintext},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return self._cipher.encrypt(envelope).decode("ascii")

    def decrypt(self, ciphertext: str, *, subject: str) -> str:
        if (
            not ciphertext
            or len(ciphertext) > _MAX_CIPHERTEXT_LENGTH
            or not subject.strip()
        ):
            raise TokenDecryptionError(
                "Saved token could not be decrypted; reconnect the WB token"
            )
        try:
            raw_bytes = self._cipher.decrypt(ciphertext.encode("ascii"))
            decoded = raw_bytes.decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise TokenDecryptionError(
                "Saved token could not be decrypted; reconnect the WB token"
            ) from exc

        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            if self._allow_legacy_unbound and decoded:
                return decoded
            raise TokenDecryptionError(
                "Saved token envelope is invalid; reconnect the WB token"
            ) from exc

        if not isinstance(payload, dict) or payload.get("v") != _ENVELOPE_VERSION:
            raise TokenDecryptionError(
                "Saved token envelope version is unsupported; reconnect the WB token"
            )
        stored_subject = payload.get("sub")
        stored_token = payload.get("tok")
        if not isinstance(stored_subject, str) or not isinstance(stored_token, str):
            raise TokenDecryptionError(
                "Saved token envelope is malformed; reconnect the WB token"
            )
        if not secrets.compare_digest(stored_subject, subject.strip()):
            raise TokenDecryptionError(
                "Saved token subject mismatch; reconnect the WB token"
            )
        if not stored_token or len(stored_token) > MAX_WB_TOKEN_LENGTH:
            raise TokenDecryptionError(
                "Saved token payload is invalid; reconnect the WB token"
            )
        return stored_token

    def rotate(self, ciphertext: str, *, subject: str) -> str:
        """Verify subject ownership and re-encrypt under the primary keyring key."""
        plaintext = self.decrypt(ciphertext, subject=subject)
        return self.encrypt(plaintext, subject=subject)
