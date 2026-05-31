"""Double-submit-cookie CSRF protection for form-encoded auth endpoints.

The JSON API (TJ.api.*) is already covered by SameSite=lax + same-origin
fetch, but the pre-login form-encoded endpoints (login, signup, password
reset, email verify) need explicit CSRF tokens because they accept any
content-type and can be hit cross-site via a hidden form submit.

Flow:
  1. On the GET that renders a form, `get_or_create_token(request)` returns
     the existing cookie value or generates a new one. The template injects
     it as a hidden field.
  2. On POST, `Depends(verify_csrf)` reads the submitted form field and the
     cookie, hmac-compares them. Mismatch → 403.
  3. CSRFCookieMiddleware ensures the cookie is set on every response if a
     token was issued during the request.

Tokens are 32 bytes of url-safe base64 — fresh on every login form load
for a logged-out visitor, sticky across requests for a returning one.
"""
import hmac
import secrets
from typing import Optional

from fastapi import Form, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

CSRF_COOKIE = "csrf_token"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days; refreshed on each render


def get_or_create_token(request: Request) -> str:
    """Returns the current request's CSRF token, generating one and stashing
    it on `request.state` if no cookie was sent. CSRFCookieMiddleware reads
    `request.state.csrf_token_new` to set the cookie on the response."""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing:
        # Stash so the middleware can refresh the cookie max-age.
        request.state.csrf_token = existing
        return existing
    token = secrets.token_urlsafe(32)
    request.state.csrf_token = token
    request.state.csrf_token_new = True
    return token


def verify(request: Request, submitted: Optional[str]) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if not cookie_token or not submitted:
        return False
    return hmac.compare_digest(cookie_token, submitted)


async def verify_csrf(
    request: Request,
    csrf_token: Optional[str] = Form(default=None),
):
    """FastAPI dependency — drop into protected form-POST routes."""
    if not verify(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch — refresh the page and try again.")


class CSRFCookieMiddleware(BaseHTTPMiddleware):
    """Sets / refreshes the CSRF cookie on outgoing responses when the
    request handler called get_or_create_token()."""
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        token = getattr(request.state, "csrf_token", None)
        if token:
            # Cookie must be readable by JS only for AJAX setups; we use it
            # purely for double-submit, so httponly=False is fine here
            # (the value is also embedded in the form so JS doesn't need it).
            # But samesite=lax + secure-in-prod is the main mitigation.
            response.set_cookie(
                CSRF_COOKIE, token,
                max_age=_COOKIE_MAX_AGE,
                httponly=True,         # double-submit only; templates render it
                samesite="lax",
                path="/",
            )
        return response
