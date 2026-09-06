from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from auth import get_current_user, get_current_user_optional
from database import get_db
from display import install_filters
from models import (
    ActionLog, EmailBlast, EnvConfig, SocialConnection, SocialPost,
    TierConfig, User, log_action,
)
from services.env_config import list_for_admin, set_env

router = APIRouter()
templates = install_filters(Jinja2Templates(directory="templates"))

VALID_ROLES = ["admin", "tech", "strategist", "marketing", "client"]
VALID_TIERS = ["trial", "bronze", "silver", "gold"]


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _ctx(user: User, **extra) -> dict:
    return {"user": user, **extra}


# --------------------------------------------------------------------------- #
# Web pages
# --------------------------------------------------------------------------- #

@router.get("/admin")
async def admin_root(user: User = Depends(get_current_user_optional)):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/users")
async def admin_users_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.is_active == True).count()  # noqa: E712
    paying = db.query(User).filter(User.tier.in_(["bronze", "silver", "gold"])).count()
    trial = db.query(User).filter(User.tier == "trial").count()

    return templates.TemplateResponse(request, "admin_users.html", _ctx(
        user,
        total_users=total_users,
        active_users=active_users,
        paying_users=paying,
        trial_users=trial,
        roles=VALID_ROLES,
        tiers=VALID_TIERS,
    ))


@router.get("/admin/env")
async def admin_env_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return templates.TemplateResponse(request, "admin_env.html", _ctx(user))


@router.get("/admin/plans")
async def admin_plans_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
):
    """Admin editor for the billing page: subscription tiers.
    Everything visible to end users on /billing is editable here."""
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return templates.TemplateResponse(request, "admin_plans.html", _ctx(user))


@router.get("/admin/logs")
async def admin_logs_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return templates.TemplateResponse(request, "admin_logs.html", _ctx(user))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class AdminUserUpdate(BaseModel):
    role: Optional[str] = None
    tier: Optional[str] = None
    is_active: Optional[bool] = None
    is_verified: Optional[bool] = None
    subscribed_until: Optional[datetime] = None


@router.get("/api/admin/users")
async def list_users(
    q: str = "",
    role: str = "",
    tier: str = "",
    status: str = "",  # "active" | "disabled" | ""
    limit: int = 50,
    offset: int = 0,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(User)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(or_(
            func.lower(User.username).like(like),
            func.lower(User.email).like(like),
            func.lower(func.coalesce(User.nickname, "")).like(like),
        ))
    if role:
        query = query.filter(User.role.like(f"%{role}%"))
    if tier:
        query = query.filter(User.tier == tier)
    if status == "active":
        query = query.filter(User.is_active == True)  # noqa: E712
    elif status == "disabled":
        query = query.filter(User.is_active == False)  # noqa: E712

    total = query.count()
    rows = (
        query.order_by(User.created_at.desc())
        .offset(max(0, offset)).limit(max(1, min(200, limit)))
        .all()
    )

    return {
        "total": total,
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "nickname": u.nickname,
                "email": u.email,
                "role": u.role,
                "tier": u.tier,
                "is_active": u.is_active,
                "is_verified": u.is_verified,
                "stripe_customer_id": u.stripe_customer_id,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "subscribed_until": u.subscribed_until.isoformat() if u.subscribed_until else None,
            }
            for u in rows
        ],
    }


@router.get("/api/admin/users/{user_id}")
async def get_user_detail(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": target.id,
        "username": target.username,
        "nickname": target.nickname,
        "email": target.email,
        "role": target.role,
        "tier": target.tier,
        "is_active": target.is_active,
        "is_verified": target.is_verified,
        "stripe_customer_id": target.stripe_customer_id,
        "subscribed_until": target.subscribed_until.isoformat() if target.subscribed_until else None,
        "created_at": target.created_at.isoformat() if target.created_at else None,
        "counts": {
            "connections": db.query(SocialConnection).filter(SocialConnection.user_id == target.id, SocialConnection.is_active == True).count(),  # noqa: E712
            "posts": db.query(SocialPost).filter(SocialPost.user_id == target.id).count(),
            "blasts": db.query(EmailBlast).filter(EmailBlast.user_id == target.id).count(),
        },
    }


@router.patch("/api/admin/users/{user_id}")
async def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Don't let an admin lock themselves out.
    if target.id == admin.id:
        if payload.role is not None and "admin" not in _split(payload.role):
            raise HTTPException(status_code=400, detail="You can't remove your own admin role.")
        if payload.is_active is False:
            raise HTTPException(status_code=400, detail="You can't disable your own account.")

    changed: List[str] = []

    if payload.role is not None:
        roles = _split(payload.role)
        for r in roles:
            if r not in VALID_ROLES:
                raise HTTPException(status_code=400, detail=f"Unknown role: {r}")
        target.role = ",".join(roles) if roles else "client"
        changed.append(f"role={target.role}")

    if payload.tier is not None:
        if payload.tier not in VALID_TIERS:
            raise HTTPException(status_code=400, detail=f"Unknown tier: {payload.tier}")
        target.tier = payload.tier
        changed.append(f"tier={target.tier}")

    if payload.is_active is not None:
        target.is_active = bool(payload.is_active)
        changed.append(f"is_active={target.is_active}")

    if payload.is_verified is not None:
        target.is_verified = bool(payload.is_verified)
        changed.append(f"is_verified={target.is_verified}")

    if payload.subscribed_until is not None:
        target.subscribed_until = payload.subscribed_until
        changed.append(f"subscribed_until={target.subscribed_until.isoformat()}")

    if not changed:
        return {"ok": True, "changed": []}

    db.commit()
    log_action(db, admin, "ADMIN_UPDATE", "User", str(target.id),
               detail=f"@{target.username}: " + "; ".join(changed))
    return {"ok": True, "changed": changed}


class EnvUpdate(BaseModel):
    value: Optional[str] = None


@router.get("/api/admin/env")
async def admin_env_list(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {"items": list_for_admin(db)}


@router.patch("/api/admin/env/{key}")
async def admin_env_update(
    key: str,
    payload: EnvUpdate,
    admin: User = Depends(require_admin),
):
    try:
        snapshot = set_env(key, payload.value, admin)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {
        "ok": True,
        "key": snapshot["key"],
        "cleared": snapshot["cleared"],
        "updated_at": snapshot["updated_at"].isoformat() if snapshot["updated_at"] else None,
    }


@router.get("/api/admin/logs")
async def list_logs(
    user_id: Optional[int] = None,
    action: str = "",
    limit: int = 100,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(ActionLog)
    if user_id:
        q = q.filter(ActionLog.user_id == user_id)
    if action:
        q = q.filter(ActionLog.action.like(f"%{action}%"))
    rows = q.order_by(ActionLog.created_at.desc()).limit(max(1, min(500, limit))).all()
    return {
        "logs": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_name": r.user_name,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


def _split(csv: str) -> List[str]:
    return [s.strip() for s in (csv or "").split(",") if s.strip()]


# --------------------------------------------------------------------------- #
# Plans API — backs /admin/plans
# --------------------------------------------------------------------------- #

class TierQuotas(BaseModel):
    """The four enforced limits in services/quotas.py.  Null = unlimited."""
    social_connections: Optional[int] = None
    posts_per_month: Optional[int] = None
    blasts_per_month: Optional[int] = None
    ai_generations_per_month: Optional[int] = None


class TierUpdate(BaseModel):
    """All fields optional; we only patch the ones the admin sent."""
    display_name: Optional[str] = None
    blurb: Optional[str] = None
    monthly_price_cents: Optional[int] = None
    stripe_price_id: Optional[str] = None
    yearly_stripe_price_id: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None
    features: Optional[List[str]] = None
    quotas: Optional[TierQuotas] = None


_PLANS_VALID_TIERS = ("trial", "bronze", "silver", "gold")


_QUOTA_KEYS = (
    "social_connections", "posts_per_month",
    "blasts_per_month", "ai_generations_per_month",
)


def _serialize_tier(t: TierConfig, *, user_count: int = 0) -> dict:
    quotas = t.quotas_dict() or {}
    return {
        "tier": t.tier,
        "display_name": t.display_name or t.tier.title(),
        "blurb": t.blurb or "",
        "monthly_price_cents": t.monthly_price_cents or 0,
        "monthly_price_display": f"${(t.monthly_price_cents or 0) / 100:.2f}",
        "stripe_price_id": t.stripe_price_id or "",
        "yearly_stripe_price_id": t.yearly_stripe_price_id or "",
        "yearly_price_display": f"${((t.monthly_price_cents or 0) * 10) / 100:.2f}",
        "features": t.features_list(),
        "sort_order": int(t.sort_order or 0),
        "is_active": bool(t.is_active if t.is_active is not None else True),
        # Enforced limits — only render the four keys the quota engine knows
        # about so the UI stays in sync with services/quotas.py.
        "quotas": {k: quotas.get(k) for k in _QUOTA_KEYS},
        # Number of users currently on this tier — admin needs to see who
        # they're about to affect before lowering a quota.
        "user_count": user_count,
    }


@router.get("/api/admin/tiers")
async def list_tiers(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    rows = db.query(TierConfig).order_by(TierConfig.sort_order.asc(), TierConfig.tier.asc()).all()
    # One aggregate query — count active users grouped by tier — so the UI
    # can tell the admin how many real accounts each quota change affects.
    counts_q = (
        db.query(User.tier, func.count(User.id))
        .filter(User.is_active == True)  # noqa: E712
        .group_by(User.tier)
        .all()
    )
    counts = {tier: int(n) for tier, n in counts_q}
    return {"tiers": [_serialize_tier(r, user_count=counts.get(r.tier, 0)) for r in rows]}


@router.patch("/api/admin/tiers/{tier_key}")
async def update_tier(
    tier_key: str,
    payload: TierUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    import json as _json
    if tier_key not in _PLANS_VALID_TIERS:
        raise HTTPException(status_code=404, detail=f"Unknown tier: {tier_key}")
    row = db.query(TierConfig).filter(TierConfig.tier == tier_key).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Tier row missing for {tier_key} — re-run init_db")

    changed: list = []
    if payload.display_name is not None and payload.display_name.strip():
        row.display_name = payload.display_name.strip()[:80]
        changed.append("display_name")
    if payload.blurb is not None:
        row.blurb = payload.blurb.strip()[:280]
        changed.append("blurb")
    if payload.monthly_price_cents is not None:
        row.monthly_price_cents = max(0, int(payload.monthly_price_cents))
        changed.append("monthly_price_cents")
    if payload.stripe_price_id is not None:
        row.stripe_price_id = payload.stripe_price_id.strip()[:120] or None
        changed.append("stripe_price_id")
    if payload.yearly_stripe_price_id is not None:
        row.yearly_stripe_price_id = payload.yearly_stripe_price_id.strip()[:120] or None
        changed.append("yearly_stripe_price_id")
    if payload.sort_order is not None:
        row.sort_order = int(payload.sort_order)
        changed.append("sort_order")
    if payload.is_active is not None:
        row.is_active = bool(payload.is_active)
        changed.append("is_active")
    if payload.features is not None:
        # Cap at 12 bullets, 120 chars each — sane defaults to prevent abuse.
        clean = [str(f).strip()[:120] for f in payload.features if str(f).strip()][:12]
        row.features_json = _json.dumps(clean)
        changed.append("features")

    # Enforced quotas — written to quotas_json so services/quotas.py picks
    # them up on the next request.  null on any key = unlimited for that key.
    if payload.quotas is not None:
        existing_q = row.quotas_dict() or {}
        for k in _QUOTA_KEYS:
            new_val = getattr(payload.quotas, k)
            if new_val is None:
                # Treat -1 / None / blank as "unlimited" — remove the key
                # from quotas_json so services/quotas.py.get_quota returns None.
                existing_q.pop(k, None)
            else:
                existing_q[k] = max(0, int(new_val))
        row.quotas_json = _json.dumps(existing_q)
        changed.append("quotas")

    db.commit()
    log_action(db, admin, "UPDATE", "TierConfig", tier_key, detail=f"changed: {','.join(changed) or 'none'}")
    # Fresh user-count so the UI stays accurate after save (cheap single query).
    user_count = (
        db.query(User)
        .filter(User.tier == tier_key, User.is_active == True)  # noqa: E712
        .count()
    )
    return _serialize_tier(row, user_count=user_count)
