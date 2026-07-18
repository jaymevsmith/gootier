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
    """Get-or-create the user's JTS wallet id, caching it on the User row.

    Known tradeoff: two near-simultaneous first-time calls for the same user
    could both miss the cache below and both call JTS's ensure-wallet endpoint.
    Harmless — JTS's endpoint is documented as idempotent/get-or-create keyed
    by external_user_id, so both calls return the same wallet_id — just
    wasteful. Not fixed here, matching this migration's other known tradeoffs."""
    if user.jts_wallet_id is not None:
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
        # get_balance() only returns the raw token count today (by design, from
        # an earlier approved task) — no `_display` field is available here to
        # render instead. Per JTS's own integration doc, dividing by 1000 and
        # rounding is the documented fallback for a response shape that doesn't
        # carry a `_display` field yet; this isn't a "recompute instead of
        # rendering `_display`" oversight.
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
    try:
        wallet_id = ensure_wallet(db, user)
        return _client().debit(wallet_id, model_key, request_id, **usage)
    except InsufficientTokensError:
        log.error("token debit found insufficient balance after success: "
                  "user=%s model_key=%s request_id=%s", user.id, model_key, request_id)
        return None
    except JTSError:
        log.exception("token debit failed: user=%s model_key=%s request_id=%s",
                      user.id, model_key, request_id)
        return None
    except Exception:
        # Catch-all so a raw transport-level failure (e.g. httpx connect/read
        # timeout, DNS error) — which is NOT a JTSError subclass — can't
        # escape and blow up a caller that already has a successful result
        # (e.g. compose_ai_plan, a synchronous request handler). The typed
        # excepts above are the expected cases; this is the safety net for
        # everything else so the "never raises" contract actually holds.
        log.exception("token debit failed unexpectedly (non-JTS error): "
                      "user=%s model_key=%s request_id=%s", user.id, model_key, request_id)
        return None
