"""Thin async wrapper around fal.ai's API.

Two concerns live here:
  1. The model catalog — short keys → fal endpoint IDs + credit cost mapping
  2. File upload + job submission + status polling via fal-client

fal-client reads its key from the FAL_KEY env var. We source ours from the
runtime env_config layer (`/admin/env` overrides .env) and write it into
os.environ just before each call so admin edits take effect immediately
without a restart.
"""
import os
from typing import Optional

from fastapi import HTTPException

from services.env_config import get_env

# ---------------------------------------------------------------------------
# Model catalog. Credit cost is paid at job submission; `1 credit = $0.01`
# retail, with margin built into each model's price tag. Tune freely.
# ---------------------------------------------------------------------------

MEDIA_MODEL_CATALOG = {
    "image": {
        "nano-banana-2": {
            "endpoint": "fal-ai/gemini-3.1-flash-image-preview/edit",
            "label": "Nano Banana 2 — consistent character (default)",
            "credits": 5,
            "supports_reference": True,
            "default": True,
        },
        "nano-banana-pro": {
            "endpoint": "fal-ai/gemini-3-pro-image-preview/edit",
            "label": "Nano Banana Pro — high fidelity",
            "credits": 15,
            "supports_reference": True,
            "default": False,
        },
        "flux-pro-ultra": {
            "endpoint": "fal-ai/flux-pro/v1.1-ultra",
            "label": "Flux 1.1 Pro Ultra — photoreal, no reference",
            "credits": 10,
            "supports_reference": False,
            "default": False,
        },
    },
    "video": {
        "kling-2.1-master": {
            "endpoint": "fal-ai/kling-video/v2.1/master/image-to-video",
            "label": "Kling 2.1 Master — premium image-to-video (default)",
            "credits": 250,
            "supports_reference": True,
            "default": True,
        },
        "kling-2.1-pro": {
            "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
            "label": "Kling 2.1 Pro — balanced",
            "credits": 150,
            "supports_reference": True,
            "default": False,
        },
        "veo-3.1": {
            "endpoint": "fal-ai/veo3.1/image-to-video",
            "label": "Veo 3.1 — top tier with native audio",
            "credits": 400,
            "supports_reference": True,
            "default": False,
        },
        "veo-3.1-fast": {
            "endpoint": "fal-ai/veo3.1/fast/image-to-video",
            "label": "Veo 3.1 Fast — quicker, cheaper",
            "credits": 200,
            "supports_reference": True,
            "default": False,
        },
    },
}


def catalog_for(kind: str) -> dict:
    if kind not in MEDIA_MODEL_CATALOG:
        raise ValueError(f"Unknown media kind: {kind!r}")
    return MEDIA_MODEL_CATALOG[kind]


def resolve_model(kind: str, model_key: Optional[str] = None) -> dict:
    """Returns the catalog entry for (kind, model_key). If model_key is None,
    returns the default for that kind."""
    options = catalog_for(kind)
    if model_key:
        if model_key not in options:
            raise HTTPException(status_code=400, detail=f"Unknown {kind} model: {model_key!r}")
        return {"key": model_key, **options[model_key]}
    default_key = next((k for k, v in options.items() if v.get("default")), None)
    if not default_key:
        raise HTTPException(status_code=500, detail=f"No default {kind} model configured")
    return {"key": default_key, **options[default_key]}


# ---------------------------------------------------------------------------
# fal-client wrappers
# ---------------------------------------------------------------------------

ACCEPTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # 12 MB ceiling for reference images


def _sync_fal_key() -> str:
    """Push the env_config FAL_API_KEY value into os.environ for fal-client.

    Done per-call so admin edits via /admin/env take effect on next request.
    Raises 503 if no key is set anywhere.
    """
    key = get_env("FAL_API_KEY", "")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="fal.ai isn't configured. Set FAL_API_KEY in /admin/env.",
        )
    os.environ["FAL_KEY"] = key
    return key


async def upload_bytes(content: bytes, content_type: str) -> str:
    """Upload raw bytes to fal's CDN; returns the resulting public URL."""
    if content_type not in ACCEPTED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type: {content_type}. "
                   f"Accepted: {', '.join(sorted(ACCEPTED_IMAGE_TYPES))}.",
        )
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image is too large ({len(content)} bytes). Max is {MAX_UPLOAD_BYTES // (1024*1024)} MB.",
        )

    _sync_fal_key()
    import fal_client  # local import — only loaded when fal is actually used

    try:
        # fal-client >= 0.5 exposes upload_async(data, content_type)
        url = await fal_client.upload_async(content, content_type)
    except AttributeError:
        # Older fal-client only had upload_file_async (path-based). Fall back.
        import tempfile
        import aiofiles  # noqa: F401  (we just need tempfile here)
        suffix = {
            "image/png": ".png", "image/jpeg": ".jpg",
            "image/webp": ".webp", "image/gif": ".gif",
        }.get(content_type, "")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            url = await fal_client.upload_file_async(tmp.name)
    return url


async def submit_job(endpoint: str, payload: dict) -> str:
    """Submit a generation job. Returns the fal request_id for polling/webhook."""
    _sync_fal_key()
    import fal_client
    handler = await fal_client.submit_async(endpoint, arguments=payload)
    return handler.request_id


async def get_job_status(endpoint: str, request_id: str) -> dict:
    """Return current status snapshot. fal-client returns either a Queued/InProgress/Completed object."""
    _sync_fal_key()
    import fal_client
    return await fal_client.status_async(endpoint, request_id, with_logs=False)


async def get_job_result(endpoint: str, request_id: str) -> dict:
    """Fetch the final result payload for a completed job."""
    _sync_fal_key()
    import fal_client
    return await fal_client.result_async(endpoint, request_id)
