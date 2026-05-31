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
