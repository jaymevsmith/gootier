"""Server-side flash helper — fires a single toast on the next page load.

Pair with `tj-notify.js` which reads + clears the `_tj_flash` cookie on
`DOMContentLoaded`. Use for PRG (post-redirect-get) flows where you want
to greet the user with a toast on the destination page.

Usage:
    response = RedirectResponse(url="/dashboard", status_code=303)
    set_flash(response, "success", f"Welcome back, {user.username}!")
    return response
"""
import json
from typing import Literal
from urllib.parse import quote

from fastapi import Response

FlashKind = Literal["success", "error", "warning", "info"]
COOKIE_NAME = "_tj_flash"


def set_flash(response: Response, kind: FlashKind, msg: str) -> None:
    # URL-encode the JSON so Python's SimpleCookie doesn't octal-escape commas
    # (which JSON.parse on the client can't undo).
    payload = quote(json.dumps({"kind": kind, "msg": msg}), safe="")
    response.set_cookie(
        key=COOKIE_NAME,
        value=payload,
        max_age=60,
        httponly=False,      # JS must read this
        samesite="lax",
        path="/",
    )
