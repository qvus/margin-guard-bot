"""Logging filter defending against secret leakage and CRLF log injection."""

from __future__ import annotations

import logging
import re

_CONTROL_RE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_TG_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_FERNET_TOKEN_RE = re.compile(r"\bgAAAAA[A-Za-z0-9_-]{40,}=*\b")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;\"']+")


def redact_sensitive_text(text: str) -> str:
    """Scrub credentials and control characters from a log line."""
    sanitized = _CONTROL_RE.sub(" ", text)
    sanitized = _TG_BOT_TOKEN_RE.sub("[REDACTED_TG_TOKEN]", sanitized)
    sanitized = _JWT_RE.sub("[REDACTED_JWT]", sanitized)
    sanitized = _FERNET_TOKEN_RE.sub("[REDACTED_CIPHERTEXT]", sanitized)
    sanitized = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", sanitized)
    return sanitized


class RedactingLogFilter(logging.Filter):
    """Render and sanitize log messages before handlers emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = redact_sensitive_text(rendered)
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            # Avoid dumping raw exception arguments that might contain sensitive inputs;
            # retain only sanitized text when formatted.
            exc_type, exc_val, _ = record.exc_info
            safe_exc_msg = redact_sensitive_text(str(exc_val))
            if safe_exc_msg != str(exc_val) and exc_type is not None:
                record.exc_info = None
                record.msg = f"{record.msg} [{exc_type.__name__}: {safe_exc_msg}]"
        return True
