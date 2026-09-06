"""Token generation and hashing for the Backoffice SSO handoff. Only the
SHA-256 hash is ever stored -- the plaintext token exists only in memory and
in the one response that hands it back inside a consume_url.
"""
import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models import HandoffToken

TOKEN_TTL_MINUTES = 2
REAP_RETENTION = timedelta(days=1)


def generate_token() -> str:
    """Return a new random token in plaintext. Never store this value."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def default_expiry() -> datetime:
    return datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MINUTES)


def reap_expired_tokens(db: Session) -> int:
    """Delete handoff_tokens rows past their expiry plus a retention grace
    period. Runs from the scheduler loop; rows are single-use and hash-only,
    so nothing is lost by deleting used or long-expired ones."""
    cutoff = datetime.utcnow() - REAP_RETENTION
    deleted = (
        db.query(HandoffToken)
        .filter(HandoffToken.expires_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted
