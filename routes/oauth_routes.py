"""OAuth-based social-account connections.

One connect-via-login flow per platform. State is HMAC-signed and binds the
authenticated session to the callback. PKCE-using flows (TikTok) carry
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
# YouTube — Google OAuth 2.0 (sensitive scope: youtube.upload)
# --------------------------------------------------------------------------- #

def _yt_client_id() -> str:     return get_env("YOUTUBE_CLIENT_ID", "")
def _yt_client_secret() -> str: return get_env("YOUTUBE_CLIENT_SECRET", "")
def _yt_redirect() -> str:
    return get_env("YOUTUBE_OAUTH_REDIRECT", "http://localhost:8000/oauth/youtube/callback")

YT_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN     = "https://oauth2.googleapis.com/token"
YT_CHANNELS  = "https://www.googleapis.com/youtube/v3/channels"
YT_SCOPES    = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


@router.get("/youtube/start")
async def youtube_start(user: User = Depends(get_current_user)):
    if not _yt_client_id():
        raise HTTPException(status_code=503, detail="YouTube OAuth is not configured")
    params = {
        "client_id": _yt_client_id(),
        "redirect_uri": _yt_redirect(),
        "response_type": "code",
        "scope": YT_SCOPES,
        "state": _sign_state(user.id),
        # access_type=offline + prompt=consent forces Google to return a
        # refresh_token. Without prompt=consent on re-auth Google withholds
        # the refresh_token and breaks scheduled uploads.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return RedirectResponse(url=f"{YT_AUTHORIZE}?{urlencode(params)}", status_code=303)


@router.get("/youtube/callback")
async def youtube_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if error:
        raise HTTPException(status_code=400, detail=f"YouTube OAuth declined: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    if _verify_state(state, user.id) is None:
        raise HTTPException(status_code=400, detail="Invalid or forged OAuth state")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.post(
            YT_TOKEN,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": _yt_client_id(),
                "client_secret": _yt_client_secret(),
                "redirect_uri": _yt_redirect(),
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"YouTube token exchange failed: {token_resp.text}")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = int(token_data.get("expires_in", 3600))
        if not access_token:
            raise HTTPException(status_code=502, detail="No access_token from Google")

        ch_resp = await client.get(
            YT_CHANNELS,
            params={"mine": "true", "part": "snippet"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if ch_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"YouTube channels.list failed: {ch_resp.text}")
        items = (ch_resp.json() or {}).get("items") or []
        if not items:
            raise HTTPException(status_code=400, detail="No YouTube channel found on this account")

    _check_connection_quota(db, user, len(items))

    from datetime import timedelta
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)

    for ch in items:
        ch_id = ch.get("id")
        snippet = ch.get("snippet") or {}
        title = snippet.get("title") or ch_id
        if not ch_id:
            continue
        existing = db.query(SocialConnection).filter(
            SocialConnection.user_id == user.id,
            SocialConnection.platform == "youtube",
            SocialConnection.page_id == ch_id,
        ).first()
        if existing:
            existing.access_token = access_token
            # Google only returns refresh_token on first consent — preserve
            # an existing one if this re-auth didn't include one.
            if refresh_token:
                existing.refresh_token = refresh_token
            existing.account_name = title
            existing.expires_at = expires_at
            existing.is_active = True
        else:
            db.add(SocialConnection(
                user_id=user.id, platform="youtube",
                account_name=title,
                access_token=access_token, refresh_token=refresh_token,
                page_id=ch_id, expires_at=expires_at,
                is_active=True,
            ))

    db.commit()
    log_action(db, user, "CREATE", "SocialConnection",
               detail=f"YouTube OAuth — {len(items)} channel(s)")
    response = RedirectResponse(url="/connections", status_code=303)
    bits = ", ".join(c.get('snippet', {}).get('title', '?') for c in items)
    set_flash(response, "success", f"Connected YouTube channel: {bits}.")
    return response


# Needed by services/social_publish.py for the refresh-token dance
def refresh_youtube_access_token(refresh_token: str) -> dict:
    """Synchronous helper — used by the scheduler's publish path. Returns the
    full token-endpoint response (caller persists the new access_token +
    expires_at)."""
    import httpx as _httpx
    resp = _httpx.post(
        YT_TOKEN, timeout=20.0,
        data={
            "refresh_token": refresh_token,
            "client_id": _yt_client_id(),
            "client_secret": _yt_client_secret(),
            "grant_type": "refresh_token",
        },
    )
    resp.raise_for_status()
    return resp.json()


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
TT_SCOPES    = "user.info.basic,video.upload"


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


# --------------------------------------------------------------------------- #
# "Sign in with Google" — identity-only OAuth (NOT a social connection).
#
# Reuses the YouTube OAuth client by default (Google's developer console
# issues one client ID per project; you just register multiple redirect URIs
# under it).  Setting GOOGLE_AUTH_CLIENT_ID/SECRET overrides that fallback
# if you'd rather use a dedicated client for sign-in.
# --------------------------------------------------------------------------- #

def _google_auth_client_id() -> str:
    return get_env("GOOGLE_AUTH_CLIENT_ID", "") or get_env("YOUTUBE_CLIENT_ID", "")


def _google_auth_client_secret() -> str:
    return get_env("GOOGLE_AUTH_CLIENT_SECRET", "") or get_env("YOUTUBE_CLIENT_SECRET", "")


def _google_auth_redirect() -> str:
    return get_env(
        "GOOGLE_AUTH_REDIRECT",
        "http://localhost:8000/oauth/google/callback",
    )


GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN     = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO  = "https://openidconnect.googleapis.com/v1/userinfo"
# openid -> get a stable Google account ID in the `sub` claim
# email   -> read the user's email address + email_verified
# profile -> read display name (used as nickname)
GOOGLE_AUTH_SCOPES = "openid email profile"


@router.get("/google/start", include_in_schema=False)
async def google_auth_start():
    """Kicks off "Sign in with Google".  Unauthenticated — that's the point."""
    if not _google_auth_client_id():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    # _sign_state takes a user_id, but we don't have one yet (we're signing IN).
    # Pass 0 — the signature still binds the nonce to our SECRET_KEY, so a
    # forged callback can't be replayed without the secret.
    params = {
        "client_id": _google_auth_client_id(),
        "redirect_uri": _google_auth_redirect(),
        "response_type": "code",
        "scope": GOOGLE_AUTH_SCOPES,
        "state": _sign_state(0),
        "access_type": "online",            # no refresh token needed — we just want identity
        "include_granted_scopes": "true",
        "prompt": "select_account",         # always show the account picker
    }
    return RedirectResponse(url=f"{GOOGLE_AUTHORIZE}?{urlencode(params)}", status_code=303)


@router.get("/google/callback", include_in_schema=False)
async def google_auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    """Receives Google's callback, finds-or-creates the User, sets the JWT
    cookie, and redirects to /dashboard.  Returns 400 on any forged or
    malformed input so a stray callback URL never auto-creates an account."""
    from datetime import datetime
    import secrets as _py_secrets
    from auth import create_access_token, hash_password, set_session_cookie
    from services.flash import set_flash

    if error:
        return _google_auth_fail(request, f"Google declined the sign-in: {error}")
    if not code:
        return _google_auth_fail(request, "Missing OAuth code from Google.")
    if _verify_state(state, 0) is None:
        return _google_auth_fail(request, "Invalid or forged sign-in state.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": _google_auth_client_id(),
                "client_secret": _google_auth_client_secret(),
                "redirect_uri": _google_auth_redirect(),
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            return _google_auth_fail(
                request,
                f"Google token exchange failed ({token_resp.status_code}).  "
                "If this keeps happening, the redirect URI in Google Cloud Console "
                "may not match GOOGLE_AUTH_REDIRECT.",
            )
        access_token = (token_resp.json() or {}).get("access_token")
        if not access_token:
            return _google_auth_fail(request, "Google returned no access_token.")

        info_resp = await client.get(
            GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_resp.status_code >= 400:
            return _google_auth_fail(request, "Couldn't read your Google profile.")
        info = info_resp.json() or {}

    sub = (info.get("sub") or "").strip()
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or "").strip()
    email_verified = bool(info.get("email_verified"))
    if not sub or not email:
        return _google_auth_fail(request, "Google didn't return an email — can't sign you in.")
    if not email_verified:
        return _google_auth_fail(request, "Google reports this email as unverified.")

    # 1) Match by google_sub first (stable even if the user changes email).
    user = db.query(User).filter(User.google_sub == sub).first()
    is_new = False
    if not user:
        # 2) Fall back to email — links Google to an existing password account.
        user = db.query(User).filter(User.email == email).first()
        if user:
            user.google_sub = sub
            if not user.is_verified:
                user.is_verified = True
            db.commit()
    if not user:
        # 3) New account — provision with a random unguessable password.
        # The user can use "Forgot password" later if they want password auth.
        username = _unique_username_from_email(db, email)
        user = User(
            username=username,
            email=email,
            hashed_password=hash_password(_py_secrets.token_urlsafe(32)),
            role="client",
            tier="trial",
            is_active=True,
            is_verified=True,           # Google has verified the email
            nickname=name or username,
            google_sub=sub,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        is_new = True
        log_action(db, user, "SIGNUP", "User", str(user.id),
                   detail="Sign in with Google — new account")
        # Best-effort welcome email; never block sign-in.
        try:
            from services.onboarding import send_welcome_email
            send_welcome_email(user)
        except Exception:
            pass
    else:
        log_action(db, user, "LOGIN", "User", str(user.id),
                   detail="Sign in with Google")

    if not user.is_active:
        return _google_auth_fail(request, "This account is disabled.")

    token = create_access_token(user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(response, token)
    set_flash(
        response,
        "success",
        f"Welcome to Gootier, {user.nickname or user.username}!" if is_new
        else f"Welcome back, {user.nickname or user.username}!",
    )
    return response


def _google_auth_fail(request: Request, message: str):
    """Helper: render the login page with a flash error.  Used everywhere
    in the callback that we want to bail out cleanly without 5xx-ing."""
    from fastapi.templating import Jinja2Templates
    from services.csrf import get_or_create_token
    _t = Jinja2Templates(directory="templates")
    return _t.TemplateResponse(
        request, "login.html",
        {"error": message, "csrf_token": get_or_create_token(request)},
        status_code=400,
    )


def _unique_username_from_email(db, email: str) -> str:
    """Derives a username from the local part of the email, appending a
    numeric suffix on collision (e.g. jaymev, jaymev2, jaymev3, …).

    Matches the existing username constraints: 3-20 chars, letters / digits /
    underscore / hyphen.  Anything outside that set is dropped.
    """
    import re as _re
    base = _re.sub(r"[^a-zA-Z0-9_-]", "", (email.split("@", 1)[0] or "").lower())[:18]
    if len(base) < 3:
        base = (base + "user")[:18]
    candidate = base
    suffix = 2
    while db.query(User).filter(User.username == candidate).first():
        tail = str(suffix)
        candidate = (base[: 20 - len(tail)] + tail)
        suffix += 1
        if suffix > 999:  # defence-in-depth, never expected to hit
            candidate = base[:16] + secrets.token_urlsafe(3).lower()
            break
    return candidate
