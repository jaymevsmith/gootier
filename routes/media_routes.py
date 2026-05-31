"""HTTP routes for the media library (Phase 2) — upload, list, delete reference assets.

Generation endpoints (image / video) and webhook receivers ship in Phase 3+.
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import get_current_user, get_current_user_optional
from database import get_db
from models import MediaAsset, User, log_action
from services.credits import balance as credits_balance
from services.media import ACCEPTED_IMAGE_TYPES, MAX_UPLOAD_BYTES, upload_bytes

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
