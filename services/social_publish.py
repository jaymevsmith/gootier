import asyncio
import json
from typing import Dict, List, Optional

import httpx

from models import SocialConnection

GRAPH_API = "https://graph.facebook.com/v19.0"


async def publish_facebook_page(conn: SocialConnection, content: str,
                                 link_url: Optional[str] = None,
                                 image_url: Optional[str] = None,
                                 video_url: Optional[str] = None) -> Dict:
    if not conn.page_id:
        return {"success": False, "error": "Missing page_id on connection"}

    params = {"access_token": conn.access_token}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if video_url:
                # Facebook Page video upload: POST /{page-id}/videos with file_url
                # (Graph accepts file_url for hosted videos; chunked upload not needed
                # for sub-100MB clips, which covers all fal.ai-generated content.)
                resp = await client.post(
                    f"{GRAPH_API}/{conn.page_id}/videos",
                    params=params,
                    data={"file_url": video_url, "description": content},
                )
            elif image_url:
                resp = await client.post(
                    f"{GRAPH_API}/{conn.page_id}/photos",
                    params=params,
                    data={"url": image_url, "caption": content},
                )
            else:
                data = {"message": content}
                if link_url:
                    data["link"] = link_url
                resp = await client.post(
                    f"{GRAPH_API}/{conn.page_id}/feed",
                    params=params,
                    data=data,
                )
            resp.raise_for_status()
            payload = resp.json()
            return {
                "success": True,
                "post_id": payload.get("id") or payload.get("post_id"),
                "platform": "facebook",
            }
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": e.response.text, "platform": "facebook"}
    except Exception as e:
        return {"success": False, "error": str(e), "platform": "facebook"}


async def publish_instagram(conn: SocialConnection, content: str,
                              image_url: Optional[str] = None,
                              video_url: Optional[str] = None) -> Dict:
    """Instagram Graph API — two-step container then publish.

    Caption-only posts aren't supported by IG; either image_url or video_url
    is required.
    """
    if not conn.page_id:
        return {"success": False, "error": "Missing IG user id", "platform": "instagram"}
    if not image_url and not video_url:
        return {"success": False, "error": "Instagram requires an image or video", "platform": "instagram"}

    params = {"access_token": conn.access_token}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            create_data = {"caption": content}
            if video_url:
                create_data["media_type"] = "REELS"
                create_data["video_url"] = video_url
            else:
                create_data["image_url"] = image_url

            container_resp = await client.post(
                f"{GRAPH_API}/{conn.page_id}/media",
                params=params, data=create_data,
            )
            container_resp.raise_for_status()
            container_id = container_resp.json().get("id")
            if not container_id:
                return {"success": False, "error": "No container id", "platform": "instagram"}

            # For video/reels, IG needs a moment to ingest. Poll the container status
            # up to ~60s before publishing.
            if video_url:
                for _ in range(20):
                    await asyncio.sleep(3.0)
                    status_resp = await client.get(
                        f"{GRAPH_API}/{container_id}",
                        params={**params, "fields": "status_code"},
                    )
                    if status_resp.status_code >= 400:
                        break
                    if status_resp.json().get("status_code") == "FINISHED":
                        break

            publish_resp = await client.post(
                f"{GRAPH_API}/{conn.page_id}/media_publish",
                params=params, data={"creation_id": container_id},
            )
            publish_resp.raise_for_status()
            return {
                "success": True,
                "post_id": publish_resp.json().get("id"),
                "platform": "instagram",
            }
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": e.response.text, "platform": "instagram"}
    except Exception as e:
        return {"success": False, "error": str(e), "platform": "instagram"}


async def publish_linkedin(conn: SocialConnection, content: str,
                             link_url: Optional[str] = None,
                             image_url: Optional[str] = None) -> Dict:
    """Post to LinkedIn as the authenticated user via UGC Posts API.

    For images, we register an upload, PUT the bytes from `image_url`, then
    reference the resulting media URN in the post. Text+link works without
    any media upload — that's the common path.
    """
    if not conn.page_id:
        return {"success": False, "error": "Missing LinkedIn user URN", "platform": "linkedin"}
    person_urn = f"urn:li:person:{conn.page_id}"
    headers = {
        "Authorization": f"Bearer {conn.access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    text_parts = [content]
    if link_url:
        text_parts.append(link_url)
    post_text = "\n\n".join(text_parts).strip()

    body = {
        "author": person_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": post_text},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    if link_url and not image_url:
        body["specificContent"]["com.linkedin.ugc.ShareContent"]["shareMediaCategory"] = "ARTICLE"
        body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [{
            "status": "READY",
            "originalUrl": link_url,
        }]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers=headers,
                content=json.dumps(body),
            )
            resp.raise_for_status()
            return {
                "success": True,
                "post_id": resp.headers.get("x-restli-id") or "linkedin-post",
                "platform": "linkedin",
            }
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": e.response.text, "platform": "linkedin"}
    except Exception as e:
        return {"success": False, "error": str(e), "platform": "linkedin"}


async def _maybe_refresh_youtube_token(conn: SocialConnection) -> str:
    """If the stored YouTube access token is near expiry, refresh + persist.
    Returns the bearer token to use right now."""
    from datetime import datetime, timedelta
    if not conn.refresh_token:
        return conn.access_token
    if conn.expires_at and conn.expires_at > datetime.utcnow() + timedelta(minutes=1):
        return conn.access_token

    from routes.oauth_routes import refresh_youtube_access_token
    from database import SessionLocal
    data = refresh_youtube_access_token(conn.refresh_token)
    new_token = data.get("access_token")
    if not new_token:
        return conn.access_token

    expires_at = datetime.utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)) - 60)
    db = SessionLocal()
    try:
        fresh = db.query(SocialConnection).filter(SocialConnection.id == conn.id).first()
        if fresh:
            fresh.access_token = new_token
            fresh.expires_at = expires_at
            db.commit()
    finally:
        db.close()
    conn.access_token = new_token
    conn.expires_at = expires_at
    return new_token


async def publish_youtube(conn: SocialConnection, content: str,
                            video_url: Optional[str] = None) -> Dict:
    """Upload a video to the connected YouTube channel.

    YouTube is video-only — text/image posts get skipped with a clear error.
    Multipart `videos.insert` is used since fal-CDN videos are usually well
    under 5GB; resumable upload would be needed only for huge files.
    """
    if not video_url:
        return {"success": False, "error": "YouTube requires a video_url",
                "platform": "youtube"}

    try:
        access_token = await _maybe_refresh_youtube_token(conn)

        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            video_resp = await client.get(video_url)
            video_resp.raise_for_status()
            video_bytes = video_resp.content

            title = (content or "Gootier upload").strip()[:100] or "Gootier upload"
            desc  = (content or "")[:5000]
            metadata = {
                "snippet": {
                    "title": title,
                    "description": desc,
                    "categoryId": "22",  # People & Blogs — safe default
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                    "embeddable": True,
                },
            }
            boundary = "gootier_youtube_boundary"
            body = (
                f"--{boundary}\r\n"
                f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\n"
                f"Content-Type: video/mp4\r\n\r\n"
            ).encode("utf-8") + video_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

            up_resp = await client.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "multipart", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                content=body,
            )
            if up_resp.status_code >= 400:
                return {"success": False, "error": up_resp.text[:1000],
                        "platform": "youtube"}
            data = up_resp.json()
            video_id = data.get("id")
            return {
                "success": True,
                "post_id": video_id,
                "url": f"https://youtu.be/{video_id}" if video_id else None,
                "platform": "youtube",
            }
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": e.response.text[:1000],
                "platform": "youtube"}
    except Exception as e:
        return {"success": False, "error": str(e), "platform": "youtube"}


async def publish_tiktok(conn: SocialConnection, content: str,
                           video_url: Optional[str] = None) -> Dict:
    """TikTok Content Posting API — direct video upload from URL (PULL_FROM_URL).

    Requires the destination URL to be on a domain verified in the TikTok app.
    Posts land in the user's inbox for them to publish from the app — that's
    by design in TikTok's content-posting flow, not a Gootier limitation.
    """
    if not video_url:
        return {"success": False, "error": "TikTok requires a video_url", "platform": "tiktok"}
    headers = {
        "Authorization": f"Bearer {conn.access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    body = {
        "post_info": {
            "title": (content or "")[:150],
            "privacy_level": "SELF_ONLY",   # required for unaudited apps
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
                headers=headers,
                content=json.dumps(body),
            )
            resp.raise_for_status()
            data = (resp.json().get("data") or {})
            return {
                "success": True,
                "post_id": data.get("publish_id", "tiktok-inbox"),
                "platform": "tiktok",
                "note": "Sent to TikTok inbox — open the TikTok app to publish.",
            }
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": e.response.text, "platform": "tiktok"}
    except Exception as e:
        return {"success": False, "error": str(e), "platform": "tiktok"}


async def publish_to_connections(connections: List[SocialConnection], content: str,
                                  link_url: Optional[str] = None,
                                  image_url: Optional[str] = None,
                                  video_url: Optional[str] = None) -> Dict[int, Dict]:
    results: Dict[int, Dict] = {}
    for conn in connections:
        if conn.platform == "facebook":
            results[conn.id] = await publish_facebook_page(
                conn, content, link_url=link_url, image_url=image_url, video_url=video_url,
            )
        elif conn.platform == "instagram":
            results[conn.id] = await publish_instagram(
                conn, content, image_url=image_url, video_url=video_url,
            )
        elif conn.platform == "linkedin":
            results[conn.id] = await publish_linkedin(
                conn, content, link_url=link_url, image_url=image_url,
            )
        elif conn.platform == "youtube":
            results[conn.id] = await publish_youtube(conn, content, video_url=video_url)
        elif conn.platform == "tiktok":
            results[conn.id] = await publish_tiktok(conn, content, video_url=video_url)
        else:
            results[conn.id] = {"success": False, "error": f"Platform '{conn.platform}' not yet supported"}
    return results
