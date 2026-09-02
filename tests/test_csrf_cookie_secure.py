"""tests/test_csrf_cookie_secure.py

Mirrors tests/test_session_cookie_secure.py for the OTHER cookie that was
missing secure=True: the CSRF double-submit cookie set by
CSRFCookieMiddleware. Drives it through a minimal app so the assertion is
against the real Set-Cookie header the middleware produces, not a hand-built
Response.
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.csrf import CSRF_COOKIE, CSRFCookieMiddleware, get_or_create_token


def _client():
    app = FastAPI()
    app.add_middleware(CSRFCookieMiddleware)

    @app.get("/form")
    def form(request: Request):
        get_or_create_token(request)
        return {"ok": True}

    return TestClient(app)


def test_csrf_cookie_is_secure_in_prod(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    resp = _client().get("/form")

    header = resp.headers.get("set-cookie", "")
    assert CSRF_COOKIE in header
    assert "Secure" in header


def test_csrf_cookie_is_not_secure_outside_prod(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    resp = _client().get("/form")

    header = resp.headers.get("set-cookie", "")
    assert CSRF_COOKIE in header
    assert "Secure" not in header
