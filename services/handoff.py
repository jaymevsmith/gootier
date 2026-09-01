"""Token generation and hashing for the Backoffice SSO handoff. Only the
SHA-256 hash is ever stored -- the plaintext token exists only in memory and
in the one response that hands it back inside a consume_url.
"""
import hashlib
import secrets
from datetime import datetime, timedelta

TOKEN_TTL_MINUTES = 2


def generate_token() -> str:
    """Return a new random token in plaintext. Never store this value."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def default_expiry() -> datetime:
    return datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MINUTES)
