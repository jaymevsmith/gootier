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

# Text-to-speech models live alongside image/video so the catalog API can
# surface them to the /studio narration picker.
TTS_MODEL_CATALOG = {
    "eleven-turbo": {
        "endpoint": "fal-ai/elevenlabs/tts/turbo-v2.5",
        "label": "ElevenLabs Turbo v2.5 — natural, fast (default)",
        "credits_per_100_chars": 5,
        "voices": [
            ("Rachel",   "21m00Tcm4TlvDq8ikWAM"),
            ("Bella",    "EXAVITQu4vr4xnSDxMaL"),
            ("Antoni",   "ErXwobaYiN019PkySvjV"),
            ("Domi",     "AZnzlk1XvdvUeBnXmlld"),
            ("Adam",     "pNInz6obpgDQGcFmaJgB"),
        ],
        "default": True,
    },
}


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


# Maps this catalog's short keys to the JTS Rate.model_key registered for
# them (see Jhome-Token-Service/scripts/register_gootier_rates.py).
#
# "stable-audio" is not part of MEDIA_MODEL_CATALOG/TTS_MODEL_CATALOG (music
# generation has a single fixed fal endpoint, no model picker) — it's a
# Gootier-internal key that exists purely to hang the JTS rate/price mapping
# off of, used consistently by estimate_tokens(), debit_after_success(), and
# the MediaJob.model_key column for /api/media/jobs/music.
JTS_RATE_KEY = {
    "nano-banana-2":   "fal-nano-banana-2",
    "nano-banana-pro": "fal-nano-banana-pro",
    "flux-pro-ultra":  "fal-flux-pro-ultra",
    "kling-2.1-master": "fal-kling-2.1-master",
    "kling-2.1-pro":    "fal-kling-2.1-pro",
    "veo-3.1":          "fal-veo-3.1",
    "veo-3.1-fast":     "fal-veo-3.1-fast",
    "eleven-turbo":     "fal-elevenlabs-tts-turbo",
    "stable-audio":     "fal-stable-audio-music",
}

# Real USD price per unit, mirrored from the same script — used only for the
# soft pre-flight balance estimate (never for the actual debit, which always
# reports real usage to JTS).
JTS_PRICE_PER_UNIT_USD = {
    "nano-banana-2":   0.08,
    "nano-banana-pro": 0.15,
    "flux-pro-ultra":  0.06,
    "kling-2.1-master": 0.28,
    "kling-2.1-pro":    0.098,
    "veo-3.1":          0.40,
    "veo-3.1-fast":     0.15,
    "eleven-turbo":     0.00005,
    "stable-audio":     0.00125,
}


def estimate_tokens(model_key: str, units: float = 1) -> int:
    """USD cost estimate -> raw JTS tokens (1 token = $0.000001), for the
    soft pre-flight balance check only."""
    usd = JTS_PRICE_PER_UNIT_USD[model_key] * units
    return int(usd * 1_000_000)


def catalog_for(kind: str) -> dict:
    if kind not in MEDIA_MODEL_CATALOG:
        raise ValueError(f"Unknown media kind: {kind!r}")
    return MEDIA_MODEL_CATALOG[kind]


def tts_catalog() -> dict:
    return TTS_MODEL_CATALOG


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


async def generate_music(prompt: str, seconds: int = 30) -> str:
    """Generate an instrumental music clip via fal stable-audio.

    Used by the AI studio sequencer to back a composed video with a music
    bed it can describe in plain English ("upbeat synth, 110 bpm, building
    energy").  Returns the fal CDN URL of the rendered audio.

    Costs ~$0.05 per render — caller is responsible for charging credits
    via the standard MediaJob path.
    """
    _sync_fal_key()
    import fal_client  # noqa: F401 — populates the cache
    seconds = max(5, min(60, int(seconds or 30)))
    result = await fal_client.run_async(
        "fal-ai/stable-audio",
        arguments={
            "prompt": (prompt or "").strip()[:800] or "ambient instrumental, gentle, cinematic",
            "seconds_total": seconds,
        },
    )
    audio = (result or {}).get("audio_file") or (result or {}).get("audio") or {}
    if isinstance(audio, dict):
        return audio.get("url") or ""
    return audio or ""


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


async def submit_job(endpoint: str, payload: dict, webhook_url: Optional[str] = None) -> str:
    """Submit a generation job. Returns the fal request_id.

    When `webhook_url` is provided, fal will POST the completion event to it,
    eliminating the polling lag. The polling endpoint still works as a fallback
    for clients that want to render progress in the UI.
    """
    _sync_fal_key()
    import fal_client
    kwargs = {"arguments": payload}
    if webhook_url:
        kwargs["webhook_url"] = webhook_url
    handler = await fal_client.submit_async(endpoint, **kwargs)
    return handler.request_id


async def fetch_status(endpoint: str, request_id: str):
    """Return current status object (InQueue / InProgress / Completed). Raises on transport errors."""
    _sync_fal_key()
    import fal_client
    return await fal_client.status_async(endpoint, request_id, with_logs=False)


async def fetch_result(endpoint: str, request_id: str) -> dict:
    """Fetch the final result payload for a completed job."""
    _sync_fal_key()
    import fal_client
    return await fal_client.result_async(endpoint, request_id)


def _status_phase(status_obj) -> str:
    """Map fal-client status objects to our four phases: queued | running | done | failed."""
    name = type(status_obj).__name__
    if name == "Completed":
        return "done"
    if name == "InProgress":
        return "running"
    if name == "InQueue":
        return "queued"
    return "running"  # unknown → assume still working


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def build_image_payload(model: dict, prompt: str, ref_urls: list,
                          aspect_ratio: str = "auto",
                          resolution: str = "1K",
                          num_images: int = 1) -> dict:
    """Construct the fal payload for an image-gen model. Schema differs per model:
       - Gemini 3.x flash/pro `image_urls` array (required) for the /edit endpoint
       - Flux 1.1 Pro Ultra `image_url` singular (optional, treated as style prompt)
    """
    endpoint = model["endpoint"]
    if "gemini-3" in endpoint and endpoint.endswith("/edit"):
        if not ref_urls:
            raise ValueError("Nano-Banana edit endpoints require at least one reference image_urls entry.")
        return {
            "prompt": prompt,
            "image_urls": ref_urls,
            "aspect_ratio": aspect_ratio or "auto",
            "resolution": resolution or "1K",
            "num_images": num_images,
            "output_format": "png",
            "limit_generations": True,
        }
    if "flux-pro/v1.1-ultra" in endpoint:
        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio if aspect_ratio and aspect_ratio != "auto" else "16:9",
            "num_images": num_images,
            "output_format": "jpeg",
        }
        # Flux Pro Ultra accepts a single optional `image_url` for style prompting.
        if ref_urls:
            payload["image_url"] = ref_urls[0]
            payload["image_prompt_strength"] = 0.45
        return payload
    raise ValueError(f"No payload builder configured for endpoint: {endpoint}")


def extract_first_image_url(result: dict) -> str:
    """Both Gemini and Flux return `images: [{url, ...}, ...]`. Pull the first URL."""
    images = (result or {}).get("images") or []
    if not images:
        raise ValueError("Result payload contained no images")
    first = images[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        url = first.get("url") or first.get("image_url")
        if not url:
            raise ValueError(f"Image entry missing url: {first}")
        return url
    raise ValueError(f"Unrecognised image entry shape: {type(first)}")


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------

# Each video model accepts a different `duration` shape. Normalize from a UI
# integer (5, 6, 8, 10) into the literal the model wants.
def _normalize_video_duration(endpoint: str, seconds: int) -> str:
    if "kling-video" in endpoint:
        # Kling 2.1: enum is "5" or "10"
        return "10" if seconds >= 10 else "5"
    if "veo3.1" in endpoint:
        # Veo 3.1: enum is "4s" / "6s" / "8s"
        if seconds >= 8:
            return "8s"
        if seconds >= 6:
            return "6s"
        return "4s"
    return str(seconds)


def billed_video_seconds(endpoint: str, requested_seconds: int) -> float:
    """The actual duration fal will generate/bill for, after bucketing —
    use this for BOTH the pre-flight token estimate and the real debit
    `units`, never the raw user-requested duration (fal's enum buckets
    don't match arbitrary requested values)."""
    bucketed = _normalize_video_duration(endpoint, requested_seconds)
    return float(bucketed.rstrip("s"))


def build_video_payload(model: dict, prompt: str, ref_url: str,
                          duration_seconds: int = 5,
                          aspect_ratio: str = "auto",
                          resolution: str = "720p",
                          generate_audio: bool = True) -> dict:
    """Construct the fal payload for an image-to-video model.

    All shipping defaults are image-to-video (Kling 2.1 + Veo 3.1), which all
    take a single `image_url` + `prompt`. Model-specific extras get added
    based on endpoint.
    """
    endpoint = model["endpoint"]
    if not ref_url:
        raise ValueError("Image-to-video models require a reference image.")

    base = {
        "image_url": ref_url,
        "prompt": prompt,
        "duration": _normalize_video_duration(endpoint, duration_seconds),
    }

    if "kling-video" in endpoint:
        # Kling has no aspect_ratio parameter (uses input image as-is) and
        # no audio. cfg_scale leaves default.
        return base

    if "veo3.1" in endpoint:
        if aspect_ratio not in ("auto", "16:9", "9:16"):
            aspect_ratio = "auto"
        if resolution not in ("720p", "1080p", "4k"):
            resolution = "720p"
        return {
            **base,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "generate_audio": bool(generate_audio),
        }

    return base


def extract_video_url(result: dict) -> str:
    """fal video models return `{video: {url, ...}}` or sometimes a bare URL string."""
    video = (result or {}).get("video")
    if not video:
        raise ValueError("Result payload contained no video")
    if isinstance(video, str):
        return video
    if isinstance(video, dict):
        url = video.get("url") or video.get("video_url")
        if not url:
            raise ValueError(f"Video entry missing url: {video}")
        return url
    raise ValueError(f"Unrecognised video entry shape: {type(video)}")
