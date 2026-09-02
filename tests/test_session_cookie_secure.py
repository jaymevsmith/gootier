"""tests/test_session_cookie_secure.py

The session cookie (COOKIE_NAME) is minted at five call sites (login,
/sso/consume, signup auto-login, verify-email auto-login, Google OAuth
callback) and none of them set `secure=True`, so the cookie would be sent
over plain HTTP in production. `set_session_cookie()` centralizes minting
and must mark the cookie Secure whenever ENV is prod/production, and must
NOT do so otherwise (Secure blocks the cookie entirely on http:// localhost
dev).
"""
from fastapi import Response

from auth import COOKIE_NAME, set_session_cookie


def _set_cookie_header(response: Response) -> str:
    return response.headers.get("set-cookie", "")


def test_session_cookie_is_secure_in_prod(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    response = Response()

    set_session_cookie(response, "test-token")

    header = _set_cookie_header(response)
    assert COOKIE_NAME in header
    assert "Secure" in header


def test_session_cookie_is_not_secure_outside_prod(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    response = Response()

    set_session_cookie(response, "test-token")

    header = _set_cookie_header(response)
    assert COOKIE_NAME in header
    assert "Secure" not in header
