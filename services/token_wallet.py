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
    wallet_id = _client().ensure_wallet(
        external_user_id=str(user.id), email=user.email,
        customer_ref=user.jhome_sub,
    )
    user.jts_wallet_id = wallet_id
    db.commit()
    return wallet_id


def link_wallet_to_customer(db: Session, user: User) -> int:
    """Apply (or re-apply) customer_ref grouping for this user's JTS wallet.

    Unlike ensure_wallet, this ALWAYS calls the Token Service -- even when
    user.jts_wallet_id is already cached. The Token Service's POST /wallets is
    an idempotent get-or-create, so calling it again for an existing wallet is
    safe, and it is exactly what's needed to apply customer_ref grouping to a
    user whose wallet was created BEFORE they ever linked their Jhome identity
    (the common case: an existing Gootier customer connecting their account via
    the Backoffice handoff -- their wallet was minted at signup, so
    ensure_wallet's cache short-circuit would return immediately and the
    grouping call would never happen).

    ensure_wallet's cache short-circuit is correct for its OWN callers (balance
    checks, debits -- repeated calls there would be wasteful and grouping is
    not their concern), but wrong for this one. Same reasoning as RingBack's
    internal_api handoff: gating grouping on the first-sight jhome_sub
    transition makes it one-shot, so a single timeout leaves the user linked
    but ungrouped forever with nothing that could ever retry."""
    wallet_id = _client().ensure_wallet(
        external_user_id=str(user.id), email=user.email,
        customer_ref=user.jhome_sub,
    )
    if user.jts_wallet_id is None:
        user.jts_wallet_id = wallet_id
        db.commit()
    return wallet_id


def balance_tokens(db: Session, user: User) -> int:
    wallet_id = ensure_wallet(db, user)
    return _client().get_balance(wallet_id)


def balance_tokens_or_none(db: Session, user: User) -> Optional[int]:
    """The balance for DISPLAY, or None when the Token Service could not answer.

    Page renders must not die because the Token Service is unavailable. On
    2026-09-03 a ~30-minute JTS outage (empty-body 404s between a broken deploy
    and the "Restore token service" redeploy) turned every unguarded
    `balance_tokens(...)` call site into a 500 -- including /dashboard, which is
    where the Backoffice handoff lands, so a customer was signed in correctly
    and then shown an Internal Server Error, and /billing, which is the page
    they would have gone to in order to do something about it.

    An unreachable Token Service means the balance is UNKNOWN, not zero. None
    is the "unknown" value and renders as an em-dash; returning 0 instead would
    read as "you are out of tokens", which is a different and false statement.
    Same fail-open reasoning as debit_after_success, and the same catch-all for
    non-JTSError transport failures (httpx connect/read errors are not JTSError
    subclasses, and a host that stops answering entirely raises those).

    Deliberately NOT used by check_sufficient. Failing open on a label is not
    the same decision as failing open on an authorization: a balance we cannot
    read is not a balance we may authorize a charge against, so the spend gate
    keeps raising.
    """
    try:
        return balance_tokens(db, user)
    except JTSError:
        log.warning("token balance unavailable for display: user=%s", user.id, exc_info=True)
        return None
    except Exception:
        log.warning("token balance unavailable for display (non-JTS error): user=%s",
                    user.id, exc_info=True)
        return None


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
