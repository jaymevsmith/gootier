"""Credit ledger — every grant + spend is an append-only row in `credit_ledger`.

Balance is `SUM(delta)`. Monthly tier grants are inserted lazily the first
time a user's balance is consulted in a calendar month — no background job
required. Top-up purchases credit the ledger via the Stripe webhook.

Tier monthly grants (1 credit ≈ $0.01 retail):
  trial:  0
  bronze: 2,000
  silver: 10,000
  gold:   50,000

Pricing for media generation lives in services/media.py MEDIA_MODEL_CATALOG.
"""
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import CreditLedger, TierConfig, TopupPackConfig, User, log_action

# Final-fallback grant amounts if a tier row is missing or has no
# monthly_credit_grant set.  Should only kick in for legacy installs
# pre-dating the DB-backed tier display fields; the seeder populates
# real values into tier_configs on first boot.
_FALLBACK_GRANT_BY_TIER = {
    "trial":  0,
    "bronze": 2_000,
    "silver": 10_000,
    "gold":   50_000,
}


def monthly_grant_for_tier(db: Session, tier: str) -> int:
    """Look up the configured monthly credit grant for a tier from the DB.
    Falls back to the hardcoded default if the row is missing / unconfigured."""
    row = db.query(TierConfig).filter(TierConfig.tier == tier).first()
    if row and row.monthly_credit_grant:
        return int(row.monthly_credit_grant)
    return _FALLBACK_GRANT_BY_TIER.get(tier, 0)


def list_topup_packs(db: Session) -> list[dict]:
    """All active credit packs, sorted by sort_order then label.
    Shape matches the legacy hardcoded list so call sites don't have to change."""
    rows = (
        db.query(TopupPackConfig)
        .filter(TopupPackConfig.is_active == True)  # noqa: E712
        .order_by(TopupPackConfig.sort_order.asc(), TopupPackConfig.label.asc())
        .all()
    )
    return [{
        "key": r.key,
        "label": r.label,
        "credits": r.credits,
        "price_cents": r.price_cents,
    } for r in rows]


def get_topup_pack(db: Session, pack_key: str) -> Optional[dict]:
    """Single pack by key — used by the checkout endpoint."""
    r = (
        db.query(TopupPackConfig)
        .filter(TopupPackConfig.key == pack_key, TopupPackConfig.is_active == True)  # noqa: E712
        .first()
    )
    if not r:
        return None
    return {"key": r.key, "label": r.label, "credits": r.credits, "price_cents": r.price_cents}


def _current_month_tag() -> str:
    now = datetime.utcnow()
    return f"{now.year:04d}-{now.month:02d}"


def _ensure_monthly_grant(db: Session, user: User) -> None:
    """Lazy-insert the user's tier monthly grant if not already granted this month.
    Staff also get a grant (admins shouldn't be blocked from testing media gen)."""
    month = _current_month_tag()
    already = db.query(CreditLedger).filter(
        CreditLedger.user_id == user.id,
        CreditLedger.granted_for_month == month,
        CreditLedger.reason.like("monthly_grant_%"),
    ).first()
    if already:
        return

    # Staff bypass: give them gold-tier grant so they can fully test.
    tier_key = user.tier if not user.is_staff() else "gold"
    amount = monthly_grant_for_tier(db, tier_key)
    if amount <= 0:
        return

    db.add(CreditLedger(
        user_id=user.id,
        delta=amount,
        reason=f"monthly_grant_{tier_key}",
        granted_for_month=month,
        detail=f"Monthly grant for {tier_key} tier ({month})",
    ))
    db.commit()


def balance(db: Session, user: User) -> int:
    """Total available credits for `user`. Triggers a monthly grant on first call this month."""
    _ensure_monthly_grant(db, user)
    total = db.query(func.coalesce(func.sum(CreditLedger.delta), 0)).filter(
        CreditLedger.user_id == user.id,
    ).scalar()
    return int(total or 0)


def spend(db: Session, user: User, amount: int, reason: str,
          media_job_id: Optional[int] = None, detail: Optional[str] = None) -> int:
    """Atomically spend `amount` credits. Raises 402 if insufficient.

    Returns the new balance.
    """
    if amount <= 0:
        raise ValueError("Spend amount must be positive")

    current = balance(db, user)
    if current < amount:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient credits: you have {current}, this costs {amount}. "
                   f"Top up at /billing.",
        )

    db.add(CreditLedger(
        user_id=user.id,
        delta=-amount,
        reason=reason,
        media_job_id=media_job_id,
        detail=detail,
    ))
    db.commit()
    log_action(db, user, "CREDIT_SPEND", "CreditLedger",
               detail=f"-{amount} ({reason}) media_job={media_job_id}")
    return current - amount


def grant(db: Session, user: User, amount: int, reason: str,
          stripe_session_id: Optional[str] = None, detail: Optional[str] = None) -> int:
    """Add credits — for top-up purchases, refunds, admin adjustments."""
    if amount <= 0:
        raise ValueError("Grant amount must be positive")
    db.add(CreditLedger(
        user_id=user.id,
        delta=amount,
        reason=reason,
        stripe_session_id=stripe_session_id,
        detail=detail,
    ))
    db.commit()
    log_action(db, user, "CREDIT_GRANT", "CreditLedger",
               detail=f"+{amount} ({reason})")
    return balance(db, user)


def recent_history(db: Session, user: User, limit: int = 25) -> list:
    rows = db.query(CreditLedger).filter(
        CreditLedger.user_id == user.id,
    ).order_by(CreditLedger.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "delta": r.delta,
            "reason": r.reason,
            "detail": r.detail,
            "media_job_id": r.media_job_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
