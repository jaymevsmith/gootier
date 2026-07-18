import json
from typing import List, Optional

from anthropic import Anthropic

from services.env_config import get_env

DEFAULT_MODEL = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """You are a marketing campaign generator for the Gootier platform.

You receive a brand's marketing plan and desired schedule, then return a list of \
ready-to-publish campaign items (social posts and/or email blasts) that fit the plan.

Output strict JSON only — no prose, no markdown fences. Schema:
{
  "items": [
    {
      "kind": "social_post" | "email_blast",
      "scheduled_at": "ISO-8601 UTC timestamp or null",
      "content": "post body (social) or full HTML body (email)",
      "subject": "email subject (email_blast only, else null)",
      "link_url": "optional URL or null",
      "image_prompt": "concrete visual prompt for an image generator, OR null if no image suits this item",
      "video_prompt": "concrete motion+scene prompt for an image-to-video generator, OR null if no video is warranted",
      "suggested_asset_kind": "mascot | person | product | other — which reference asset works best for the image/video, OR null"
    }
  ]
}

Rules:
- Social posts: punchy, on-brand, platform-native voice. <= 280 chars unless platform allows more.
- Email blasts: full HTML body, mobile-friendly, single CTA, include subject line.
- Image prompts: be visual + concrete. Mention setting, lighting, mood, composition. Assume the brand's
  reference character/product image will be added as a reference at generation time — don't describe its
  appearance, just the scene it's in. Example: "studio backdrop with soft afternoon light, holding the
  bottle, candid smile, eye-level shot, shallow depth of field".
- Video prompts: describe motion + camera move + scene change. 5-10 seconds of content. Reserve for
  high-impact moments (launches, reveals, testimonials) — most items should have image_prompt only and
  video_prompt: null.
- Reserve suggested_asset_kind: "person" for testimonials/spokesperson moments, "product" for SKU shots
  and demos, "mascot" for brand-voice posts.
- Spread items across the requested schedule window. No two items at the same timestamp.
- If schedule is unspecified, use null for scheduled_at (immediate publish)."""


def _client() -> Anthropic:
    return Anthropic(api_key=get_env("ANTHROPIC_API_KEY", ""))


_VIDEO_PLANNER_SYSTEM = """You are a short-form video editor planning a 15-60s social media clip.

You receive (a) an ordered list of source clips the user picked, with each clip's
approximate duration and source model, and (b) a creative brief describing what
the finished video should feel like.  You return a structured plan that drives
our ffmpeg compose pipeline.

Output strict JSON only — no prose, no markdown fences.  Schema:
{
  "narrative": "1-2 sentence summary of the story this video tells.",
  "clip_options": [
    {
      "clip_index": 0,                 // 0-based, parallel to source_job_ids
      "crop_aspect": "9:16" | "1:1" | "4:5" | "16:9" | "4:3" | "3:4" | null,
      "reverse": false,
      "speed": 1.0,                    // 0.25..4.0 — slow-mo or fast-fwd
      "rationale": "why this transform fits the brief"
    }
  ],
  "transitions": [
    {
      "between": 0,                    // gap between clip 0 and clip 1
      "type": "" | "fade" | "fadeblack" | "fadewhite" | "slideleft" |
              "slideright" | "slideup" | "slidedown" | "wipeleft" |
              "wiperight" | "circleopen" | "circleclose" | "radial" |
              "pixelize" | "dissolve",
      "duration": 0.5,                 // 0.25, 0.5, 0.75, or 1.0
      "rationale": "why this transition"
    }
  ],
  "text_overlays": [
    {
      "clip_index": 0,
      "text": "Headline copy (keep short, < 40 chars per line)",
      "position": "top" | "center" | "bottom" |
                  "top-left" | "top-right" | "bottom-left" | "bottom-right",
      "start": 0.5,                    // seconds into clip
      "duration": 2.5,
      "fontsize": 72,                  // 48..128, big and legible on mobile
      "color": "white"                 // "white" | "#hex" — sit on a dark area
    }
  ],
  "music_prompt": "string — describe music in plain English (genre, BPM, mood, instruments) OR null"
}

Rules:
- clip_options MUST have exactly one entry per source clip, in order.
- transitions MUST have len = (number of clips - 1).  Use type:"" for a hard cut.
- Default to crop_aspect: "9:16" unless the brief implies a different aspect.
- Use reverse and >1.5× / <0.75× speeds sparingly — they're impactful moments,
  not the baseline.  Most clips should be speed: 1.0 with no reverse.
- text_overlays should land on dramatic beats — opening, key reveal, closing CTA.
  Max ~3 overlays total for a 30s video.  Don't overlap two overlays on the same clip.
- Keep music_prompt concrete and short: "120 BPM lo-fi hip-hop with mellow piano,
  upbeat but laid-back" beats "happy music".  Return null if the brief explicitly
  says no music or the clips already carry their own audio narrative.
- Every rationale field should be 1 short sentence — used for explainability."""


def plan_video_compose(brief: str, clip_meta: List[dict],
                        want_music: bool = True, want_text: bool = True,
                        style: Optional[str] = None,
                        model: str = DEFAULT_MODEL) -> dict:
    """Generate a structured video-composition plan from a brief + clip list.

    clip_meta is a list parallel to source_job_ids, each entry:
        {"clip_index": int, "duration_seconds": float, "model_key": str,
         "prompt": str (the original gen prompt, may be truncated)}
    """
    lines = [f"Style hint: {style or 'no preference — match the brief.'}"]
    lines.append(f"Add background music: {'yes' if want_music else 'no — return music_prompt: null'}")
    lines.append(f"Add on-screen text overlays: {'yes' if want_text else 'no — return text_overlays: []'}")
    lines.append("")
    lines.append("Source clips (in order):")
    for c in clip_meta:
        prompt_snip = (c.get("prompt") or "").replace("\n", " ")[:140]
        lines.append(
            f"  - clip {c['clip_index']}: ~{float(c.get('duration_seconds') or 5):.0f}s "
            f"(model: {c.get('model_key') or 'unknown'})"
            + (f" — original gen prompt: \"{prompt_snip}\"" if prompt_snip else "")
        )
    lines.append("")
    lines.append("Creative brief:")
    lines.append((brief or "").strip())
    lines.append("")
    lines.append("Generate the plan now.")
    user_msg = "\n".join(lines)

    response = _client().messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": _VIDEO_PLANNER_SYSTEM}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        plan = json.loads(cleaned)
    return {**plan, "_usage": usage}


def generate_campaign(plan: str, schedule: str = "", count: int = 5,
                       channels: Optional[List[str]] = None,
                       model: str = DEFAULT_MODEL) -> dict:
    """Generate `count` campaign items from a marketing plan + schedule.

    The plan/schedule is sent with cache_control so subsequent generations against
    the same plan hit the cache and run faster + cheaper.
    """
    channels = channels or ["social_post", "email_blast"]
    user_msg = (
        f"Channels allowed: {', '.join(channels)}\n"
        f"Number of items to generate: {count}\n"
        f"Schedule window / cadence: {schedule or 'unspecified'}\n\n"
        f"Generate the campaign now."
    )

    response = _client().messages.create(
        model=model,
        max_tokens=4096,
        system=[
            {"type": "text", "text": _SYSTEM_PROMPT},
            {
                "type": "text",
                "text": f"Brand marketing plan:\n\n{plan}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Strip markdown fences if model added them despite instructions
        cleaned = text.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
