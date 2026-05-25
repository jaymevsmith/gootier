"""Per-tier quota enforcement.

Quotas live in TierConfig.quotas_json. Staff (admin/tech/strategist) bypass
all checks. `marketing` role bypasses tier checks but is still subject to
quotas — that's a small simplification vs. ECOSYSTEM.md; revisit if needed.
"""
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import ActionLog, EmailBlast, SocialConnection, SocialPost, TierConfig, User

# Keys that are aggregate over the calendar month
_MONTH_KEYS = {"posts_per_month", "blasts_per_month", "ai_generations_per_month"}


def _month_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


def get_quota(db: Session, tier: str, key: str) -> Optional[int]:
    """Returns the configured quota for (tier, key), or None if not configured."""
    cfg = db.query(TierConfig).filter(TierConfig.tier == tier).first()
    if not cfg:
        return None
    return cfg.quotas_dict().get(key)


def current_usage(db: Session, user: User, key: str) -> int:
    if key == "social_connections":
        return db.query(SocialConnection).filter(
            SocialConnection.user_id == user.id,
            SocialConnection.is_active == True,  # noqa: E712
        ).count()

    if key == "posts_per_month":
        return db.query(SocialPost).filter(
            SocialPost.user_id == user.id,
            SocialPost.created_at >= _month_start(),
            SocialPost.status != "cancelled",
        ).count()

    if key == "blasts_per_month":
        return db.query(EmailBlast).filter(
            EmailBlast.user_id == user.id,
            EmailBlast.created_at >= _month_start(),
            EmailBlast.status != "cancelled",
        ).count()

    if key == "ai_generations_per_month":
        return db.query(ActionLog).filter(
            ActionLog.user_id == user.id,
            ActionLog.action == "AI_GENERATE",
            ActionLog.created_at >= _month_start(),
        ).count()

    return 0


def remaining(db: Session, user: User, key: str) -> int:
    """Returns remaining quota. -1 means unlimited (staff bypass)."""
    if user.is_staff():
        return -1
    quota = get_quota(db, user.tier, key)
    if quota is None:
        return -1
    return max(0, quota - current_usage(db, user, key))


def check_and_raise(db: Session, user: User, key: str, increment: int = 1,
                     upgrade_hint: bool = True) -> None:
    """Raise 403 with an upgrade-nudge detail if user would exceed quota."""
    if user.is_staff():
        return
    quota = get_quota(db, user.tier, key)
    if quota is None:
        return
    used = current_usage(db, user, key)
    if used + increment > quota:
        label = _readable(key)
        msg = f"You've hit your {user.tier} plan's {label} limit ({used}/{quota})."
        if upgrade_hint:
            msg += " Upgrade your plan to keep going."
        raise HTTPException(status_code=403, detail=msg)


def check_per_call(db: Session, user: User, key: str, value: int) -> None:
    """Per-call ceiling (not aggregate over month). Example: blast_recipients."""
    if user.is_staff():
        return
    cap = get_quota(db, user.tier, key)
    if cap is None:
        return
    if value > cap:
        label = _readable(key)
        raise HTTPException(
            status_code=403,
            detail=f"{label} per blast on the {user.tier} plan is capped at {cap} (you tried {value}). "
                   f"Upgrade your plan to send larger blasts.",
        )


def _readable(key: str) -> str:
    return {
        "social_connections": "connected accounts",
        "posts_per_month": "posts-per-month",
        "blasts_per_month": "email-blasts-per-month",
        "ai_generations_per_month": "AI generations-per-month",
        "blast_recipients": "recipients",
    }.get(key, key)


def usage_summary(db: Session, user: User) -> dict:
    """Snapshot for the dashboard usage panel."""
    keys = ["social_connections", "posts_per_month", "blasts_per_month", "ai_generations_per_month"]
    out = {}
    for k in keys:
        quota = get_quota(db, user.tier, k)
        out[k] = {
            "used": current_usage(db, user, k),
            "quota": quota if quota is not None else 0,
            "unlimited": user.is_staff(),
            "label": _readable(k),
        }
    return out
