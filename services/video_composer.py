"""Server-side video composition via ffmpeg.

The /studio flow lets a user pick N clips from their video MediaJob history,
optionally add a TTS narration script and a music bed URL, and produce a
single composed video. This module is the orchestration:

  1. Download each source URL to a temp file via httpx (fal CDN URLs).
  2. Normalise to a single resolution/codec via ffmpeg `concat` filter
     (clips from different models / runs have different specs).
  3. If a narration script was provided, generate TTS via fal.ai and mix it
     in alongside the original audio (ducked) and optional music bed.
  4. Upload the result to fal's CDN and return the URL.

All ffmpeg work happens via asyncio subprocesses so the FastAPI worker
isn't blocked while a 60s video re-encodes.
"""
import asyncio
import logging
import os
import shutil
import tempfile
from typing import List, Optional

import httpx

logger = logging.getLogger("gootier.composer")

MAX_TOTAL_SECONDS = 60
DOWNLOAD_TIMEOUT = 60.0


async def _run(*args, **kwargs) -> str:
    """asyncio wrapper around ffmpeg / ffprobe subprocess invocation.
    Returns combined stdout+stderr as text on success, raises on non-zero."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = (stderr.decode("utf-8", errors="ignore")
               or stdout.decode("utf-8", errors="ignore")
               or f"exit {proc.returncode}")
        raise RuntimeError(f"{args[0]} failed: {msg.strip()[-800:]}")
    return (stdout.decode("utf-8", errors="ignore")
            + stderr.decode("utf-8", errors="ignore"))


async def _download(url: str, dest: str) -> None:
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1 << 20):
                    f.write(chunk)


async def _probe_duration(path: str) -> float:
    """Returns the media file's duration in seconds (0.0 on failure)."""
    try:
        out = await _run(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        )
        return float(out.strip().splitlines()[0])
    except Exception:
        return 0.0


async def _has_audio_stream(path: str) -> bool:
    """True if the file has at least one audio stream."""
    try:
        out = await _run(
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        )
        return bool(out.strip())
    except Exception:
        return False


async def _normalise_clip(src: str, dest: str, target_w: int = 1080,
                            target_h: int = 1920) -> None:
    """Re-encode a clip to a known-good baseline so the concat filter is happy.
    Vertical 1080x1920 by default — that's the Reels / TikTok / Shorts native
    aspect. ffmpeg pads or crops as needed via scale + setsar + pad.

    If the source has no audio, add a silent stereo track so concat doesn't
    barf on mismatched stream counts.
    """
    has_audio = await _has_audio_stream(src)

    vf = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
          f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
          "setsar=1,fps=30")

    common = [
        "ffmpeg", "-y",
        "-i", src,
    ]
    if not has_audio:
        # Inject a silent audio track via the lavfi anullsrc input.
        common += [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
        ]
    common += [
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        dest,
    ]
    await _run(*common)


async def _concat_clips(normalised_paths: List[str], dest: str,
                         max_seconds: int = MAX_TOTAL_SECONDS) -> None:
    """Concat-filter approach since the demuxer requires exact-matching streams.
    All inputs at this point are already normalised, but the filter is robust
    to small mismatches anyway."""
    inputs = []
    for p in normalised_paths:
        inputs += ["-i", p]

    # Build the filter graph: [0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[v][a]
    n = len(normalised_paths)
    parts = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    filter_complex = f"{parts}concat=n={n}:v=1:a=1[v][a]"

    await _run(
        *(["ffmpeg", "-y"] + inputs),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        "-t", str(max_seconds),
        dest,
    )


async def _mix_audio(video_path: str, dest_path: str,
                       narration_path: Optional[str] = None,
                       music_path: Optional[str] = None,
                       keep_original: bool = True) -> None:
    """Replace / mix the video's audio with optional narration + music bed."""
    if not narration_path and not music_path and keep_original:
        # Nothing to do — just copy through.
        shutil.copy(video_path, dest_path)
        return

    args = ["ffmpeg", "-y", "-i", video_path]
    audio_inputs = []  # filter labels in order
    if narration_path:
        args += ["-i", narration_path]
        audio_inputs.append("narr")
    if music_path:
        args += ["-i", music_path]
        audio_inputs.append("mus")

    # Build a filter that mixes any combination of:
    #   [0:a]volume=X[orig]  — original audio (ducked when narration present)
    #   [N:a]volume=Y[narr]  — narration on top
    #   [N:a]aloop[mus]      — music bed, looped + low
    parts = []
    mix_labels = []

    if keep_original:
        orig_vol = "0.15" if narration_path else ("0.6" if music_path else "1.0")
        parts.append(f"[0:a]volume={orig_vol}[orig]")
        mix_labels.append("[orig]")

    input_idx = 1
    if narration_path:
        parts.append(f"[{input_idx}:a]volume=1.0[narr]")
        mix_labels.append("[narr]")
        input_idx += 1
    if music_path:
        # Loop the bed so it covers the full video duration; duck low.
        parts.append(f"[{input_idx}:a]aloop=loop=-1:size=2e9,volume=0.18[mus]")
        mix_labels.append("[mus]")
        input_idx += 1

    if len(mix_labels) > 1:
        parts.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first[a]")
        out_label = "[a]"
    else:
        out_label = mix_labels[0]

    filter_complex = ";".join(parts)

    args += [
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", out_label,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        dest_path,
    ]
    await _run(*args)


async def compose(clip_urls: List[str],
                    narration_path: Optional[str] = None,
                    music_path: Optional[str] = None,
                    keep_original_audio: bool = True,
                    max_seconds: int = MAX_TOTAL_SECONDS) -> str:
    """Top-level entry: download → normalise → concat → mix → upload.
    Returns the fal CDN URL of the result.
    """
    if not clip_urls:
        raise ValueError("Need at least one source clip to compose.")

    work = tempfile.mkdtemp(prefix="gootier-compose-")
    try:
        local_clips = []
        for i, url in enumerate(clip_urls):
            local = os.path.join(work, f"src{i}.mp4")
            await _download(url, local)
            local_clips.append(local)

        normalised = []
        for i, src in enumerate(local_clips):
            dest = os.path.join(work, f"norm{i}.mp4")
            await _normalise_clip(src, dest)
            normalised.append(dest)

        concat_out = os.path.join(work, "concat.mp4")
        await _concat_clips(normalised, concat_out, max_seconds=max_seconds)

        mixed_out = os.path.join(work, "final.mp4")
        await _mix_audio(
            concat_out, mixed_out,
            narration_path=narration_path,
            music_path=music_path,
            keep_original=keep_original_audio,
        )

        # Upload via the existing fal helper (lazy import to avoid cycle)
        with open(mixed_out, "rb") as f:
            payload = f.read()
        from services.media import upload_bytes
        # fal accepts whatever mime; mp4 is video/mp4. Bypass the image-only
        # check in upload_bytes by calling fal-client directly.
        from services.media import _sync_fal_key
        _sync_fal_key()
        import fal_client
        url = await fal_client.upload_async(payload, "video/mp4")
        return url
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def synth_tts_to_file(text: str, voice_id: str, dest_path: str,
                              model_endpoint: str = "fal-ai/elevenlabs/tts/turbo-v2.5") -> float:
    """Generate TTS via fal, write the audio to dest_path, return duration."""
    from services.media import _sync_fal_key
    _sync_fal_key()
    import fal_client

    result = await fal_client.run_async(
        model_endpoint,
        arguments={"text": text, "voice": voice_id},
    )
    # ElevenLabs returns {audio: {url, ...}} typically.
    audio = (result or {}).get("audio") or {}
    audio_url = audio.get("url") if isinstance(audio, dict) else audio
    if not audio_url:
        raise RuntimeError(f"TTS produced no audio URL: {result!r}")
    await _download(audio_url, dest_path)
    return await _probe_duration(dest_path)
