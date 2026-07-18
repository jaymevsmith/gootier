"""App-facing wrapper around services/jts_client.py.

Signature convention mirrors the old services/credits.py: (db, user, ...).
Wallet id is cached on User.jts_wallet_id after first lookup."""
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import User
from services.jts_client import InsufficientTokensError, JTSClient, JTSError

log = logging.getLogger("gootier.token_wallet")

_client_instance: Optional[JTSClient] = None


def _client() -> JTSClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JTSClient()
    return _client_instance


def ensure_wallet(db: Session, user: User) -> int:
    """Get-or-create the user's JTS wallet id, caching it on the User row."""
    if user.jts_wallet_id:
        return user.jts_wallet_id
    wallet_id = _client().ensure_wallet(external_user_id=str(user.id), email=user.email)
    user.jts_wallet_id = wallet_id
    db.commit()
    return wallet_id


def balance_tokens(db: Session, user: User) -> int:
    wallet_id = ensure_wallet(db, user)
    return _client().get_balance(wallet_id)


def check_sufficient(db: Session, user: User, estimated_tokens: int) -> None:
    """Soft pre-flight gate — not atomic, purely UX (see design doc 'Debit timing').
    Raises 402 if the current balance clearly can't cover the estimate."""
    current = balance_tokens(db, user)
    if current < estimated_tokens:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient tokens: you have {current // 1000}, this needs "
                   f"about {estimated_tokens // 1000}. Top up at /billing.",
        )


def debit_after_success(db: Session, user: User, model_key: str, request_id: str,
                         **usage) -> Optional[dict]:
    """Call only after the AI/media call has already succeeded. Never raises —
    a debit failure must not undo or block a result the user already has;
    it's logged loudly instead so it can be reconciled manually."""
    wallet_id = ensure_wallet(db, user)
    try:
        return _client().debit(wallet_id, model_key, request_id, **usage)
    except InsufficientTokensError:
        log.error("token debit found insufficient balance after success: "
                  "user=%s model_key=%s request_id=%s", user.id, model_key, request_id)
        return None
    except JTSError:
        log.exception("token debit failed: user=%s model_key=%s request_id=%s",
                      user.id, model_key, request_id)
        return None
