"""routes/internal_routes.py

POST /internal/handoff -- resolves a Backoffice customer into a Gootier
session, keyed by email (person-shaped, unlike Cloud Storage's domain-keyed
org resolution). See
docs/superpowers/specs/2026-09-01-gootier-connected-app-design.md in the
jhome-backoffice repo for the full requirement list this implements.
"""
import logging
import re
import secrets
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import _load_permissions, hash_password
from database import get_db
from models import EmailBlast, HandoffToken, SocialConnection, SocialPost, User, log_action
from routes.oauth_routes import _unique_username_from_email
from services.ai_generator import generate_campaign
from services.env_config import get_env
from services.email_utils import send_blast_email
from services.handoff import generate_token, hash_token, default_expiry
from services.quotas import check_and_raise, check_per_call
from services.social_publish import publish_to_connections
from services import token_wallet

log = logging.getLogger("gootier.internal_handoff")

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class HandoffRequest(BaseModel):
    email: str
    name: str | None = None
    jhome_sub: str | None = None
    # The Backoffice's assertion that IT has proof of this address. Defaults
    # False so a caller that omits it fails closed -- refused rather than
    # silently vouching for an address nobody verified. Only gates binding an
    # EXISTING account by email (see the check in handoff()); creating a brand
    # new account is not a bind, so it stays reachable.
    email_verified: bool = False


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    expected = get_env("GOOTIER_INTERNAL_KEY", "")
    # Fail CLOSED on an unset key. Compare as bytes, not str:
    # secrets.compare_digest raises TypeError on non-ASCII str operands, and
    # Starlette decodes headers as latin-1, so any byte >= 0x80 reaches here.
    if not expected or not secrets.compare_digest(
        x_internal_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid internal key")


def require_mcp_internal_key(x_internal_key: str = Header(default="")) -> None:
    expected = get_env("GOOTIER_MCP_INTERNAL_KEY", "")
    if not expected or not secrets.compare_digest(
        x_internal_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid internal key")


def _create_user(db: Session, email: str, jhome_sub: str | None, name: str | None) -> User:
    """Find-or-create with a bounded retry for the rare concurrent-username
    race: two handoffs for different brand-new emails that happen to derive
    the same base username, racing on the same candidate slot."""
    for attempt in range(3):
        username = _unique_username_from_email(db, email)
        user = User(
            username=username,
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            role="client",
            tier="trial",
            is_active=True,
            is_verified=True,
            nickname=name or username,
            jhome_sub=jhome_sub,
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
            log_action(db, user, "SIGNUP", "User", str(user.id),
                       detail="Backoffice handoff -- new account")
            return user
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise HTTPException(status_code=500, detail="could not allocate a username")
    raise HTTPException(status_code=500, detail="could not allocate a username")


def resolve_or_create_gootier_user(
    db: Session, *, jhome_sub: str | None, email: str, email_verified: bool,
    name: str | None = None,
) -> User:
    """Find-or-create a Gootier user by email, mirroring handoff()'s own
    refusal logic exactly -- every one of the 5 /internal/mcp/* routes calls
    this directly (not only ensure-account), so the system works correctly
    even if a customer's AI client never calls ensure-account first,
    matching MidCanvas's generate_image_for_mcp precedent.

    `email` must already be normalized (.strip().lower()) by the caller.
    """
    matches = db.query(User).filter(func.lower(User.email) == email).order_by(User.id).all()
    if len(matches) > 1:
        log.warning("mcp identity refused: %d case-variant accounts for email %s", len(matches), email)
        raise HTTPException(status_code=409, detail={"error": "ambiguous_identity"})
    user = matches[0] if matches else None

    if user is not None and not user.is_active:
        log.warning("mcp identity refused: user %s is deactivated", user.id)
        raise HTTPException(status_code=403, detail={"error": "account_inactive"})

    if user is not None and not email_verified:
        log.warning("mcp identity refused: caller did not assert email_verified for user %s", user.id)
        raise HTTPException(status_code=409, detail={"error": "unverified_caller_email"})

    if user is None and jhome_sub:
        existing_sub_holder = db.query(User).filter(User.jhome_sub == jhome_sub).first()
        if existing_sub_holder is not None:
            log.warning(
                "mcp identity refused: jhome_sub %s already belongs to a different user (%s), "
                "but the request's email does not match that user",
                jhome_sub, existing_sub_holder.id,
            )
            raise HTTPException(status_code=409, detail={"error": "linked_elsewhere"})

    if user is None:
        user = _create_user(db, email, jhome_sub, name)
    elif jhome_sub and not user.jhome_sub:
        # NOTE: deliberately not committed here. A caller-side refusal that
        # fires later in this function (e.g. the admin check below) must
        # leave zero DB side effects -- see
        # test_admin_jhome_sub_adoption_does_not_persist_on_refusal in
        # tests/test_internal_handoff.py. The caller (handoff() or an
        # /internal/mcp/* route) is responsible for committing once identity
        # resolution has fully succeeded.
        user.jhome_sub = jhome_sub
    elif jhome_sub and user.jhome_sub and user.jhome_sub != jhome_sub:
        log.warning(
            "mcp identity refused: user %s carried jhome_sub %s but it already has %s",
            user.id, jhome_sub, user.jhome_sub,
        )
        raise HTTPException(status_code=409, detail={"error": "linked_elsewhere"})

    if user.has_role("admin"):
        log.warning("mcp identity refused: user %s has platform admin access", user.id)
        raise HTTPException(status_code=403, detail={"error": "admin_account_not_supported"})

    if user.jhome_sub:
        try:
            token_wallet.link_wallet_to_customer(db, user)
        except Exception:  # noqa: BLE001 -- must never fail an identity resolution on this
            log.exception("could not link wallet for user %s", user.id)

    return user


class _McpIdentityRequest(BaseModel):
    jhome_sub: str | None = None
    email: str
    email_verified: bool = False


def _resolve_mcp_identity(db: Session, req: "_McpIdentityRequest") -> User:
    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail={"error": "invalid_email"})
    user = resolve_or_create_gootier_user(
        db, jhome_sub=req.jhome_sub, email=email,
        email_verified=req.email_verified, name=None,
    )
    # See Task 1's note: resolve_or_create_gootier_user does NOT commit a
    # jhome_sub binding itself. By the time this call returns successfully,
    # every identity-related refusal has already passed -- a route-level
    # permission or quota refusal AFTER this point is orthogonal to identity
    # and must not roll the binding back. So this is the right place to
    # commit it, once, for all 5 routes.
    db.commit()
    _load_permissions(db, user)
    return user


def _raise_quota_error(exc: HTTPException, code: str) -> None:
    """Re-raise a plain-string-detail HTTPException from services/quotas.py
    as this router's own structured shape, preserving the original
    human-readable string as `message` so customer-facing copy can still
    reflect Gootier's real limits without the MCP tool layer string-matching
    UI prose."""
    raise HTTPException(status_code=exc.status_code,
                        detail={"error": code, "message": exc.detail}) from exc


class EnsureAccountRequest(_McpIdentityRequest):
    pass


@router.post("/internal/mcp/ensure-account", dependencies=[Depends(require_mcp_internal_key)])
def mcp_ensure_account(req: EnsureAccountRequest, response: Response,
                       db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)
    return {"user_id": user.id, "tier": user.tier}


class SocialConnectionsRequest(_McpIdentityRequest):
    pass


@router.post("/internal/mcp/social-connections", dependencies=[Depends(require_mcp_internal_key)])
def mcp_social_connections(req: SocialConnectionsRequest, response: Response,
                           db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)
    conns = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id,
        SocialConnection.is_active == True,  # noqa: E712
    ).all()
    return {"connections": [
        {"id": c.id, "platform": c.platform, "display_name": c.account_name}
        for c in conns
    ]}


class SchedulePostRequest(_McpIdentityRequest):
    content: str = Field(..., min_length=1, max_length=5000)
    connection_ids: List[int]
    image_url: str | None = None
    video_url: str | None = None
    link_url: str | None = None
    scheduled_at: datetime | None = None


@router.post("/internal/mcp/schedule-post", dependencies=[Depends(require_mcp_internal_key)])
async def mcp_schedule_post(req: SchedulePostRequest, response: Response,
                            db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)

    if not req.connection_ids:
        # Without this, an empty list passes ownership vacuously (0 == 0)
        # and, when unscheduled, publish_to_connections([]) returns {} --
        # successes (0) == len(owned) (0) reads as "published" with zero
        # connections actually posted to. A false success is worse than a
        # refusal here.
        raise HTTPException(status_code=400,
                            detail={"error": "invalid_connections",
                                    "message": "connection_ids must not be empty"})

    owned = db.query(SocialConnection).filter(
        SocialConnection.id.in_(req.connection_ids),
        SocialConnection.user_id == user.id,
        SocialConnection.is_active == True,  # noqa: E712
    ).all()
    if len(owned) != len(req.connection_ids):
        raise HTTPException(status_code=400,
                            detail={"error": "invalid_connections",
                                    "message": "One or more connections invalid"})

    if not user.perm("marketing.social_post"):
        raise HTTPException(status_code=403,
                            detail={"error": "plan_upgrade_required",
                                    "message": "Requires permission: marketing.social_post"})
    try:
        check_and_raise(db, user, "posts_per_month")
    except HTTPException as exc:
        _raise_quota_error(exc, "posts_quota_exceeded")

    post = SocialPost(
        user_id=user.id,
        content=req.content,
        image_url=req.image_url,
        video_url=req.video_url,
        link_url=req.link_url,
        connection_ids=",".join(str(c.id) for c in owned),
        scheduled_at=req.scheduled_at,
        status="pending",
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    if not req.scheduled_at:
        results = await publish_to_connections(
            owned, post.content, link_url=post.link_url,
            image_url=post.image_url, video_url=post.video_url,
        )
        successes = sum(1 for r in results.values() if r.get("success"))
        post.status = ("published" if successes == len(owned)
                       else "partial" if successes else "failed")
        post.published_at = datetime.utcnow()
        import json as _json
        post.publish_results = _json.dumps({str(k): v for k, v in results.items()})
        db.commit()

    log_action(db, user, "CREATE", "SocialPost", str(post.id), detail="via MCP")
    return {"id": post.id, "status": post.status}


class ScheduleEmailBlastRequest(_McpIdentityRequest):
    subject: str = Field(..., min_length=1, max_length=200)
    body_html: str = Field(..., min_length=1)
    recipients: List[str]
    scheduled_at: datetime | None = None


@router.post("/internal/mcp/schedule-email-blast", dependencies=[Depends(require_mcp_internal_key)])
def mcp_schedule_email_blast(req: ScheduleEmailBlastRequest, response: Response,
                             db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)

    if not user.perm("marketing.email_blast"):
        raise HTTPException(status_code=403,
                            detail={"error": "plan_upgrade_required",
                                    "message": "Requires permission: marketing.email_blast"})
    try:
        check_and_raise(db, user, "blasts_per_month")
    except HTTPException as exc:
        _raise_quota_error(exc, "blasts_quota_exceeded")
    try:
        check_per_call(db, user, "blast_recipients", len(req.recipients))
    except HTTPException as exc:
        _raise_quota_error(exc, "recipient_cap_exceeded")

    blast = EmailBlast(
        user_id=user.id,
        subject=req.subject,
        body_html=req.body_html,
        recipient_list="\n".join(req.recipients),
        recipient_count=len(req.recipients),
        scheduled_at=req.scheduled_at,
        status="pending",
    )
    db.add(blast)
    db.commit()
    db.refresh(blast)

    if not req.scheduled_at:
        sent, failed = send_blast_email(blast.subject, blast.body_html, req.recipients)
        blast.sent_count = sent
        blast.failed_count = failed
        blast.status = ("sent" if failed == 0 and sent > 0
                        else "partial" if sent > 0 else "failed")
        db.commit()

    log_action(db, user, "CREATE", "EmailBlast", str(blast.id), detail="via MCP")
    return {"id": blast.id, "status": blast.status}


class DraftCampaignRequest(_McpIdentityRequest):
    plan: str = Field(..., min_length=10)
    schedule: str = ""
    count: int = Field(5, ge=1, le=20)
    channels: List[str] = ["social_post", "email_blast"]


@router.post("/internal/mcp/draft-campaign", dependencies=[Depends(require_mcp_internal_key)])
def mcp_draft_campaign(req: DraftCampaignRequest, response: Response,
                       db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)

    if not user.perm("marketing.ai_generate"):
        raise HTTPException(status_code=403,
                            detail={"error": "plan_upgrade_required",
                                    "message": "Requires permission: marketing.ai_generate"})
    try:
        check_and_raise(db, user, "ai_generations_per_month")
    except HTTPException as exc:
        _raise_quota_error(exc, "ai_generations_quota_exceeded")

    try:
        result = generate_campaign(plan=req.plan, schedule=req.schedule,
                                   count=req.count, channels=req.channels)
    except Exception:
        # Never relay the raw exception text: a bug inside generate_campaign
        # (a malformed-response KeyError, an SDK error) would otherwise
        # surface verbatim through a 502 an MCP client renders to the
        # customer, and would read as generic upstream-AI flakiness instead
        # of the programming bug it might actually be. The real exception
        # goes to the log, where it belongs.
        log.exception("draft-campaign failed for user %s", user.id)
        raise HTTPException(status_code=502,
                            detail={"error": "ai_generation_failed",
                                    "message": "AI generation failed, please try again."})

    log_action(db, user, "AI_GENERATE", "Campaign",
              detail=f"Generated {len((result or {}).get('items', []))} item(s) via MCP")
    return result


@router.post("/internal/handoff", dependencies=[Depends(require_internal_key)])
def handoff(req: HandoffRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"

    app_url = get_env("APP_URL", "").rstrip("/")
    if not app_url:
        raise HTTPException(status_code=500, detail="APP_URL is not configured")

    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid email")

    user = resolve_or_create_gootier_user(
        db, jhome_sub=req.jhome_sub, email=email,
        email_verified=req.email_verified, name=req.name,
    )

    token = generate_token()
    db.add(HandoffToken(token_hash=hash_token(token), user_id=user.id,
                         expires_at=default_expiry()))
    db.commit()

    log.info("handoff minted token for user %s", user.id)
    log_action(db, user, "BACKOFFICE_HANDOFF", "User", str(user.id))

    return {"consume_url": f"{app_url}/sso/consume?token={token}"}
