import hashlib
import hmac
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from auth import SECRET_KEY, get_current_user
from database import get_db
from models import SocialConnection, User, log_action
from services.env_config import get_env
from services.flash import set_flash
from services.quotas import current_usage, get_quota

router = APIRouter(prefix="/oauth")

# These resolve lazily so admin edits in /admin/env take effect on next request.
def _meta_app_id() -> str: return get_env("META_APP_ID", "")
def _meta_app_secret() -> str: return get_env("META_APP_SECRET", "")
def _meta_redirect() -> str:
    return get_env("META_OAUTH_REDIRECT", "http://localhost:8000/oauth/facebook/callback")

SCOPES = "pages_manage_posts,pages_read_engagement,pages_show_list"
DIALOG_URL = "https://www.facebook.com/v19.0/dialog/oauth"
TOKEN_URL = "https://graph.facebook.com/v19.0/oauth/access_token"
ACCOUNTS_URL = "https://graph.facebook.com/v19.0/me/accounts"


def _sign_state(user_id: int) -> str:
    """HMAC-signed state: `<user_id>:<nonce>:<sig>` — protects against CSRF
    and forged callbacks attaching pages to other users' accounts."""
    nonce = secrets.token_urlsafe(16)
    payload = f"{user_id}:{nonce}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"


def _verify_state(state: str, expected_user_id: int) -> bool:
    parts = (state or "").split(":")
    if len(parts) != 3:
        return False
    user_id_str, nonce, sig = parts
    if not user_id_str.isdigit() or int(user_id_str) != expected_user_id:
        return False
    payload = f"{user_id_str}:{nonce}"
    expected_sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, expected_sig)


@router.get("/facebook/start")
async def facebook_start(
    request: Request,
    user: User = Depends(get_current_user),
):
    app_id = _meta_app_id()
    if not app_id:
        raise HTTPException(status_code=503, detail="Facebook OAuth is not configured")

    params = {
        "client_id": app_id,
        "redirect_uri": _meta_redirect(),
        "scope": SCOPES,
        "response_type": "code",
        "state": _sign_state(user.id),
    }
    return RedirectResponse(url=f"{DIALOG_URL}?{urlencode(params)}", status_code=303)


@router.get("/facebook/callback")
async def facebook_callback(
    request: Request,
    code: str = "",
    state: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    if not _verify_state(state, user.id):
        raise HTTPException(status_code=400, detail="Invalid or forged OAuth state")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.get(TOKEN_URL, params={
            "client_id": _meta_app_id(),
            "client_secret": _meta_app_secret(),
            "redirect_uri": _meta_redirect(),
            "code": code,
        })
        token_resp.raise_for_status()
        user_token = token_resp.json().get("access_token")
        if not user_token:
            raise HTTPException(status_code=502, detail="No access_token from Meta")

        accounts_resp = await client.get(ACCOUNTS_URL, params={"access_token": user_token})
        accounts_resp.raise_for_status()
        pages = accounts_resp.json().get("data", []) or []

    if not pages:
        raise HTTPException(status_code=400, detail="No Facebook Pages found on this account")

    # Quota: how many NEW pages would this OAuth callback add?
    if not user.is_staff():
        existing_page_ids = {
            c.page_id for c in db.query(SocialConnection).filter(
                SocialConnection.user_id == user.id,
                SocialConnection.platform == "facebook",
                SocialConnection.is_active == True,  # noqa: E712
            ).all()
        }
        new_pages = [p for p in pages if p.get("id") not in existing_page_ids]
        if new_pages:
            cap = get_quota(db, user.tier, "social_connections")
            used = current_usage(db, user, "social_connections")
            if cap is not None and used + len(new_pages) > cap:
                raise HTTPException(
                    status_code=403,
                    detail=(f"Your {user.tier} plan allows {cap} connected account(s); "
                            f"you already have {used} and tried to add {len(new_pages)} more. "
                            f"Upgrade your plan or disconnect an account first."),
                )

    for page in pages:
        page_id = page.get("id")
        page_token = page.get("access_token")
        page_name = page.get("name") or page_id
        if not page_id or not page_token:
            continue
        existing = db.query(SocialConnection).filter(
            SocialConnection.user_id == user.id,
            SocialConnection.platform == "facebook",
            SocialConnection.page_id == page_id,
        ).first()
        if existing:
            existing.access_token = page_token
            existing.account_name = page_name
            existing.is_active = True
        else:
            db.add(SocialConnection(
                user_id=user.id,
                platform="facebook",
                account_name=page_name,
                access_token=page_token,
                page_id=page_id,
                is_active=True,
            ))
    db.commit()
    log_action(db, user, "CREATE", "SocialConnection",
               detail=f"Facebook OAuth — {len(pages)} page(s) connected")

    response = RedirectResponse(url="/connections", status_code=303)
    noun = "page" if len(pages) == 1 else "pages"
    set_flash(response, "success", f"Connected {len(pages)} Facebook {noun}.")
    return response
