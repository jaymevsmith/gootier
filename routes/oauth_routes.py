"""OAuth-based social-account connections.

One connect-via-login flow per platform. State is HMAC-signed and binds the
authenticated session to the callback. PKCE-using flows (X, TikTok) carry
the code_verifier inside the state so we don't need a session store.
"""
import base64
import hashlib
import hmac
import secrets
from typing import Optional
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


# --------------------------------------------------------------------------- #
# State + PKCE helpers (HMAC-signed; verifier embedded for PKCE flows)
# --------------------------------------------------------------------------- #

def _sign_state(user_id: int, verifier: str = "") -> str:
    """State = `<user_id>:<nonce>:<verifier_b64>:<sig>`. verifier may be empty."""
    nonce = secrets.token_urlsafe(16)
    vb = base64.urlsafe_b64encode(verifier.encode()).decode().rstrip("=") if verifier else ""
    payload = f"{user_id}:{nonce}:{vb}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"


def _verify_state(state: str, expected_user_id: int) -> Optional[str]:
    """Returns the PKCE verifier (empty string if none) on success, or None if invalid."""
    parts = (state or "").split(":")
    if len(parts) != 4:
        return None
    user_id_str, nonce, vb, sig = parts
    if not user_id_str.isdigit() or int(user_id_str) != expected_user_id:
        return None
    payload = f"{user_id_str}:{nonce}:{vb}"
    expected_sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected_sig):
        return None
    if not vb:
        return ""
    try:
        # urlsafe_b64decode requires correct padding
        padded = vb + "=" * (-len(vb) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception:
        return None


def _pkce_pair():
    """RFC 7636 S256: 43-128-char verifier, SHA-256 challenge, base64url-no-pad."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# --------------------------------------------------------------------------- #
# Connection-quota guard (shared by every platform's callback)
# --------------------------------------------------------------------------- #

def _check_connection_quota(db: Session, user: User, new_count: int) -> None:
    if user.is_staff() or new_count <= 0:
        return
    cap = get_quota(db, user.tier, "social_connections")
    if cap is None:
        return
    used = current_usage(db, user, "social_connections")
    if used + new_count > cap:
        raise HTTPException(
            status_code=403,
            detail=(f"Your {user.tier} plan allows {cap} connected account(s); "
                    f"you already have {used} and tried to add {new_count} more. "
                    f"Upgrade your plan or disconnect an account first."),
        )


# --------------------------------------------------------------------------- #
# Facebook + Instagram (one Meta app, two platforms)
# --------------------------------------------------------------------------- #

def _meta_app_id() -> str:     return get_env("META_APP_ID", "")
def _meta_app_secret() -> str: return get_env("META_APP_SECRET", "")
def _meta_redirect() -> str:
    return get_env("META_OAUTH_REDIRECT", "http://localhost:8000/oauth/facebook/callback")

META_SCOPES = (
    "pages_manage_posts,pages_read_engagement,pages_show_list,"
    "instagram_basic,instagram_content_publish"
)
META_DIALOG = "https://www.facebook.com/v19.0/dialog/oauth"
META_TOKEN  = "https://graph.facebook.com/v19.0/oauth/access_token"
META_ACCTS  = "https://graph.facebook.com/v19.0/me/accounts"


@router.get("/facebook/start")
async def facebook_start(request: Request, user: User = Depends(get_current_user)):
    app_id = _meta_app_id()
    if not app_id:
        raise HTTPException(status_code=503, detail="Facebook/Instagram OAuth is not configured")
    params = {
        "client_id": app_id,
        "redirect_uri": _meta_redirect(),
        "scope": META_SCOPES,
        "response_type": "code",
        "state": _sign_state(user.id),
    }
    return RedirectResponse(url=f"{META_DIALOG}?{urlencode(params)}", status_code=303)


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
    if _verify_state(state, user.id) is None:
        raise HTTPException(status_code=400, detail="Invalid or forged OAuth state")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.get(META_TOKEN, params={
            "client_id": _meta_app_id(),
            "client_secret": _meta_app_secret(),
            "redirect_uri": _meta_redirect(),
            "code": code,
        })
        token_resp.raise_for_status()
        user_token = token_resp.json().get("access_token")
        if not user_token:
            raise HTTPException(status_code=502, detail="No access_token from Meta")

        accounts_resp = await client.get(META_ACCTS, params={"access_token": user_token})
        accounts_resp.raise_for_status()
        pages = accounts_resp.json().get("data", []) or []
        if not pages:
            raise HTTPException(status_code=400, detail="No Facebook Pages found on this account")

        # For each page, also try to fetch the linked Instagram Business account.
        ig_targets = []
        for page in pages:
            page_id = page.get("id")
            page_token = page.get("access_token")
            if not page_id or not page_token:
                continue
            try:
                ig_resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{page_id}",
                    params={"fields": "instagram_business_account{username}",
                            "access_token": page_token},
                )
                ig_resp.raise_for_status()
                ig = (ig_resp.json().get("instagram_business_account") or {})
                ig_id = ig.get("id")
                if ig_id:
                    ig_targets.append({
                        "page_token": page_token,
                        "ig_id": ig_id,
                        "ig_username": ig.get("username") or ig_id,
                    })
            except Exception:
                continue

    # Pre-flight quota across all new connections we're about to add.
    existing_fb = {
        c.page_id for c in db.query(SocialConnection).filter(
            SocialConnection.user_id == user.id,
            SocialConnection.platform == "facebook",
            SocialConnection.is_active == True,  # noqa: E712
        ).all()
    }
    existing_ig = {
        c.page_id for c in db.query(SocialConnection).filter(
            SocialConnection.user_id == user.id,
            SocialConnection.platform == "instagram",
            SocialConnection.is_active == True,  # noqa: E712
        ).all()
    }
    new_fb_count = sum(1 for p in pages if p.get("id") not in existing_fb)
    new_ig_count = sum(1 for t in ig_targets if t["ig_id"] not in existing_ig)
    _check_connection_quota(db, user, new_fb_count + new_ig_count)

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
                user_id=user.id, platform="facebook",
                account_name=page_name, access_token=page_token,
                page_id=page_id, is_active=True,
            ))

    for t in ig_targets:
        existing = db.query(SocialConnection).filter(
            SocialConnection.user_id == user.id,
            SocialConnection.platform == "instagram",
            SocialConnection.page_id == t["ig_id"],
        ).first()
        if existing:
            existing.access_token = t["page_token"]
            existing.account_name = f"@{t['ig_username']}"
            existing.is_active = True
        else:
            db.add(SocialConnection(
                user_id=user.id, platform="instagram",
                account_name=f"@{t['ig_username']}",
                access_token=t["page_token"], page_id=t["ig_id"],
                is_active=True,
            ))

    db.commit()
    log_action(db, user, "CREATE", "SocialConnection",
               detail=f"Meta OAuth — {len(pages)} FB page(s), {len(ig_targets)} IG account(s)")

    bits = []
    if len(pages): bits.append(f"{len(pages)} Facebook Page" + ("s" if len(pages) != 1 else ""))
    if len(ig_targets): bits.append(f"{len(ig_targets)} Instagram account" + ("s" if len(ig_targets) != 1 else ""))
    response = RedirectResponse(url="/connections", status_code=303)
    set_flash(response, "success", "Connected: " + (", ".join(bits) or "(no accounts found)"))
    return response


# --------------------------------------------------------------------------- #
# X (Twitter) — OAuth 2.0 with PKCE
# --------------------------------------------------------------------------- #

def _x_client_id() -> str:     return get_env("X_CLIENT_ID", "")
def _x_client_secret() -> str: return get_env("X_CLIENT_SECRET", "")
def _x_redirect() -> str:
    return get_env("X_OAUTH_REDIRECT", "http://localhost:8000/oauth/twitter/callback")

X_AUTHORIZE = "https://twitter.com/i/oauth2/authorize"
X_TOKEN     = "https://api.twitter.com/2/oauth2/token"
X_ME        = "https://api.twitter.com/2/users/me"
X_SCOPES    = "tweet.write tweet.read users.read offline.access"


@router.get("/twitter/start")
async def twitter_start(user: User = Depends(get_current_user)):
    if not _x_client_id():
        raise HTTPException(status_code=503, detail="X (Twitter) OAuth is not configured")
    verifier, challenge = _pkce_pair()
    params = {
        "response_type": "code",
        "client_id": _x_client_id(),
        "redirect_uri": _x_redirect(),
        "scope": X_SCOPES,
        "state": _sign_state(user.id, verifier),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(url=f"{X_AUTHORIZE}?{urlencode(params)}", status_code=303)


@router.get("/twitter/callback")
async def twitter_callback(
    code: str = "",
    state: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    verifier = _verify_state(state, user.id)
    if verifier is None:
        raise HTTPException(status_code=400, detail="Invalid or forged OAuth state")

    auth_b64 = base64.b64encode(f"{_x_client_id()}:{_x_client_secret()}".encode()).decode()
    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.post(
            X_TOKEN,
            headers={
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _x_redirect(),
                "client_id": _x_client_id(),
                "code_verifier": verifier,
            },
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"X token exchange failed: {token_resp.text}")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="No access_token from X")

        me_resp = await client.get(X_ME, headers={"Authorization": f"Bearer {access_token}"})
        me = (me_resp.json().get("data") or {}) if me_resp.status_code < 400 else {}
        x_id = me.get("id") or "unknown"
        username = me.get("username") or x_id

    _check_connection_quota(db, user, 1)

    existing = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id,
        SocialConnection.platform == "twitter",
        SocialConnection.page_id == x_id,
    ).first()
    if existing:
        existing.access_token = access_token
        existing.refresh_token = refresh_token
        existing.account_name = f"@{username}"
        existing.is_active = True
    else:
        db.add(SocialConnection(
            user_id=user.id, platform="twitter",
            account_name=f"@{username}",
            access_token=access_token, refresh_token=refresh_token,
            page_id=x_id, is_active=True,
        ))

    db.commit()
    log_action(db, user, "CREATE", "SocialConnection",
               detail=f"X OAuth — connected @{username}")
    response = RedirectResponse(url="/connections", status_code=303)
    set_flash(response, "success", f"Connected X account @{username}.")
    return response


# --------------------------------------------------------------------------- #
# LinkedIn — OAuth 2.0 (no PKCE required for confidential clients)
# --------------------------------------------------------------------------- #

def _li_client_id() -> str:     return get_env("LINKEDIN_CLIENT_ID", "")
def _li_client_secret() -> str: return get_env("LINKEDIN_CLIENT_SECRET", "")
def _li_redirect() -> str:
    return get_env("LINKEDIN_OAUTH_REDIRECT", "http://localhost:8000/oauth/linkedin/callback")

LI_AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
LI_TOKEN     = "https://www.linkedin.com/oauth/v2/accessToken"
LI_USERINFO  = "https://api.linkedin.com/v2/userinfo"
LI_SCOPES    = "openid profile email w_member_social"


@router.get("/linkedin/start")
async def linkedin_start(user: User = Depends(get_current_user)):
    if not _li_client_id():
        raise HTTPException(status_code=503, detail="LinkedIn OAuth is not configured")
    params = {
        "response_type": "code",
        "client_id": _li_client_id(),
        "redirect_uri": _li_redirect(),
        "scope": LI_SCOPES,
        "state": _sign_state(user.id),
    }
    return RedirectResponse(url=f"{LI_AUTHORIZE}?{urlencode(params)}", status_code=303)


@router.get("/linkedin/callback")
async def linkedin_callback(
    code: str = "",
    state: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    if _verify_state(state, user.id) is None:
        raise HTTPException(status_code=400, detail="Invalid or forged OAuth state")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.post(
            LI_TOKEN,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _li_redirect(),
                "client_id": _li_client_id(),
                "client_secret": _li_client_secret(),
            },
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"LinkedIn token exchange failed: {token_resp.text}")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="No access_token from LinkedIn")

        me_resp = await client.get(LI_USERINFO, headers={"Authorization": f"Bearer {access_token}"})
        me = me_resp.json() if me_resp.status_code < 400 else {}
        # `sub` is the user's URN id, e.g. "abc123" — full URN is "urn:li:person:abc123"
        sub = me.get("sub") or "unknown"
        display = me.get("name") or me.get("email") or sub

    _check_connection_quota(db, user, 1)
    existing = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id,
        SocialConnection.platform == "linkedin",
        SocialConnection.page_id == sub,
    ).first()
    if existing:
        existing.access_token = access_token
        existing.account_name = display
        existing.is_active = True
    else:
        db.add(SocialConnection(
            user_id=user.id, platform="linkedin",
            account_name=display,
            access_token=access_token,
            page_id=sub, is_active=True,
        ))
    db.commit()
    log_action(db, user, "CREATE", "SocialConnection",
               detail=f"LinkedIn OAuth — connected {display}")
    response = RedirectResponse(url="/connections", status_code=303)
    set_flash(response, "success", f"Connected LinkedIn account: {display}.")
    return response


# --------------------------------------------------------------------------- #
# TikTok — OAuth 2.0 with PKCE
# --------------------------------------------------------------------------- #

def _tt_client_key() -> str:    return get_env("TIKTOK_CLIENT_KEY", "")
def _tt_client_secret() -> str: return get_env("TIKTOK_CLIENT_SECRET", "")
def _tt_redirect() -> str:
    return get_env("TIKTOK_OAUTH_REDIRECT", "http://localhost:8000/oauth/tiktok/callback")

TT_AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN     = "https://open.tiktokapis.com/v2/oauth/token/"
TT_USERINFO  = "https://open.tiktokapis.com/v2/user/info/"
TT_SCOPES    = "user.info.basic,video.publish,video.upload"


@router.get("/tiktok/start")
async def tiktok_start(user: User = Depends(get_current_user)):
    if not _tt_client_key():
        raise HTTPException(status_code=503, detail="TikTok OAuth is not configured")
    verifier, challenge = _pkce_pair()
    params = {
        "client_key": _tt_client_key(),
        "response_type": "code",
        "scope": TT_SCOPES,
        "redirect_uri": _tt_redirect(),
        "state": _sign_state(user.id, verifier),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(url=f"{TT_AUTHORIZE}?{urlencode(params)}", status_code=303)


@router.get("/tiktok/callback")
async def tiktok_callback(
    code: str = "",
    state: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    verifier = _verify_state(state, user.id)
    if verifier is None:
        raise HTTPException(status_code=400, detail="Invalid or forged OAuth state")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.post(
            TT_TOKEN,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": _tt_client_key(),
                "client_secret": _tt_client_secret(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _tt_redirect(),
                "code_verifier": verifier,
            },
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"TikTok token exchange failed: {token_resp.text}")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        open_id = token_data.get("open_id")
        if not access_token or not open_id:
            raise HTTPException(status_code=502, detail=f"TikTok token response missing required fields: {token_data}")

        me_resp = await client.get(
            TT_USERINFO,
            params={"fields": "open_id,union_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        me = ((me_resp.json() or {}).get("data") or {}).get("user", {}) if me_resp.status_code < 400 else {}
        display = me.get("display_name") or open_id

    _check_connection_quota(db, user, 1)
    existing = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id,
        SocialConnection.platform == "tiktok",
        SocialConnection.page_id == open_id,
    ).first()
    if existing:
        existing.access_token = access_token
        existing.refresh_token = refresh_token
        existing.account_name = display
        existing.is_active = True
    else:
        db.add(SocialConnection(
            user_id=user.id, platform="tiktok",
            account_name=display,
            access_token=access_token, refresh_token=refresh_token,
            page_id=open_id, is_active=True,
        ))
    db.commit()
    log_action(db, user, "CREATE", "SocialConnection",
               detail=f"TikTok OAuth — connected {display}")
    response = RedirectResponse(url="/connections", status_code=303)
    set_flash(response, "success", f"Connected TikTok account: {display}.")
    return response
