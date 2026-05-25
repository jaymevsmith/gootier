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
from models import ActionLog, EmailBlast, EnvConfig, SocialConnection, SocialPost, User, log_action
from services.env_config import list_for_admin, set_env

router = APIRouter()
templates = Jinja2Templates(directory="templates")

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
