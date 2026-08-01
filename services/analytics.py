"""Per-platform engagement metrics fetchers.

Each fetcher takes the SocialConnection used to publish the post and the
platform-specific post id, and returns a normalised dict:

    {
      "platform": "facebook",
      "fetched_at": "...iso...",
      "impressions": 0,
      "reach": 0,
      "engagement": 0,           # likes+comments+shares+reactions
      "likes": 0,
      "comments": 0,
      "shares": 0,
      "video_views": 0,           # 0 if N/A
      "raw": {...platform response...},
    }
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from models import MediaJob, SocialConnection, SocialPost  # noqa: F401

logger = logging.getLogger("gootier.analytics")
GRAPH_API = "https://graph.facebook.com/v19.0"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


async def fetch_facebook_metrics(conn: SocialConnection, post_id: str) -> Optional[Dict]:
    """Insights on a Facebook Page post — impressions, engaged users, reactions."""
    params = {
        "access_token": conn.access_token,
        "metric": "post_impressions,post_engaged_users,post_reactions_by_type_total",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{GRAPH_API}/{post_id}/insights", params=params)
            if resp.status_code >= 400:
                return None
            data = resp.json().get("data") or []
            out = {
                "platform": "facebook", "fetched_at": _now_iso(),
                "impressions": 0, "reach": 0, "engagement": 0,
                "likes": 0, "comments": 0, "shares": 0, "video_views": 0,
                "raw": data,
            }
            for metric in data:
                name = metric.get("name")
                values = metric.get("values") or []
                if not values:
                    continue
                val = values[0].get("value")
                if name == "post_impressions" and isinstance(val, int):
                    out["impressions"] = val
                elif name == "post_engaged_users" and isinstance(val, int):
                    out["engagement"] = val
                elif name == "post_reactions_by_type_total" and isinstance(val, dict):
                    out["likes"] = sum(int(v) for v in val.values() if isinstance(v, int))
            return out
    except Exception as e:
        logger.warning("FB metrics fetch failed for %s: %s", post_id, e)
        return None


async def fetch_instagram_metrics(conn: SocialConnection, media_id: str) -> Optional[Dict]:
    params = {
        "access_token": conn.access_token,
        "metric": "impressions,reach,likes,comments,saved,shares",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{GRAPH_API}/{media_id}/insights", params=params)
            if resp.status_code >= 400:
                return None
            data = resp.json().get("data") or []
            out = {
                "platform": "instagram", "fetched_at": _now_iso(),
                "impressions": 0, "reach": 0, "engagement": 0,
                "likes": 0, "comments": 0, "shares": 0, "video_views": 0,
                "raw": data,
            }
            for metric in data:
                name = metric.get("name")
                values = metric.get("values") or []
                val = values[0].get("value") if values else 0
                if isinstance(val, int):
                    if name in out:
                        out[name] = val
            out["engagement"] = out["likes"] + out["comments"] + out["shares"]
            return out
    except Exception as e:
        logger.warning("IG metrics fetch failed for %s: %s", media_id, e)
        return None


async def fetch_linkedin_metrics(conn: SocialConnection, ugc_urn: str) -> Optional[Dict]:
    """LinkedIn /v2/socialActions/{share-urn} returns counts."""
    if not ugc_urn:
        return None
    headers = {
        "Authorization": f"Bearer {conn.access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"https://api.linkedin.com/v2/socialActions/{ugc_urn}",
                headers=headers,
            )
            if resp.status_code >= 400:
                return None
            data = resp.json() or {}
            likes = int((data.get("likesSummary") or {}).get("totalLikes", 0) or 0)
            comments = int((data.get("commentsSummary") or {}).get("aggregatedTotalComments", 0) or 0)
            return {
                "platform": "linkedin", "fetched_at": _now_iso(),
                "impressions": 0, "reach": 0,
                "engagement": likes + comments,
                "likes": likes, "comments": comments, "shares": 0, "video_views": 0,
                "raw": data,
            }
    except Exception as e:
        logger.warning("LinkedIn metrics fetch failed for %s: %s", ugc_urn, e)
        return None


async def fetch_post_metrics(db: Session, post: SocialPost) -> Dict:
    """Iterate the per-connection publish_results, hit each platform's insights
    endpoint, return {connection_id: metrics-dict}.

    Stores the result on the post and returns it for the caller to render."""
    if not post.publish_results:
        return {}
    try:
        results = json.loads(post.publish_results) or {}
    except Exception:
        return {}

    conn_ids = [int(k) for k in results.keys() if str(k).isdigit()]
    conns = {c.id: c for c in db.query(SocialConnection).filter(
        SocialConnection.id.in_(conn_ids),
        SocialConnection.user_id == post.user_id,
    ).all()}

    out: Dict[str, Dict] = {}
    for conn_id_str, publish in results.items():
        if not isinstance(publish, dict) or not publish.get("success"):
            continue
        post_id = publish.get("post_id")
        if not post_id:
            continue
        conn = conns.get(int(conn_id_str)) if str(conn_id_str).isdigit() else None
        if not conn:
            continue
        if conn.platform == "facebook":
            metrics = await fetch_facebook_metrics(conn, post_id)
        elif conn.platform == "instagram":
            metrics = await fetch_instagram_metrics(conn, post_id)
        elif conn.platform == "linkedin":
            metrics = await fetch_linkedin_metrics(conn, post_id)
        else:
            metrics = None
        if metrics:
            out[str(conn_id_str)] = metrics

    if out:
        post.analytics_json = json.dumps(out)
        post.analytics_fetched_at = datetime.utcnow()
        db.commit()
    return out


def summarise_metrics(posts: List[SocialPost]) -> Dict:
    """Aggregate impressions / reach / engagement across a list of posts."""
    totals = {"impressions": 0, "reach": 0, "engagement": 0,
              "likes": 0, "comments": 0, "shares": 0, "posts_with_data": 0}
    by_platform: Dict[str, Dict] = {}
    for p in posts:
        if not p.analytics_json:
            continue
        try:
            data = json.loads(p.analytics_json)
        except Exception:
            continue
        if not data:
            continue
        totals["posts_with_data"] += 1
        for conn_id, metrics in data.items():
            plat = metrics.get("platform", "other")
            slot = by_platform.setdefault(plat, {
                "impressions": 0, "reach": 0, "engagement": 0,
                "likes": 0, "comments": 0, "shares": 0,
            })
            for k in ("impressions", "reach", "engagement", "likes", "comments", "shares"):
                v = int(metrics.get(k, 0) or 0)
                totals[k] += v
                slot[k] += v
    return {"totals": totals, "by_platform": by_platform}
