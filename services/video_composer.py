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


def _build_atempo_chain(speed: float) -> List[str]:
    """ffmpeg's `atempo` filter only accepts 0.5..2.0 in a single instance;
    chain multiple instances to hit more extreme tempos.  Returns a list of
    filter clauses ready to be joined with commas (empty list if speed==1.0)."""
    s = max(0.25, min(4.0, float(speed)))
    if abs(s - 1.0) < 1e-3:
        return []
    parts: List[str] = []
    remaining = s
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-3:
        parts.append(f"atempo={remaining:.4f}")
    return parts


async def _normalise_clip(src: str, dest: str, target_w: int = 1080,
                            target_h: int = 1920,
                            crop_aspect: Optional[str] = None,
                            reverse: bool = False,
                            speed: float = 1.0) -> None:
    """Re-encode a clip to a known-good baseline so the concat filter is happy.
    Vertical 1080x1920 by default — that's the Reels / TikTok / Shorts native
    aspect. ffmpeg pads or crops as needed via scale + setsar + pad.

    If the source has no audio, add a silent stereo track so concat doesn't
    barf on mismatched stream counts.

    Optional per-clip transforms:
      - crop_aspect ("9:16" | "1:1" | "16:9" | "4:5"): centre-crop to this
        aspect before scaling to target dims (instead of letterboxing).
      - reverse: play the clip backwards (video + audio).
      - speed: playback rate multiplier; 0.5 = slow-mo, 2.0 = fast-fwd, etc.
        Clamped to 0.25..4.0.  Audio retimed via atempo (chained when needed).
    """
    has_audio = await _has_audio_stream(src)

    # ---------- video filter chain ----------
    vf_chain: List[str] = []
    if crop_aspect:
        try:
            ax_s, ay_s = crop_aspect.split(":")
            ax, ay = int(ax_s), int(ay_s)
            if ax <= 0 or ay <= 0:
                raise ValueError
        except Exception:
            ax, ay = 9, 16
        # Centre-crop the largest rectangle of source that matches the
        # requested aspect ratio, then scale exactly to target dims.
        vf_chain.append(
            f"crop='min(iw,ih*{ax}/{ay})':'min(ih,iw*{ay}/{ax})':"
            f"'(iw-min(iw,ih*{ax}/{ay}))/2':'(ih-min(ih,iw*{ay}/{ax}))/2'"
        )
        vf_chain.append(f"scale={target_w}:{target_h}")
    else:
        vf_chain.append(
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease"
        )
        vf_chain.append(
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black"
        )
    vf_chain.append("setsar=1")
    if reverse:
        vf_chain.append("reverse")
    if speed and abs(float(speed) - 1.0) > 1e-3:
        s = max(0.25, min(4.0, float(speed)))
        # Higher PTS divisor → faster playback (frames closer together).
        vf_chain.append(f"setpts=PTS/{s}")
    vf_chain.append("fps=30")
    vf = ",".join(vf_chain)

    # ---------- audio filter chain ----------
    af_chain: List[str] = []
    if reverse:
        af_chain.append("areverse")
    af_chain.extend(_build_atempo_chain(speed))

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
    ]
    if af_chain:
        common += ["-af", ",".join(af_chain)]
    common += [
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


async def _concat_with_transitions(normalised_paths: List[str],
                                    durations: List[float],
                                    transitions: List[dict],
                                    dest: str,
                                    max_seconds: int = MAX_TOTAL_SECONDS) -> None:
    """Chain N clips together using ffmpeg's ``xfade`` (video) and
    ``acrossfade`` (audio), one transition per gap.

    ``transitions`` is parallel to the gaps — length N-1.  Each entry is
    ``{"type": str, "duration": float}``; ``type=""`` (or missing) falls back
    to a concat-style hard cut for that gap.

    Each xfade ``offset`` is the time IN THE RUNNING VIDEO where the
    transition begins, i.e. ``cumulative_duration_so_far - transition_duration``.
    Track cumulative duration as we chain.
    """
    n = len(normalised_paths)
    if n != len(durations):
        raise ValueError("durations must be parallel to normalised_paths")
    if n == 1:
        # Nothing to chain — caller should have routed elsewhere.
        await _run(
            "ffmpeg", "-y", "-i", normalised_paths[0],
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            "-t", str(max_seconds),
            dest,
        )
        return

    inputs: List[str] = []
    for p in normalised_paths:
        inputs += ["-i", p]

    parts: List[str] = []
    cum = float(durations[0])
    cur_v = "0:v"
    cur_a = "0:a"
    for i in range(n - 1):
        nxt_v_label = f"v{i+1}"
        nxt_a_label = f"a{i+1}"
        t = transitions[i] if transitions and i < len(transitions) else None
        ttype = (t or {}).get("type") or ""
        tdur = max(0.1, min(2.0, float((t or {}).get("duration") or 0.5)))
        if ttype:
            # offset = where in the running timeline the transition starts.
            offset = max(0.05, cum - tdur)
            parts.append(
                f"[{cur_v}][{i+1}:v]xfade=transition={ttype}:"
                f"duration={tdur}:offset={offset}[{nxt_v_label}]"
            )
            parts.append(
                f"[{cur_a}][{i+1}:a]acrossfade=d={tdur}[{nxt_a_label}]"
            )
            cum = cum + float(durations[i+1]) - tdur
        else:
            # Hard cut — concat the two streams.
            parts.append(
                f"[{cur_v}][{i+1}:v]concat=n=2:v=1:a=0[{nxt_v_label}]"
            )
            parts.append(
                f"[{cur_a}][{i+1}:a]concat=n=2:v=0:a=1[{nxt_a_label}]"
            )
            cum = cum + float(durations[i+1])
        cur_v = nxt_v_label
        cur_a = nxt_a_label

    await _run(
        *(["ffmpeg", "-y"] + inputs),
        "-filter_complex", ";".join(parts),
        "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
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
                    max_seconds: int = MAX_TOTAL_SECONDS,
                    clip_options: Optional[List[dict]] = None,
                    transitions: Optional[List[dict]] = None) -> str:
    """Top-level entry: download → normalise → concat → mix → upload.
    Returns the fal CDN URL of the result.

    clip_options (optional) is a list parallel to clip_urls.  Each entry can
    contain ``crop_aspect`` (e.g. "9:16"), ``reverse`` (bool), and ``speed``
    (float, 0.25..4.0).  Missing entries / keys leave the clip untransformed.

    transitions (optional) is a list parallel to the gaps between clips —
    length len(clip_urls) - 1.  Each entry is ``{"type": str, "duration": float}``;
    ``type=""`` falls back to a hard cut for that gap.  When any transition
    has a non-empty type, the compose pipeline switches to the xfade /
    acrossfade chain path instead of plain concat.
    """
    if not clip_urls:
        raise ValueError("Need at least one source clip to compose.")

    # Pad / clamp clip_options so every clip has a (possibly empty) dict.
    opts_list: List[dict] = list(clip_options or [])
    while len(opts_list) < len(clip_urls):
        opts_list.append({})
    opts_list = opts_list[: len(clip_urls)]

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
            opt = opts_list[i] or {}
            await _normalise_clip(
                src, dest,
                crop_aspect=opt.get("crop_aspect"),
                reverse=bool(opt.get("reverse")),
                speed=float(opt.get("speed") or 1.0),
            )
            normalised.append(dest)

        concat_out = os.path.join(work, "concat.mp4")
        # If any transition specifies a non-empty type, route to the xfade
        # chain path; otherwise the cheaper plain-concat path is fine.
        has_real_transition = bool(transitions) and any(
            (t or {}).get("type") for t in transitions
        )
        if has_real_transition and len(normalised) >= 2:
            durations = [await _probe_duration(p) for p in normalised]
            await _concat_with_transitions(
                normalised, durations, transitions or [], concat_out,
                max_seconds=max_seconds,
            )
        else:
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
