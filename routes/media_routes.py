"""HTTP routes for the media library (Phase 2) — upload, list, delete reference assets.

Generation endpoints (image / video) and webhook receivers ship in Phase 3+.
"""
import secrets as py_secrets
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
from services.credits import balance as credits_balance, spend as credits_spend
from services.env_config import get_env
from services.media import (
    ACCEPTED_IMAGE_TYPES, MAX_UPLOAD_BYTES,
    MEDIA_MODEL_CATALOG, build_image_payload, build_video_payload,
    extract_first_image_url, extract_video_url,
    fetch_result, fetch_status, resolve_model, submit_job,
    upload_bytes, _status_phase,
)


def _webhook_url_for(request: Request, job_id: int, token: str) -> Optional[str]:
    """Build the publicly-reachable webhook URL fal should call when the job
    completes. Returns None for local dev (where fal can't reach 127.0.0.1)."""
    base = (get_env("APP_URL", "") or "").rstrip("/")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
    if base.startswith("http://localhost") or base.startswith("http://127."):
        return None  # fal can't hit local dev — fall back to polling
    return f"{base}/webhooks/fal/{job_id}/{token}"

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
    request: Request,
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

    from services.media import estimate_tokens
    from services.token_wallet import check_sufficient
    check_sufficient(db, user, estimate_tokens(model["key"]))

    webhook_token = py_secrets.token_urlsafe(24)
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
        webhook_token=webhook_token,
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
        webhook_url = _webhook_url_for(request, job.id, webhook_token)
        request_id = await submit_job(model["endpoint"], fal_payload, webhook_url=webhook_url)
        job.fal_request_id = request_id
        job.status = "running"
        db.commit()
        log_action(db, user, "CREATE", "MediaJob", str(job.id),
                   detail=f"image submit endpoint={model['endpoint']} req={request_id} webhook={'yes' if webhook_url else 'no'}")
    except HTTPException:
        _mark_failed(db, user, job, "fal submission failed (HTTPException)")
        raise
    except Exception as e:
        _mark_failed(db, user, job, f"fal submission failed: {e}")
        raise HTTPException(status_code=502, detail=f"Image generation failed to start: {e}")

    db.refresh(job)
    return _serialize_job(job)


def _mark_failed(db: Session, user: User, job: MediaJob, error: str) -> None:
    """No refund needed — under JTS billing, nothing is charged until the job
    succeeds (see fal_webhook), so a failure before that point never spent
    anything."""
    job.status = "failed"
    job.error = error
    job.completed_at = datetime.utcnow()
    db.commit()


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
            from services.media import JTS_RATE_KEY
            from services.token_wallet import debit_after_success
            if job.kind == "video":
                units = float(job.duration_seconds or 5)
            else:
                units = 1
            debit_after_success(
                db, user, model_key=JTS_RATE_KEY[job.model_key],
                request_id=f"gootier-mediajob-{job.id}",
                units=units,
            )
            log_action(db, user, "UPDATE", "MediaJob", str(job.id), detail="completed")
        except Exception as e:
            _mark_failed(db, user, job, f"result fetch failed: {e}")
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


class ClipOption(BaseModel):
    """Per-clip transforms applied during normalisation in the compose pipeline.

    Order in ``ComposeJobCreate.clip_options`` is parallel to ``source_job_ids``.
    Any entry / field is optional — omitted values mean "no transform".
    """
    crop_aspect: Optional[str] = None  # "9:16" | "1:1" | "16:9" | "4:5"
    reverse: bool = False
    speed: float = 1.0                  # 0.25..4.0, clamped server-side


class TransitionSpec(BaseModel):
    """A transition between two adjacent clips in the compose sequence.
    ``type=""`` (or missing) means a hard cut. ``duration`` is the overlap
    in seconds (clamped 0.1..2.0)."""
    type: str = ""
    duration: float = 0.5


class TextOverlay(BaseModel):
    """A burned-in text caption on a single clip.

    Multiple overlays can sit on the same clip (e.g. headline + subline) — they
    each apply to the same drawtext filter chain.  ``clip_index`` is 0-based
    against ``source_job_ids``.
    """
    clip_index: int = 0
    text: str = ""
    position: str = "bottom"
    fontsize: int = 64
    color: str = "white"
    start: float = 0.0          # seconds into the clip
    duration: float = 3.0       # how long to display
    outline: bool = True        # black stroke for legibility on busy footage


_ALLOWED_CROP_ASPECTS = {"9:16", "1:1", "16:9", "4:5", "3:4", "4:3"}
# Subset of ffmpeg's xfade transition names we expose in the UI.  Keep this
# in sync with TRANSITION_CHOICES in templates/studio.html.
_ALLOWED_TRANSITIONS = {
    "", "fade", "fadeblack", "fadewhite",
    "slideleft", "slideright", "slideup", "slidedown",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "circleopen", "circleclose", "radial", "pixelize",
    "dissolve", "distance",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
}
_ALLOWED_TEXT_POSITIONS = {
    "top", "center", "bottom",
    "top-left", "top-right", "bottom-left", "bottom-right",
}


def _sanitize_text_overlay(ov: TextOverlay, n_clips: int) -> dict:
    """Clamp + validate a single text overlay against allowlists.  Raises
    HTTPException(400) on anything out of bounds."""
    if not ov.text or not ov.text.strip():
        raise HTTPException(status_code=400, detail="text_overlay text is empty.")
    if len(ov.text) > 300:
        raise HTTPException(status_code=400, detail="text_overlay text > 300 chars.")
    if ov.position not in _ALLOWED_TEXT_POSITIONS:
        raise HTTPException(
            status_code=400,
            detail=f"text_overlay position '{ov.position}' not in {sorted(_ALLOWED_TEXT_POSITIONS)}.",
        )
    if not (0 <= ov.clip_index < n_clips):
        raise HTTPException(
            status_code=400,
            detail=f"text_overlay clip_index {ov.clip_index} out of range.",
        )
    return {
        "clip_index": int(ov.clip_index),
        "text": ov.text.strip(),
        "position": ov.position,
        "fontsize": max(24, min(160, int(ov.fontsize or 64))),
        "color": (ov.color or "white").strip(),
        "start": max(0.0, float(ov.start or 0.0)),
        "duration": max(0.1, min(60.0, float(ov.duration or 3.0))),
        "outline": bool(ov.outline),
    }


class ComposeJobCreate(BaseModel):
    source_job_ids: List[int] = Field(..., min_length=1, max_length=10)
    keep_original_audio: bool = True
    narration_script: Optional[str] = None
    narration_voice: Optional[str] = None
    music_url: Optional[str] = None
    clip_options: Optional[List[ClipOption]] = None
    transitions: Optional[List[TransitionSpec]] = None
    text_overlays: Optional[List[TextOverlay]] = None


@router.post("/api/media/jobs/compose")
async def create_compose_job(
    payload: ComposeJobCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """String 2-10 video clips (from this user's prior video MediaJobs) into a
    single video, optionally with a TTS narration overlay and a music bed."""
    import json as _json
    from services.media import tts_catalog
    from services.video_composer import MAX_TOTAL_SECONDS

    sources = db.query(MediaJob).filter(
        MediaJob.id.in_(payload.source_job_ids),
        MediaJob.user_id == user.id,
        MediaJob.kind == "video",
        MediaJob.status == "done",
        MediaJob.result_url != None,  # noqa: E711
    ).all()
    if len(sources) != len(payload.source_job_ids):
        raise HTTPException(status_code=400,
                            detail="One or more source clips are missing, not yours, or not finished yet.")
    by_id = {s.id: s for s in sources}
    ordered_clips = [by_id[i] for i in payload.source_job_ids if i in by_id]

    # Validate + normalise per-clip transforms (parallel to source_job_ids).
    raw_opts = payload.clip_options or []
    if raw_opts and len(raw_opts) != len(payload.source_job_ids):
        raise HTTPException(status_code=400,
                            detail="clip_options length must match source_job_ids.")
    sanitized_opts: list[dict] = []
    for opt in raw_opts:
        crop = opt.crop_aspect
        if crop and crop not in _ALLOWED_CROP_ASPECTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported crop_aspect '{crop}'. "
                       f"Allowed: {', '.join(sorted(_ALLOWED_CROP_ASPECTS))}.",
            )
        speed = max(0.25, min(4.0, float(opt.speed or 1.0)))
        sanitized_opts.append({
            "crop_aspect": crop or None,
            "reverse": bool(opt.reverse),
            "speed": speed,
        })

    # Validate transitions (length N-1, one entry per gap between clips).
    raw_transitions = payload.transitions or []
    expected_trans = max(0, len(payload.source_job_ids) - 1)
    if raw_transitions and len(raw_transitions) != expected_trans:
        raise HTTPException(
            status_code=400,
            detail=f"transitions length must equal source_job_ids - 1 ({expected_trans}).",
        )
    sanitized_trans: list[dict] = []
    for t in raw_transitions:
        ttype = (t.type or "").strip()
        if ttype not in _ALLOWED_TRANSITIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported transition '{ttype}'.",
            )
        td = max(0.1, min(2.0, float(t.duration or 0.5)))
        sanitized_trans.append({"type": ttype, "duration": td})

    # Text overlays — validated + clamped per overlay.
    sanitized_overlays: list[dict] = [
        _sanitize_text_overlay(ov, len(payload.source_job_ids))
        for ov in (payload.text_overlays or [])
    ]

    # Credit cost: flat 50 for the compose + TTS surcharge if narration set.
    cost = 50
    tts_endpoint = None
    voice_id = None
    if payload.narration_script:
        script = (payload.narration_script or "").strip()
        if len(script) < 4:
            raise HTTPException(status_code=400, detail="Narration script too short.")
        if len(script) > 1500:
            raise HTTPException(status_code=400, detail="Narration script too long (max 1500 chars).")
        cat = tts_catalog()
        default = next((k for k, v in cat.items() if v.get("default")), None)
        chosen = payload.narration_voice or "Rachel"
        model = cat.get(default) or next(iter(cat.values()))
        tts_endpoint = model["endpoint"]
        voice_map = dict(model["voices"])
        voice_id = voice_map.get(chosen) or list(voice_map.values())[0]
        cost += int((len(script) / 100) * model["credits_per_100_chars"]) + 1

    credits_spend(db, user, cost, reason="video_compose",
                  detail=f"clips={len(ordered_clips)} tts={'yes' if tts_endpoint else 'no'} music={'yes' if payload.music_url else 'no'}")

    job = MediaJob(
        user_id=user.id,
        kind="video",
        provider="internal-ffmpeg",
        model_key="compose",
        model_endpoint="internal:ffmpeg-compose",
        prompt=(payload.narration_script or "")[:500],
        ref_asset_ids=None,
        status="queued",
        cost_credits=cost,
        compose_meta_json=_json.dumps({
            "source_job_ids": payload.source_job_ids,
            "keep_original_audio": payload.keep_original_audio,
            "narration_voice": payload.narration_voice,
            "music_url": payload.music_url,
            "clip_options": sanitized_opts or None,
            "transitions": sanitized_trans or None,
            "text_overlays": sanitized_overlays or None,
        }),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Fire the ffmpeg worker as an asyncio task — the work is local, bounded
    # (~20-40s for a 60s output), and doesn't need request scope.  Using
    # FastAPI BackgroundTasks in addition would double-dispatch the job and
    # cause two ffmpeg passes racing on the same MediaJob row.
    import asyncio as _asyncio
    _asyncio.create_task(_run_compose_job(
        job.id, [c.result_url for c in ordered_clips],
        payload.keep_original_audio,
        payload.narration_script, tts_endpoint, voice_id,
        payload.music_url, user.id, sanitized_opts or None,
        sanitized_trans or None, sanitized_overlays or None,
    ))

    log_action(db, user, "CREATE", "MediaJob", str(job.id),
               detail=f"compose: {len(ordered_clips)} clips")
    return _serialize_job(job)


async def _run_compose_job(job_id: int, clip_urls: list, keep_original: bool,
                            narration_script: Optional[str], tts_endpoint: Optional[str],
                            voice_id: Optional[str], music_url: Optional[str],
                            user_id: int,
                            clip_options: Optional[List[dict]] = None,
                            transitions: Optional[List[dict]] = None,
                            text_overlays: Optional[List[dict]] = None) -> None:
    """Background worker — does the actual ffmpeg + TTS work + DB update."""
    from datetime import datetime as _dt
    from database import SessionLocal
    from services.video_composer import compose, synth_tts_to_file
    import tempfile, os as _os, logging
    log = logging.getLogger("gootier.composer")
    db = SessionLocal()
    try:
        job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
        if not job:
            return
        job.status = "running"
        db.commit()

        narration_path = None
        music_path = None
        tmp_audio_dir = None
        try:
            if narration_script and tts_endpoint and voice_id:
                tmp_audio_dir = tempfile.mkdtemp(prefix="gootier-tts-")
                narration_path = _os.path.join(tmp_audio_dir, "narration.mp3")
                await synth_tts_to_file(narration_script, voice_id, narration_path,
                                          model_endpoint=tts_endpoint)
            if music_url:
                if tmp_audio_dir is None:
                    tmp_audio_dir = tempfile.mkdtemp(prefix="gootier-music-")
                music_path = _os.path.join(tmp_audio_dir, "music")
                from services.video_composer import _download
                await _download(music_url, music_path)

            url = await compose(
                clip_urls,
                narration_path=narration_path,
                music_path=music_path,
                keep_original_audio=keep_original,
                clip_options=clip_options,
                transitions=transitions,
                text_overlays=text_overlays,
            )
            job.result_url = url
            job.thumbnail_url = url
            job.status = "done"
            job.completed_at = _dt.utcnow()
            db.commit()
            log.info("compose job %s done -> %s", job.id, url)
        finally:
            if tmp_audio_dir:
                import shutil as _shutil
                _shutil.rmtree(tmp_audio_dir, ignore_errors=True)
    except Exception as e:
        log.exception("compose job %s failed: %s", job_id, e)
        try:
            job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
            if job and job.status != "done":
                from services.credits import grant as _grant
                user = db.query(User).filter(User.id == user_id).first()
                if user and job.cost_credits:
                    _grant(db, user, job.cost_credits,
                           reason=f"refund_failed_{job.id}",
                           detail=f"Auto-refund — compose failed: {e}")
                job.status = "failed"
                job.error = str(e)[:1000]
                job.completed_at = _dt.utcnow()
                db.commit()
        except Exception:
            log.exception("refund/finalise also failed for job %s", job_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# AI-assisted compose: brief + clip metadata → structured plan + (optional)
# music generation.  The plan is just a recommendation — the client pre-fills
# the existing studio sequence editor with the AI's choices and the user
# tweaks anything before hitting "Compose video".
# ---------------------------------------------------------------------------

class AIComposePlanRequest(BaseModel):
    source_job_ids: List[int] = Field(..., min_length=1, max_length=10)
    brief: str = Field(..., min_length=4, max_length=2000)
    style: Optional[str] = None         # "cinematic" | "energetic" | "chill" | etc
    want_music: bool = True
    want_text: bool = True


@router.post("/api/media/jobs/compose/ai-plan")
async def compose_ai_plan(
    payload: AIComposePlanRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask the AI to plan a compose job given an ordered clip list + a brief.

    Returns a sanitized plan with ``clip_options``, ``transitions``,
    ``text_overlays``, and an optional ``music_prompt``.  The client should
    feed this straight into the existing ``POST /api/media/jobs/compose``
    endpoint after the user reviews + edits.

    Costs 2 credits (Sonnet planning call only — music generation is billed
    separately if the user accepts the suggested music_prompt).
    """
    # 1) Validate clips belong to the user, are video, are done.
    sources = db.query(MediaJob).filter(
        MediaJob.id.in_(payload.source_job_ids),
        MediaJob.user_id == user.id,
        MediaJob.kind == "video",
        MediaJob.status == "done",
        MediaJob.result_url != None,  # noqa: E711
    ).all()
    if len(sources) != len(payload.source_job_ids):
        raise HTTPException(status_code=400,
                            detail="One or more source clips are missing, not yours, or not finished yet.")
    by_id = {s.id: s for s in sources}
    ordered_clips = [by_id[i] for i in payload.source_job_ids if i in by_id]

    clip_meta = [{
        "clip_index": i,
        "duration_seconds": float(c.duration_seconds or 5),
        "model_key": c.model_key or "unknown",
        "prompt": (c.prompt or "")[:200],
    } for i, c in enumerate(ordered_clips)]

    credits_spend(db, user, 2, reason="video_compose_ai_plan",
                  detail=f"clips={len(ordered_clips)} brief_chars={len(payload.brief)}")

    # 2) Call Sonnet for the plan.
    from services.ai_generator import plan_video_compose
    try:
        plan = plan_video_compose(
            brief=payload.brief,
            clip_meta=clip_meta,
            want_music=payload.want_music,
            want_text=payload.want_text,
            style=payload.style,
        )
    except Exception as e:
        # Refund the 2 credits on failure — same pattern as compose worker.
        from services.credits import grant as _grant
        _grant(db, user, 2,
               reason="refund_ai_plan_failed",
               detail=f"AI plan failed: {str(e)[:200]}")
        raise HTTPException(status_code=502, detail=f"AI plan failed: {e}")

    # 3) Sanitize the plan against the same allowlists the compose endpoint uses.
    #
    # Index-mapping rule: we try to honour the AI's `clip_index` / `between`
    # values, but if multiple entries collide on the same index (or indices
    # are missing entirely) we fall back to positional order — otherwise the
    # last-writer-wins dict overwrite silently drops most clips' transforms
    # and they get the default 1× speed / no crop / no reverse.
    def _index_or_fallback(items: list, key: str, expected: int) -> list:
        if not items:
            return [{} for _ in range(expected)]
        # Honour explicit indices only when they form a clean 0..expected-1 set.
        indices = [int(it.get(key, idx)) for idx, it in enumerate(items)]
        unique_indices = set(indices)
        looks_clean = (
            len(indices) == len(unique_indices) == expected
            and all(0 <= idx < expected for idx in unique_indices)
        )
        if looks_clean:
            by_idx = {indices[i]: items[i] for i in range(len(items))}
            return [by_idx.get(i, {}) for i in range(expected)]
        # Fall back to position — pad / truncate to expected length.
        out = list(items[:expected])
        while len(out) < expected:
            out.append({})
        return out

    n = len(ordered_clips)
    raw_opts = plan.get("clip_options") or []
    options_in_order = _index_or_fallback(raw_opts, "clip_index", n)
    clip_options: list[dict] = []
    for i in range(n):
        o = options_in_order[i] or {}
        crop = o.get("crop_aspect")
        if crop and crop not in _ALLOWED_CROP_ASPECTS:
            crop = None
        clip_options.append({
            "crop_aspect": crop,
            "reverse": bool(o.get("reverse")),
            "speed": max(0.25, min(4.0, float(o.get("speed") or 1.0))),
            "rationale": (o.get("rationale") or "")[:200],
        })

    raw_trans = plan.get("transitions") or []
    trans_in_order = _index_or_fallback(raw_trans, "between", max(0, n - 1))
    transitions: list[dict] = []
    for i in range(max(0, n - 1)):
        t = trans_in_order[i] or {}
        ttype = (t.get("type") or "").strip()
        if ttype not in _ALLOWED_TRANSITIONS:
            ttype = ""
        transitions.append({
            "type": ttype,
            "duration": max(0.1, min(2.0, float(t.get("duration") or 0.5))),
            "rationale": (t.get("rationale") or "")[:200],
        })

    text_overlays: list[dict] = []
    for ov in (plan.get("text_overlays") or []):
        idx = int(ov.get("clip_index", 0))
        if not (0 <= idx < n):
            continue
        text = (ov.get("text") or "").strip()
        if not text or len(text) > 300:
            continue
        pos = ov.get("position") or "bottom"
        if pos not in _ALLOWED_TEXT_POSITIONS:
            pos = "bottom"
        text_overlays.append({
            "clip_index": idx,
            "text": text,
            "position": pos,
            "start": max(0.0, float(ov.get("start") or 0.0)),
            "duration": max(0.1, min(60.0, float(ov.get("duration") or 3.0))),
            "fontsize": max(24, min(160, int(ov.get("fontsize") or 64))),
            "color": (ov.get("color") or "white").strip()[:32],
            "outline": True,
        })

    music_prompt = (plan.get("music_prompt") or "").strip() if payload.want_music else ""
    if music_prompt and len(music_prompt) > 800:
        music_prompt = music_prompt[:800]

    log_action(db, user, "CREATE", "AIComposePlan", "-",
               detail=f"clips={n} music={'yes' if music_prompt else 'no'} "
                      f"text_overlays={len(text_overlays)}")

    return {
        "narrative": (plan.get("narrative") or "")[:500],
        "clip_options": clip_options,
        "transitions": transitions,
        "text_overlays": text_overlays,
        "music_prompt": music_prompt or None,
    }


class MusicGenRequest(BaseModel):
    prompt: str = Field(..., min_length=4, max_length=800)
    seconds: int = Field(30, ge=5, le=60)


@router.post("/api/media/jobs/music")
async def create_music_job(
    payload: MusicGenRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate an instrumental music clip via fal stable-audio.

    Costs 8 credits — billed up-front, refunded on failure.  Returns the
    standard MediaJob serialization so the client can poll
    ``GET /api/media/jobs/{id}`` until ``result_url`` is set.
    """
    import asyncio as _asyncio
    cost = 8
    credits_spend(db, user, cost, reason="music_generate",
                  detail=f"seconds={payload.seconds} prompt_chars={len(payload.prompt)}")
    job = MediaJob(
        user_id=user.id,
        kind="audio",
        provider="fal-stable-audio",
        model_key="stable-audio",
        model_endpoint="fal-ai/stable-audio",
        prompt=payload.prompt[:500],
        status="queued",
        cost_credits=cost,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _asyncio.create_task(_run_music_job(job.id, payload.prompt, payload.seconds, user.id))
    log_action(db, user, "CREATE", "MediaJob", str(job.id),
               detail=f"music: {payload.seconds}s")
    return _serialize_job(job)


async def _run_music_job(job_id: int, prompt: str, seconds: int, user_id: int) -> None:
    """Background worker for /api/media/jobs/music."""
    from datetime import datetime as _dt
    from database import SessionLocal
    from services.media import generate_music
    import logging
    log = logging.getLogger("gootier.music")
    db = SessionLocal()
    try:
        job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
        if not job:
            return
        job.status = "running"
        db.commit()
        try:
            url = await generate_music(prompt, seconds=seconds)
            if not url:
                raise RuntimeError("Music generator returned no URL.")
            job.result_url = url
            job.status = "done"
            job.completed_at = _dt.utcnow()
            db.commit()
            log.info("music job %s done -> %s", job.id, url)
        except Exception as e:
            log.exception("music job %s failed: %s", job_id, e)
            from services.credits import grant as _grant
            user = db.query(User).filter(User.id == user_id).first()
            if user and job.cost_credits:
                _grant(db, user, job.cost_credits,
                       reason=f"refund_failed_{job.id}",
                       detail=f"Auto-refund — music gen failed: {e}")
            job.status = "failed"
            job.error = str(e)[:1000]
            job.completed_at = _dt.utcnow()
            db.commit()
    finally:
        db.close()


@router.post("/api/media/jobs/video")
async def create_video_job(
    payload: VideoJobCreate,
    request: Request,
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

    webhook_token = py_secrets.token_urlsafe(24)
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
        webhook_token=webhook_token,
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
        webhook_url = _webhook_url_for(request, job.id, webhook_token)
        request_id = await submit_job(model["endpoint"], fal_payload, webhook_url=webhook_url)
        job.fal_request_id = request_id
        job.status = "running"
        db.commit()
        log_action(db, user, "CREATE", "MediaJob", str(job.id),
                   detail=f"video submit endpoint={model['endpoint']} req={request_id} webhook={'yes' if webhook_url else 'no'}")
    except HTTPException:
        _mark_failed(db, user, job, "fal submission failed (HTTPException)")
        raise
    except Exception as e:
        _mark_failed(db, user, job, f"fal submission failed: {e}")
        raise HTTPException(status_code=502, detail=f"Video generation failed to start: {e}")

    db.refresh(job)
    return _serialize_job(job)


# --------------------------------------------------------------------------- #
# Webhook receiver — fal calls this on completion (replaces polling round-trips)
# --------------------------------------------------------------------------- #

@router.post("/webhooks/fal/{job_id}/{token}")
async def fal_webhook(
    job_id: int,
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """fal POSTs the result here when a job finishes. Token in the URL path
    authenticates the callback (job_ids are sequential; the per-job random
    token blocks guessing)."""
    job = db.query(MediaJob).filter(
        MediaJob.id == job_id,
        MediaJob.webhook_token == token,
    ).first()
    if not job:
        # Avoid leaking whether the job exists vs the token is wrong.
        raise HTTPException(status_code=404, detail="Not found")

    if job.status in ("done", "failed", "cancelled"):
        return {"ok": True, "noop": True}

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # fal's webhook payload shape: {status: "OK" | "ERROR", request_id, payload: {...the model result...}, error: str|None}
    status = (payload.get("status") or "").upper()
    body = payload.get("payload") or {}

    if status == "OK":
        try:
            if job.kind == "video":
                url = extract_video_url(body)
            else:
                url = extract_first_image_url(body)
            job.result_url = url
            job.thumbnail_url = url
            job.status = "done"
            job.completed_at = datetime.utcnow()
            db.commit()
            from services.media import JTS_RATE_KEY
            from services.token_wallet import debit_after_success
            if job.kind == "video":
                units = float(job.duration_seconds or 5)
            else:
                units = 1
            debit_after_success(
                db, job.user, model_key=JTS_RATE_KEY[job.model_key],
                request_id=f"gootier-mediajob-{job.id}",
                units=units,
            )
            log_action(db, job.user, "UPDATE", "MediaJob", str(job.id),
                       detail="completed via webhook")
        except Exception as e:
            _mark_failed(db, job.user, job, f"webhook result parse failed: {e}")
    elif status == "ERROR":
        err = payload.get("error") or "fal reported ERROR"
        _mark_failed(db, job.user, job, str(err))

    return {"ok": True}


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
