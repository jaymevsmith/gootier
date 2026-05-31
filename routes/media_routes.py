"""HTTP routes for the media library (Phase 2) — upload, list, delete reference assets.

Generation endpoints (image / video) and webhook receivers ship in Phase 3+.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user, get_current_user_optional
from database import get_db
from models import MediaAsset, MediaJob, User, log_action
from services.credits import balance as credits_balance, grant as credits_grant, spend as credits_spend
from services.media import (
    ACCEPTED_IMAGE_TYPES, MAX_UPLOAD_BYTES,
    MEDIA_MODEL_CATALOG, build_image_payload, build_video_payload,
    extract_first_image_url, extract_video_url,
    fetch_result, fetch_status, resolve_model, submit_job,
    upload_bytes, _status_phase,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

VALID_KINDS = ["mascot", "person", "product", "other"]


# --------------------------------------------------------------------------- #
# Web pages
# --------------------------------------------------------------------------- #

@router.get("/assets")
async def assets_page(
    request: Request,
    user: User = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    items = db.query(MediaAsset).filter(
        MediaAsset.user_id == user.id,
        MediaAsset.is_active == True,  # noqa: E712
    ).order_by(MediaAsset.created_at.desc()).all()
    return templates.TemplateResponse(request, "assets.html", {
        "user": user,
        "assets": items,
        "credits": credits_balance(db, user),
        "kinds": VALID_KINDS,
    })


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@router.get("/api/media/assets")
async def list_assets(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = db.query(MediaAsset).filter(
        MediaAsset.user_id == user.id,
        MediaAsset.is_active == True,  # noqa: E712
    ).order_by(MediaAsset.created_at.desc()).all()
    return {
        "assets": [
            {
                "id": a.id,
                "name": a.name,
                "kind": a.kind,
                "file_url": a.file_url,
                "mime_type": a.mime_type,
                "width": a.width,
                "height": a.height,
                "file_size_bytes": a.file_size_bytes,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in items
        ]
    }


@router.post("/api/media/assets")
async def upload_asset(
    name: str = Form(...),
    kind: str = Form("other"),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind. One of: {', '.join(VALID_KINDS)}")
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Name must be 1–80 characters.")

    content_type = file.content_type or "application/octet-stream"
    if content_type not in ACCEPTED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type: {content_type}. "
                   f"Accepted: {', '.join(sorted(ACCEPTED_IMAGE_TYPES))}.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image is too large ({len(content)} bytes). Max is {MAX_UPLOAD_BYTES // (1024*1024)} MB.",
        )

    # Stream the bytes to fal's CDN, store the returned URL.
    fal_url = await upload_bytes(content, content_type)

    # Best-effort image dimensions — only if Pillow is around, else null.
    width = height = None
    try:
        from io import BytesIO
        from PIL import Image  # noqa: F401
        with Image.open(BytesIO(content)) as img:
            width, height = img.size
    except Exception:
        pass

    asset = MediaAsset(
        user_id=user.id,
        name=name,
        kind=kind,
        file_url=fal_url,
        file_size_bytes=len(content),
        width=width,
        height=height,
        mime_type=content_type,
        is_active=True,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    log_action(db, user, "CREATE", "MediaAsset", str(asset.id),
               detail=f"name={asset.name} kind={asset.kind}")

    return {
        "ok": True,
        "asset": {
            "id": asset.id,
            "name": asset.name,
            "kind": asset.kind,
            "file_url": asset.file_url,
            "width": asset.width,
            "height": asset.height,
        },
    }


@router.delete("/api/media/assets/{asset_id}")
async def delete_asset(
    asset_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    asset = db.query(MediaAsset).filter(
        MediaAsset.id == asset_id, MediaAsset.user_id == user.id,
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.is_active = False
    db.commit()
    log_action(db, user, "DELETE", "MediaAsset", str(asset.id))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Catalog + balance helpers
# --------------------------------------------------------------------------- #

@router.get("/api/media/catalog")
async def media_catalog(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Catalog of available models + the user's current credit balance —
    consumed by the generation modals in /compose, /ai-builder, /assets."""
    bal = credits_balance(db, user)
    out = {"balance": bal, "models": {}}
    for kind, options in MEDIA_MODEL_CATALOG.items():
        out["models"][kind] = [
            {
                "key": k,
                "label": v["label"],
                "credits": v["credits"],
                "supports_reference": v.get("supports_reference", False),
                "default": v.get("default", False),
            }
            for k, v in options.items()
        ]
    return out


# --------------------------------------------------------------------------- #
# Image generation
# --------------------------------------------------------------------------- #

class ImageJobCreate(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=4000)
    model_key: Optional[str] = None
    asset_ids: List[int] = []
    aspect_ratio: str = "auto"
    resolution: str = "1K"


def _serialize_job(job: MediaJob) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "model_key": job.model_key,
        "model_endpoint": job.model_endpoint,
        "prompt": job.prompt,
        "aspect_ratio": job.aspect_ratio,
        "duration_seconds": job.duration_seconds,
        "result_url": job.result_url,
        "thumbnail_url": job.thumbnail_url,
        "error": job.error,
        "cost_credits": job.cost_credits,
        "fal_request_id": job.fal_request_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@router.post("/api/media/jobs/image")
async def create_image_job(
    payload: ImageJobCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = resolve_model("image", payload.model_key)

    # Validate + resolve reference asset URLs (owned by this user).
    ref_urls = []
    if payload.asset_ids:
        owned = db.query(MediaAsset).filter(
            MediaAsset.id.in_(payload.asset_ids),
            MediaAsset.user_id == user.id,
            MediaAsset.is_active == True,  # noqa: E712
        ).all()
        if len(owned) != len(payload.asset_ids):
            raise HTTPException(status_code=400, detail="One or more reference assets invalid.")
        ref_urls = [a.file_url for a in owned]

    if model.get("supports_reference") and not ref_urls and model["endpoint"].endswith("/edit"):
        raise HTTPException(
            status_code=400,
            detail=f"{model['label']} needs at least one reference image. "
                   f"Pick one from your library or choose a text-only model.",
        )

    cost = int(model["credits"])
    credits_spend(db, user, cost, reason="image_gen", detail=f"model={model['key']}")

    job = MediaJob(
        user_id=user.id,
        kind="image",
        provider="fal",
        model_key=model["key"],
        model_endpoint=model["endpoint"],
        prompt=payload.prompt,
        ref_asset_ids=",".join(str(i) for i in payload.asset_ids) if payload.asset_ids else None,
        aspect_ratio=payload.aspect_ratio,
        status="queued",
        cost_credits=cost,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Wire the actual fal submission. If it explodes, refund credits + mark failed.
    try:
        fal_payload = build_image_payload(
            model, payload.prompt, ref_urls,
            aspect_ratio=payload.aspect_ratio,
            resolution=payload.resolution,
        )
        request_id = await submit_job(model["endpoint"], fal_payload)
        job.fal_request_id = request_id
        job.status = "running"
        db.commit()
        log_action(db, user, "CREATE", "MediaJob", str(job.id),
                   detail=f"image submit endpoint={model['endpoint']} req={request_id}")
    except HTTPException:
        _refund_and_fail(db, user, job, "fal submission failed (HTTPException)")
        raise
    except Exception as e:
        _refund_and_fail(db, user, job, f"fal submission failed: {e}")
        raise HTTPException(status_code=502, detail=f"Image generation failed to start: {e}")

    db.refresh(job)
    return _serialize_job(job)


def _refund_and_fail(db: Session, user: User, job: MediaJob, error: str) -> None:
    job.status = "failed"
    job.error = error
    job.completed_at = datetime.utcnow()
    db.commit()
    if job.cost_credits:
        credits_grant(db, user, job.cost_credits,
                      reason=f"refund_failed_{job.id}",
                      detail=f"Auto-refund for failed media job #{job.id}: {error[:120]}")


@router.get("/api/media/jobs/{job_id}")
async def get_media_job(
    job_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Snapshot of a media job. If it's still pending, syncs status from fal
    (and finalises the row if fal says it's done/failed)."""
    job = db.query(MediaJob).filter(
        MediaJob.id == job_id, MediaJob.user_id == user.id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in ("done", "failed", "cancelled") or not job.fal_request_id:
        return _serialize_job(job)

    try:
        status_obj = await fetch_status(job.model_endpoint, job.fal_request_id)
    except HTTPException:
        raise
    except Exception as e:
        # Transport issue — don't fail the job yet, the user can retry on next poll.
        job_data = _serialize_job(job)
        job_data["poll_error"] = str(e)
        return job_data

    phase = _status_phase(status_obj)
    if phase == "done":
        try:
            result = await fetch_result(job.model_endpoint, job.fal_request_id)
            if job.kind == "video":
                url = extract_video_url(result)
            else:
                url = extract_first_image_url(result)
            job.result_url = url
            job.thumbnail_url = url
            job.status = "done"
            job.completed_at = datetime.utcnow()
            db.commit()
            log_action(db, user, "UPDATE", "MediaJob", str(job.id), detail="completed")
        except Exception as e:
            _refund_and_fail(db, user, job, f"result fetch failed: {e}")
    elif phase == "running" and job.status != "running":
        job.status = "running"
        db.commit()

    db.refresh(job)
    return _serialize_job(job)


# --------------------------------------------------------------------------- #
# Video generation
# --------------------------------------------------------------------------- #

class VideoJobCreate(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=4000)
    model_key: Optional[str] = None
    asset_id: int  # video models take a single image_url
    duration_seconds: int = 5
    aspect_ratio: str = "auto"
    resolution: str = "720p"
    generate_audio: bool = True


@router.post("/api/media/jobs/video")
async def create_video_job(
    payload: VideoJobCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = resolve_model("video", payload.model_key)

    asset = db.query(MediaAsset).filter(
        MediaAsset.id == payload.asset_id,
        MediaAsset.user_id == user.id,
        MediaAsset.is_active == True,  # noqa: E712
    ).first()
    if not asset:
        raise HTTPException(status_code=400, detail="Reference asset not found or not yours.")

    cost = int(model["credits"])
    credits_spend(db, user, cost, reason="video_gen", detail=f"model={model['key']}")

    job = MediaJob(
        user_id=user.id,
        kind="video",
        provider="fal",
        model_key=model["key"],
        model_endpoint=model["endpoint"],
        prompt=payload.prompt,
        ref_asset_ids=str(asset.id),
        aspect_ratio=payload.aspect_ratio,
        duration_seconds=payload.duration_seconds,
        status="queued",
        cost_credits=cost,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        fal_payload = build_video_payload(
            model,
            prompt=payload.prompt,
            ref_url=asset.file_url,
            duration_seconds=payload.duration_seconds,
            aspect_ratio=payload.aspect_ratio,
            resolution=payload.resolution,
            generate_audio=payload.generate_audio,
        )
        request_id = await submit_job(model["endpoint"], fal_payload)
        job.fal_request_id = request_id
        job.status = "running"
        db.commit()
        log_action(db, user, "CREATE", "MediaJob", str(job.id),
                   detail=f"video submit endpoint={model['endpoint']} req={request_id}")
    except HTTPException:
        _refund_and_fail(db, user, job, "fal submission failed (HTTPException)")
        raise
    except Exception as e:
        _refund_and_fail(db, user, job, f"fal submission failed: {e}")
        raise HTTPException(status_code=502, detail=f"Video generation failed to start: {e}")

    db.refresh(job)
    return _serialize_job(job)


@router.get("/api/media/jobs")
async def list_recent_jobs(
    limit: int = 25,
    kind: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(MediaJob).filter(MediaJob.user_id == user.id)
    if kind in ("image", "video"):
        q = q.filter(MediaJob.kind == kind)
    rows = q.order_by(MediaJob.created_at.desc()).limit(max(1, min(100, limit))).all()
    return {"jobs": [_serialize_job(j) for j in rows]}
