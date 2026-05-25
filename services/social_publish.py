from typing import Dict, List, Optional

import httpx

from models import SocialConnection

GRAPH_API = "https://graph.facebook.com/v19.0"


async def publish_facebook_page(conn: SocialConnection, content: str,
                                 link_url: Optional[str] = None,
                                 image_url: Optional[str] = None) -> Dict:
    if not conn.page_id:
        return {"success": False, "error": "Missing page_id on connection"}

    params = {"access_token": conn.access_token}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if image_url:
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


async def publish_to_connections(connections: List[SocialConnection], content: str,
                                  link_url: Optional[str] = None,
                                  image_url: Optional[str] = None) -> Dict[int, Dict]:
    results: Dict[int, Dict] = {}
    for conn in connections:
        if conn.platform == "facebook":
            results[conn.id] = await publish_facebook_page(conn, content, link_url, image_url)
        else:
            results[conn.id] = {"success": False, "error": f"Platform '{conn.platform}' not yet supported"}
    return results
