import calendar as pycal
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import get_current_user_optional
from database import get_db
from models import EmailBlast, MediaJob, SocialConnection, SocialPost, User
from services.credits import balance as credits_balance
from services.quotas import usage_summary

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _ctx(user: User, **extra) -> dict:
    return {"user": user, **extra}


@router.get("/")
async def root(user: User = Depends(get_current_user_optional)):
    return RedirectResponse(url="/dashboard" if user else "/login", status_code=303)


@router.get("/dashboard")
async def dashboard(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    connections = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id, SocialConnection.is_active == True,  # noqa: E712
    ).count()
    pending_posts = db.query(SocialPost).filter(
        SocialPost.user_id == user.id, SocialPost.status == "pending",
    ).count()
    pending_blasts = db.query(EmailBlast).filter(
        EmailBlast.user_id == user.id, EmailBlast.status == "pending",
    ).count()
    from datetime import timedelta
    from services.analytics import summarise_metrics
    usage = usage_summary(db, user)
    media_in_flight = db.query(MediaJob).filter(
        MediaJob.user_id == user.id,
        MediaJob.status.in_(("queued", "running")),
    ).order_by(MediaJob.created_at.desc()).limit(8).all()
    # Performance from last 30 days of published posts that have analytics.
    cutoff = datetime.utcnow() - timedelta(days=30)
    analytics_posts = db.query(SocialPost).filter(
        SocialPost.user_id == user.id,
        SocialPost.status.in_(("published", "partial")),
        SocialPost.created_at >= cutoff,
    ).all()
    performance = summarise_metrics(analytics_posts)
    recent_media = db.query(MediaJob).filter(
        MediaJob.user_id == user.id,
        MediaJob.status == "done",
        MediaJob.result_url != None,  # noqa: E711
    ).order_by(MediaJob.completed_at.desc().nullslast()).limit(8).all()
    credit_balance = credits_balance(db, user)
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        user,
        connections=connections,
        pending_posts=pending_posts,
        pending_blasts=pending_blasts,
        usage=usage,
        media_in_flight=media_in_flight,
        recent_media=recent_media,
        credit_balance=credit_balance,
        performance=performance,
    ))


@router.get("/connections")
async def connections_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    items = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id, SocialConnection.is_active == True,  # noqa: E712
    ).all()
    # Tell the template which OAuth flows are configured so we can grey out
    # the rest (with a hint pointing the admin at /admin/env).
    from services.env_config import get_env
    platform_status = {
        "meta_configured":     bool(get_env("META_APP_ID")),
        "twitter_configured":  bool(get_env("X_CLIENT_ID")),
        "linkedin_configured": bool(get_env("LINKEDIN_CLIENT_ID")),
        "tiktok_configured":   bool(get_env("TIKTOK_CLIENT_KEY")),
    }
    return templates.TemplateResponse(request, "connections.html", _ctx(
        user, connections=items, platform_status=platform_status,
    ))


@router.get("/compose")
async def compose_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    items = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id, SocialConnection.is_active == True,  # noqa: E712
    ).all()
    return templates.TemplateResponse(request, "compose.html", _ctx(user, connections=items))


@router.get("/ai-builder")
async def ai_builder_page(request: Request, user: User = Depends(get_current_user_optional)):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "ai_builder.html", _ctx(user))


@router.get("/scheduled")
async def scheduled_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    posts = db.query(SocialPost).filter(
        SocialPost.user_id == user.id, SocialPost.status == "pending",
    ).order_by(SocialPost.scheduled_at.asc()).all()
    blasts = db.query(EmailBlast).filter(
        EmailBlast.user_id == user.id, EmailBlast.status == "pending",
    ).order_by(EmailBlast.scheduled_at.asc()).all()
    return templates.TemplateResponse(request, "scheduled.html", _ctx(
        user, posts=posts, blasts=blasts,
    ))


@router.get("/blasts")
async def blasts_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    history = db.query(EmailBlast).filter(
        EmailBlast.user_id == user.id,
    ).order_by(EmailBlast.created_at.desc()).limit(50).all()
    return templates.TemplateResponse(request, "blasts.html", _ctx(user, history=history))


@router.get("/profile")
async def profile_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    connections_count = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id, SocialConnection.is_active == True,  # noqa: E712
    ).count()
    return templates.TemplateResponse(request, "profile.html", _ctx(
        user, connections_count=connections_count,
    ))


@router.get("/calendar")
async def calendar_page(
    request: Request,
    month: str = "",
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    today = datetime.utcnow().date()
    anchor = today.replace(day=1)
    if month and re.match(r"^\d{4}-\d{2}$", month):
        try:
            anchor = date(int(month[:4]), int(month[5:7]), 1)
        except ValueError:
            pass

    prev_anchor = (anchor - timedelta(days=1)).replace(day=1)
    if anchor.month == 12:
        next_anchor = date(anchor.year + 1, 1, 1)
    else:
        next_anchor = date(anchor.year, anchor.month + 1, 1)

    cal = pycal.Calendar(firstweekday=6)  # Sunday-first
    weeks = cal.monthdatescalendar(anchor.year, anchor.month)
    range_start = datetime.combine(weeks[0][0], datetime.min.time())
    range_end = datetime.combine(weeks[-1][-1], datetime.max.time())

    posts = db.query(SocialPost).filter(
        SocialPost.user_id == user.id,
        SocialPost.scheduled_at != None,  # noqa: E711
        SocialPost.scheduled_at >= range_start,
        SocialPost.scheduled_at <= range_end,
    ).all()
    blasts = db.query(EmailBlast).filter(
        EmailBlast.user_id == user.id,
        EmailBlast.scheduled_at != None,  # noqa: E711
        EmailBlast.scheduled_at >= range_start,
        EmailBlast.scheduled_at <= range_end,
    ).all()

    items_by_date: dict = {}
    for p in posts:
        items_by_date.setdefault(p.scheduled_at.date(), []).append({
            "kind": "social_post",
            "id": p.id,
            "time": p.scheduled_at.strftime("%H:%M"),
            "iso": p.scheduled_at.strftime("%Y-%m-%d %H:%M UTC"),
            "preview": (p.content or "")[:60],
            "content": p.content or "",
            "subject": None,
            "status": p.status,
            "ai": p.ai_generated,
        })
    for b in blasts:
        items_by_date.setdefault(b.scheduled_at.date(), []).append({
            "kind": "email_blast",
            "id": b.id,
            "time": b.scheduled_at.strftime("%H:%M"),
            "iso": b.scheduled_at.strftime("%Y-%m-%d %H:%M UTC"),
            "preview": b.subject,
            "content": b.body_html or "",
            "subject": b.subject,
            "status": b.status,
            "ai": b.ai_generated,
            "recipients": b.recipient_count,
        })
    for d in items_by_date:
        items_by_date[d].sort(key=lambda x: x["time"])

    return templates.TemplateResponse(request, "calendar.html", _ctx(
        user,
        anchor=anchor,
        weeks=weeks,
        items_by_date=items_by_date,
        today=today,
        prev_anchor=prev_anchor.strftime("%Y-%m"),
        next_anchor=next_anchor.strftime("%Y-%m"),
        month_label=anchor.strftime("%B %Y"),
        total_items=sum(len(v) for v in items_by_date.values()),
    ))
