"""Domain exception hierarchy.

All domain exceptions carry user-safe or operator-safe descriptions and must never
embed raw credentials, ciphertexts, or untrusted upstream response bodies.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base exception for all domain and business-rule violations."""


class DomainValidationError(DomainError):
    """Raised when user input or a domain invariant fails validation."""


class EntityNotFoundError(DomainError):
    """Raised when a requested seller or product does not exist."""


class SecretProtectionError(DomainError):
    """Base error for secret encryption, key validation, or context verification."""


class InvalidEncryptionKeyError(SecretProtectionError):
    """Raised when TOKEN_ENCRYPTION_KEY is malformed or empty."""


class TokenDecryptionError(SecretProtectionError):
    """Raised when stored ciphertext is corrupted, expired, or bound to another user."""


class MarketplaceError(DomainError):
    """Sanitized failure when communicating with an external marketplace API."""


class MarketplaceAuthError(MarketplaceError):
    """Raised when the marketplace rejects the seller API token (HTTP 401) or format."""


class MarketplacePermissionError(MarketplaceError):
    """Raised when the token lacks read access to the required API scope (HTTP 403)."""


class MarketplaceRateLimitError(MarketplaceError):
    """Raised when marketplace rate limiting persists after bounded retries (HTTP 429)."""


class MarketplaceResponseError(MarketplaceError):
    """Raised when the marketplace returns malformed, oversized, or looping data."""


class MarketplaceUnavailableError(MarketplaceError):
    """Raised on transient network errors, timeouts, redirects, or HTTP 5xx responses."""
