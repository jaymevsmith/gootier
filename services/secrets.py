"""Symmetric encryption for at-rest secrets (OAuth tokens, etc.).

We use Fernet (cryptography library) with a key sourced from env_config.
Encrypted values carry a versioned prefix (`enc:v1:`) so we can detect
ciphertext vs legacy plaintext on read — older rows continue to work,
new writes are encrypted transparently via the EncryptedString TypeDecorator.

Key derivation:
  * If TOKEN_ENCRYPTION_KEY env is set (32-byte url-safe base64), use it.
  * Else derive deterministically from SECRET_KEY via HKDF-SHA256 so existing
    installs keep working. Log a warning recommending an explicit key.
"""
import base64
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

# NOTE: auth + env_config imports happen LAZILY inside _fernet() — importing
# them at module-load time creates a circular dep through models.

logger = logging.getLogger("gootier.secrets")
PREFIX = "enc:v1:"

_cached_fernet: Optional[Fernet] = None
_cached_source: Optional[str] = None  # for re-derive on env change


def _derive_from_secret_key() -> bytes:
    """HKDF-SHA256 derivation from SECRET_KEY → 32-byte Fernet key."""
    from auth import SECRET_KEY  # lazy — breaks circular import
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"gootier-fernet-v1",
        info=b"token-encryption",
    ).derive(SECRET_KEY.encode())
    return base64.urlsafe_b64encode(raw)


def _fernet() -> Fernet:
    global _cached_fernet, _cached_source
    from services.env_config import get_env  # lazy — breaks circular import
    explicit = get_env("TOKEN_ENCRYPTION_KEY", "")
    source_marker = explicit or "__derived__"
    if _cached_fernet is None or _cached_source != source_marker:
        if explicit:
            try:
                # Accept either a real 32-byte b64-urlsafe key (44 chars) or a
                # raw secret we'll SHA-256-hash into one.
                if len(explicit) == 44 and explicit.endswith("="):
                    key_bytes = explicit.encode()
                else:
                    key_bytes = base64.urlsafe_b64encode(
                        hashlib.sha256(explicit.encode()).digest()
                    )
                _cached_fernet = Fernet(key_bytes)
            except Exception as e:
                logger.warning("TOKEN_ENCRYPTION_KEY invalid (%s) — falling back to derived", e)
                _cached_fernet = Fernet(_derive_from_secret_key())
        else:
            logger.info("TOKEN_ENCRYPTION_KEY not set — deriving from SECRET_KEY")
            _cached_fernet = Fernet(_derive_from_secret_key())
        _cached_source = source_marker
    return _cached_fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string, return `enc:v1:<base64>`. Idempotent — already-encrypted
    values pass through unchanged."""
    if plaintext is None:
        return None
    if plaintext.startswith(PREFIX):
        return plaintext
    return PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt. Plaintext (no prefix) is returned as-is for backward compat."""
    if value is None:
        return None
    if not value.startswith(PREFIX):
        return value
    body = value[len(PREFIX):]
    try:
        return _fernet().decrypt(body.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt — wrong key? Returning empty string to avoid leaking ciphertext.")
        return ""


class EncryptedString(TypeDecorator):
    """SQLAlchemy column type that encrypts on write + decrypts on read.

    Existing plaintext rows continue to work until they're rewritten, at which
    point they pick up the `enc:v1:` prefix.
    """
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt(value)
