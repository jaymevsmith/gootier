import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import (
    get_current_user, hash_password, require_perm, validate_email,
    validate_password, verify_password,
)
from database import get_db
from models import (
    EmailBlast, SocialConnection, SocialPost, User, log_action,
)
from services.ai_generator import generate_campaign
from services.media import (
    MEDIA_MODEL_CATALOG, build_image_payload, build_video_payload,
    resolve_model, submit_job,
)
from services.quotas import check_and_raise, check_per_call
from services.social_publish import publish_to_connections

logger = logging.getLogger("gootier.api")

router = APIRouter(prefix="/api")


# ------------------------------ Schemas ------------------------------ #
# Pydantic models MUST live above their handlers — annotations are evaluated
# at function-definition time on Python < 3.14.

class SocialPostCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)
    connection_ids: List[int]
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    link_url: Optional[str] = None
    scheduled_at: Optional[datetime] = None


class EmailBlastCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    body_html: str = Field(..., min_length=1)
    recipients: List[str]
    scheduled_at: Optional[datetime] = None


class CampaignGenerateRequest(BaseModel):
    plan: str = Field(..., min_length=10)
    schedule: str = ""
    count: int = Field(5, ge=1, le=20)
    channels: List[str] = ["social_post", "email_blast"]


class CampaignItem(BaseModel):
    kind: str
    content: str
    subject: Optional[str] = None
    link_url: Optional[str] = None
    image_url: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    suggested_asset_kind: Optional[str] = None


class RescheduleRequest(BaseModel):
    scheduled_at: datetime


class ProfileUpdate(BaseModel):
    nickname: Optional[str] = None
    email: Optional[str] = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class MediaScheduleSettings(BaseModel):
    generate_images: bool = False
    generate_videos: bool = False
    image_model_key: Optional[str] = None  # falls back to default in catalog
    video_model_key: Optional[str] = None
    image_asset_id: Optional[int] = None   # default reference for image jobs
    video_asset_id: Optional[int] = None   # default reference for video jobs


class CampaignScheduleRequest(BaseModel):
    items: List[CampaignItem]
    connection_ids: List[int] = []
    recipients: List[str] = []
    media: Optional[MediaScheduleSettings] = None


# ------------------------------ Profile ------------------------------ #

@router.patch("/profile")
async def update_profile(
    payload: ProfileUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    changed = []
    email_changed = False
    if payload.nickname is not None:
        nickname = payload.nickname.strip()
        if len(nickname) > 50:
            raise HTTPException(status_code=400, detail="Display name must be 50 chars or fewer.")
        user.nickname = nickname or None
        changed.append("nickname")

    if payload.email is not None:
        email = payload.email.strip().lower()
        err = validate_email(email)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if email != user.email:
            taken = db.query(User).filter(User.email == email, User.id != user.id).first()
            if taken:
                raise HTTPException(status_code=400, detail="That email is already in use.")
            user.email = email
            user.is_verified = False  # require re-verification of new address
            changed.append("email")
            email_changed = True

    if not changed:
        return {"ok": True, "changed": []}

    db.commit()
    user_id = user.id
    log_action(db, user, "UPDATE", "User", str(user.id),
               detail=f"Profile updated: {', '.join(changed)}")

    result = {"ok": True, "changed": changed}

    if email_changed:
        # The new address is already committed above, so this send is
        # best-effort — the same post-commit shape as signup_submit's tail, and
        # wrapped for the same reason: `_smtp_config()` reads five keys through
        # get_env(), each opening its own DB session, so a database blip here
        # would 500 a profile update that has already been written. The caller
        # is told whether the mail actually went out rather than being left to
        # wait on one that didn't; either way they can resend from /profile.
        from routes.auth_routes import trigger_verification_email, _app_url
        try:
            result["verification_email_sent"] = bool(
                trigger_verification_email(db, user, _app_url(request, db))
            )
        except Exception:
            db.rollback()
            logger.exception("verification email failed after email change: user=%s", user_id)
            result["verification_email_sent"] = False

    return result


@router.post("/profile/verify-email/resend")
async def resend_verification(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.is_verified:
        return {"ok": True, "already_verified": True, "delivered": True}
    from routes.auth_routes import trigger_verification_email, _app_url
    delivered = trigger_verification_email(db, user, _app_url(request, db))
    log_action(db, user, "EMAIL_VERIFY_RESEND", "User", str(user.id),
               detail=f"delivered={delivered}")
    return {"ok": True, "delivered": delivered}


@router.post("/profile/password")
async def change_password(
    payload: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    err = validate_password(payload.new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one.")

    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    log_action(db, user, "PASSWORD_CHANGE", "User", str(user.id))
    return {"ok": True}


# ------------------------------ Social posts ------------------------------ #

@router.get("/social/connections")
async def list_connections(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id, SocialConnection.is_active == True,  # noqa: E712
    ).all()
    return [
        {"id": c.id, "platform": c.platform, "account_name": c.account_name,
         "page_id": c.page_id, "expires_at": c.expires_at}
        for c in items
    ]


@router.delete("/social/connections/{conn_id}")
async def disconnect(
    conn_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conn = db.query(SocialConnection).filter(
        SocialConnection.id == conn_id, SocialConnection.user_id == user.id,
    ).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    conn.is_active = False
    db.commit()
    log_action(db, user, "DELETE", "SocialConnection", str(conn.id))
    return {"ok": True}


@router.post("/social/posts")
async def create_post(
    payload: SocialPostCreate,
    user: User = Depends(require_perm("marketing.social_post")),
    db: Session = Depends(get_db),
):
    owned = db.query(SocialConnection).filter(
        SocialConnection.id.in_(payload.connection_ids),
        SocialConnection.user_id == user.id,
        SocialConnection.is_active == True,  # noqa: E712
    ).all()
    if len(owned) != len(payload.connection_ids):
        raise HTTPException(status_code=400, detail="One or more connections invalid")

    check_and_raise(db, user, "posts_per_month")

    post = SocialPost(
        user_id=user.id,
        content=payload.content,
        image_url=payload.image_url,
        video_url=payload.video_url,
        link_url=payload.link_url,
        connection_ids=",".join(str(c.id) for c in owned),
        scheduled_at=payload.scheduled_at,
        status="pending",
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    if not payload.scheduled_at:
        results = await publish_to_connections(
            owned, post.content,
            link_url=post.link_url,
            image_url=post.image_url,
            video_url=post.video_url,
        )
        successes = sum(1 for r in results.values() if r.get("success"))
        post.status = ("published" if successes == len(owned)
                       else "partial" if successes else "failed")
        post.published_at = datetime.utcnow()
        import json as _json
        post.publish_results = _json.dumps({str(k): v for k, v in results.items()})
        db.commit()

    log_action(db, user, "CREATE", "SocialPost", str(post.id))
    return {"id": post.id, "status": post.status}


@router.patch("/social/posts/{post_id}/reschedule")
async def reschedule_post(
    post_id: int,
    payload: RescheduleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = db.query(SocialPost).filter(
        SocialPost.id == post_id, SocialPost.user_id == user.id,
    ).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending posts can be rescheduled")
    if payload.scheduled_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Cannot reschedule into the past")
    post.scheduled_at = payload.scheduled_at
    db.commit()
    log_action(db, user, "UPDATE", "SocialPost", str(post.id),
               detail=f"Rescheduled to {payload.scheduled_at.isoformat()}")
    return {"ok": True, "scheduled_at": post.scheduled_at}


@router.patch("/email-blasts/{blast_id}/reschedule")
async def reschedule_blast(
    blast_id: int,
    payload: RescheduleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    blast = db.query(EmailBlast).filter(
        EmailBlast.id == blast_id, EmailBlast.user_id == user.id,
    ).first()
    if not blast:
        raise HTTPException(status_code=404, detail="Blast not found")
    if blast.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending blasts can be rescheduled")
    if payload.scheduled_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Cannot reschedule into the past")
    blast.scheduled_at = payload.scheduled_at
    db.commit()
    log_action(db, user, "UPDATE", "EmailBlast", str(blast.id),
               detail=f"Rescheduled to {payload.scheduled_at.isoformat()}")
    return {"ok": True, "scheduled_at": blast.scheduled_at}


@router.delete("/social/posts/{post_id}/cancel")
async def cancel_post(
    post_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = db.query(SocialPost).filter(
        SocialPost.id == post_id, SocialPost.user_id == user.id,
    ).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending posts can be cancelled")
    post.status = "cancelled"
    db.commit()
    return {"ok": True}


# ------------------------------ Email blasts ------------------------------ #

@router.post("/calendar/token/regenerate")
async def regenerate_calendar_token(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rotate the calendar subscription token — old URL stops working."""
    import secrets as _py_secrets
    from services.env_config import get_env
    user.calendar_token = _py_secrets.token_urlsafe(24)
    db.commit()
    log_action(db, user, "CALENDAR_TOKEN_ROTATE", "User", str(user.id))
    base = (get_env("APP_URL", "") or f"{request.url.scheme}://{request.url.netloc}").rstrip("/")
    return {
        "ok": True,
        "ics_url":   f"{base}/calendar/{user.calendar_token}.ics",
        "webcal_url": f"{base.replace('https://', 'webcal://').replace('http://', 'webcal://')}/calendar/{user.calendar_token}.ics",
    }


@router.post("/analytics/refresh/{post_id}")
async def refresh_analytics(
    post_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """On-demand refresh of metrics for a single published post."""
    from services.analytics import fetch_post_metrics
    post = db.query(SocialPost).filter(
        SocialPost.id == post_id, SocialPost.user_id == user.id,
    ).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.status not in ("published", "partial"):
        raise HTTPException(status_code=400, detail="Post hasn't been published yet")
    metrics = await fetch_post_metrics(db, post)
    return {"ok": True, "metrics": metrics, "fetched_at": post.analytics_fetched_at.isoformat() if post.analytics_fetched_at else None}


@router.delete("/email-blasts/{blast_id}/cancel")
async def cancel_blast(
    blast_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    blast = db.query(EmailBlast).filter(
        EmailBlast.id == blast_id, EmailBlast.user_id == user.id,
    ).first()
    if not blast:
        raise HTTPException(status_code=404, detail="Blast not found")
    if blast.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending blasts can be cancelled")
    blast.status = "cancelled"
    db.commit()
    log_action(db, user, "DELETE", "EmailBlast", str(blast.id), detail="Cancelled by user")
    return {"ok": True}


@router.post("/email-blasts/parse-recipients")
async def parse_recipient_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Parse a CSV / TXT / XLSX upload and return clean emails + rejects."""
    from services.recipient_parser import parse_recipients
    from fastapi import UploadFile  # noqa
    try:
        content = await file.read()
        result = parse_recipients(content, file.content_type or "", file.filename or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Couldn't parse file: {e}")
    return {"ok": True, "filename": file.filename, **result}


@router.post("/email-blasts")
async def create_blast(
    payload: EmailBlastCreate,
    user: User = Depends(require_perm("marketing.email_blast")),
    db: Session = Depends(get_db),
):
    check_and_raise(db, user, "blasts_per_month")
    check_per_call(db, user, "blast_recipients", len(payload.recipients))

    blast = EmailBlast(
        user_id=user.id,
        subject=payload.subject,
        body_html=payload.body_html,
        recipient_list="\n".join(payload.recipients),
        recipient_count=len(payload.recipients),
        scheduled_at=payload.scheduled_at,
        status="pending",
    )
    db.add(blast)
    db.commit()
    db.refresh(blast)

    if not payload.scheduled_at:
        from services.email_utils import send_blast_email
        sent, failed = send_blast_email(blast.subject, blast.body_html, payload.recipients)
        blast.sent_count = sent
        blast.failed_count = failed
        blast.status = ("sent" if failed == 0 and sent > 0
                        else "partial" if sent > 0 else "failed")
        db.commit()

    log_action(db, user, "CREATE", "EmailBlast", str(blast.id))
    return {"id": blast.id, "status": blast.status}


# ------------------------------ AI generation ------------------------------ #

@router.post("/ai/generate")
async def ai_generate(
    payload: CampaignGenerateRequest,
    user: User = Depends(require_perm("marketing.ai_generate")),
    db: Session = Depends(get_db),
):
    check_and_raise(db, user, "ai_generations_per_month")
    try:
        result = generate_campaign(
            plan=payload.plan,
            schedule=payload.schedule,
            count=payload.count,
            channels=payload.channels,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")
    log_action(db, user, "AI_GENERATE", "Campaign",
               detail=f"Generated {len((result or {}).get('items', []))} item(s)")
    return result


@router.post("/ai/schedule")
async def ai_schedule(
    payload: CampaignScheduleRequest,
    request: Request,
    user: User = Depends(require_perm("marketing.ai_generate")),
    db: Session = Depends(get_db),
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="No items to schedule")
    from models import MediaAsset, MediaJob  # local to keep module deps tidy

    needs_social = any(i.kind == "social_post" for i in payload.items)
    needs_email = any(i.kind == "email_blast" for i in payload.items)

    owned: List[SocialConnection] = []
    if needs_social:
        if not payload.connection_ids:
            raise HTTPException(status_code=400, detail="Pick at least one social connection")
        owned = db.query(SocialConnection).filter(
            SocialConnection.id.in_(payload.connection_ids),
            SocialConnection.user_id == user.id,
            SocialConnection.is_active == True,  # noqa: E712
        ).all()
        if len(owned) != len(payload.connection_ids):
            raise HTTPException(status_code=400, detail="One or more connections invalid")

    if needs_email:
        if not user.perm("marketing.email_blast"):
            raise HTTPException(
                status_code=403,
                detail="Email blasts require a paid plan — upgrade to schedule them.",
            )
        if not payload.recipients:
            raise HTTPException(status_code=400, detail="Add at least one recipient for email blasts")
        check_per_call(db, user, "blast_recipients", len(payload.recipients))

    # Pre-flight monthly quotas: we'll create N posts and M blasts in one shot.
    new_posts = sum(1 for i in payload.items if i.kind == "social_post")
    new_blasts = sum(1 for i in payload.items if i.kind == "email_blast")
    if new_posts:
        check_and_raise(db, user, "posts_per_month", increment=new_posts)
    if new_blasts:
        check_and_raise(db, user, "blasts_per_month", increment=new_blasts)

    conn_csv = ",".join(str(c.id) for c in owned)
    recipients_block = "\n".join(payload.recipients)

    # Media settings — validate references + pre-flight total credit cost.
    media_settings = payload.media or MediaScheduleSettings()
    image_model = resolve_model("image", media_settings.image_model_key) if media_settings.generate_images else None
    video_model = resolve_model("video", media_settings.video_model_key) if media_settings.generate_videos else None
    image_asset = None
    video_asset = None
    if media_settings.generate_images:
        if not media_settings.image_asset_id:
            raise HTTPException(status_code=400, detail="Pick a reference asset for AI-generated images.")
        image_asset = db.query(MediaAsset).filter(
            MediaAsset.id == media_settings.image_asset_id,
            MediaAsset.user_id == user.id,
            MediaAsset.is_active == True,  # noqa: E712
        ).first()
        if not image_asset:
            raise HTTPException(status_code=400, detail="Image reference asset not found.")
    if media_settings.generate_videos:
        if not media_settings.video_asset_id:
            raise HTTPException(status_code=400, detail="Pick a reference asset for AI-generated videos.")
        video_asset = db.query(MediaAsset).filter(
            MediaAsset.id == media_settings.video_asset_id,
            MediaAsset.user_id == user.id,
            MediaAsset.is_active == True,  # noqa: E712
        ).first()
        if not video_asset:
            raise HTTPException(status_code=400, detail="Video reference asset not found.")

    # Pre-flight: count items that actually want media, sum cost, check balance.
    img_jobs_wanted = sum(
        1 for i in payload.items
        if media_settings.generate_images and i.kind == "social_post" and i.image_prompt
    )
    vid_jobs_wanted = sum(
        1 for i in payload.items
        if media_settings.generate_videos and i.kind == "social_post" and i.video_prompt
    )
    total_estimated_tokens = 0
    if image_model and img_jobs_wanted:
        from services.media import estimate_tokens
        total_estimated_tokens += estimate_tokens(image_model["key"]) * img_jobs_wanted
    if video_model and vid_jobs_wanted:
        from services.media import billed_video_seconds, estimate_tokens
        video_units = billed_video_seconds(video_model["endpoint"], 5)  # matches _enqueue_media_job's video default
        total_estimated_tokens += estimate_tokens(video_model["key"], units=video_units) * vid_jobs_wanted
    if total_estimated_tokens > 0:
        from services.token_wallet import check_sufficient
        check_sufficient(db, user, total_estimated_tokens)

    created_posts = 0
    created_blasts = 0
    enqueued_image_jobs = 0
    enqueued_video_jobs = 0
    media_errors: List[str] = []

    for item in payload.items:
        if item.kind == "social_post":
            post = SocialPost(
                user_id=user.id,
                content=item.content,
                image_url=item.image_url,
                link_url=item.link_url,
                connection_ids=conn_csv,
                scheduled_at=item.scheduled_at,
                status="pending",
                ai_generated=True,
            )
            db.add(post)
            db.flush()  # need post.id for media job FK
            created_posts += 1

            if media_settings.generate_images and item.image_prompt and image_model and image_asset:
                job = await _enqueue_media_job(
                    db, user, kind="image", model=image_model,
                    prompt=item.image_prompt, ref_urls=[image_asset.file_url],
                    ref_asset_ids=[image_asset.id], request=request,
                )
                if job is None:
                    media_errors.append(f"image gen failed for item {created_posts}")
                else:
                    post.image_job_id = job.id
                    enqueued_image_jobs += 1
            if media_settings.generate_videos and item.video_prompt and video_model and video_asset:
                job = await _enqueue_media_job(
                    db, user, kind="video", model=video_model,
                    prompt=item.video_prompt, ref_urls=[video_asset.file_url],
                    ref_asset_ids=[video_asset.id], request=request,
                )
                if job is None:
                    media_errors.append(f"video gen failed for item {created_posts}")
                else:
                    post.video_job_id = job.id
                    enqueued_video_jobs += 1

        elif item.kind == "email_blast":
            db.add(EmailBlast(
                user_id=user.id,
                subject=item.subject or "(no subject)",
                body_html=item.content,
                recipient_list=recipients_block,
                recipient_count=len(payload.recipients),
                scheduled_at=item.scheduled_at,
                status="pending",
                ai_generated=True,
            ))
            created_blasts += 1

    db.commit()
    log_action(
        db, user, "CREATE", "Campaign",
        detail=f"AI campaign: {created_posts} post(s), {created_blasts} blast(s), "
               f"{enqueued_image_jobs} image jobs, {enqueued_video_jobs} video jobs",
    )
    return {
        "posts": created_posts,
        "blasts": created_blasts,
        "image_jobs": enqueued_image_jobs,
        "video_jobs": enqueued_video_jobs,
        "media_errors": media_errors,
    }


async def _enqueue_media_job(db, user, *, kind: str, model: dict, prompt: str,
                              ref_urls: list, ref_asset_ids: list,
                              request: Optional[Request] = None):
    """Pre-flight balance check + create a MediaJob row + submit to fal.
    Returns the job on success, or None if any step failed.

    No credits are spent up front — under JTS billing, the real debit
    happens on success via the shared `fal_webhook`/polling completion
    path (see services/token_wallet.debit_after_success), same as
    routes/media_routes.py's create_image_job/create_video_job. So there
    is nothing to refund on failure here either."""
    from models import MediaJob
    import secrets as _py_secrets
    from services.media import billed_video_seconds, estimate_tokens
    from services.token_wallet import check_sufficient

    # Video here always uses build_video_payload's default 5s (no duration
    # is plumbed through the AI-campaign flow yet) — keep the pre-flight
    # estimate and the row's duration_seconds consistent with that so the
    # eventual real debit uses the same value.
    duration_seconds = 5 if kind == "video" else None
    if kind == "video":
        units = billed_video_seconds(model["endpoint"], duration_seconds)
    else:
        units = 1
    check_sufficient(db, user, estimate_tokens(model["key"], units=units))

    webhook_token = _py_secrets.token_urlsafe(24)
    job = MediaJob(
        user_id=user.id,
        kind=kind,
        provider="fal",
        model_key=model["key"],
        model_endpoint=model["endpoint"],
        prompt=prompt,
        ref_asset_ids=",".join(str(i) for i in ref_asset_ids),
        status="queued",
        duration_seconds=duration_seconds,
        webhook_token=webhook_token,
    )
    db.add(job)
    db.flush()

    try:
        if kind == "image":
            fal_payload = build_image_payload(model, prompt, ref_urls)
        else:
            fal_payload = build_video_payload(model, prompt, ref_urls[0])
        webhook_url = None
        if request is not None:
            from routes.media_routes import _webhook_url_for
            webhook_url = _webhook_url_for(request, job.id, webhook_token)
        request_id = await submit_job(model["endpoint"], fal_payload, webhook_url=webhook_url)
        job.fal_request_id = request_id
        job.status = "running"
        return job
    except Exception as e:
        job.status = "failed"
        job.error = f"submit failed: {e}"
        return None
