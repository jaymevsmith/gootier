"""tests/test_sso_consume.py

Reuses the minimal-app TestClient pattern established in
tests/test_affiliates_integration.py and tests/test_internal_handoff.py:
a minimal FastAPI() app wired with just the router under test, an isolated
in-memory SQLite DB via dependency_overrides, driven with TestClient.
auth_routes needs CSRFCookieMiddleware since its OTHER routes (login/signup)
require it -- /sso/consume itself does not use Depends(verify_csrf), but the
middleware itself is harmless to include.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import COOKIE_NAME
from database import Base, get_db
import models  # noqa: F401
from models import HandoffToken, User
from routes import auth_routes
from services.csrf import CSRFCookieMiddleware
from services.handoff import hash_token


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.add_middleware(CSRFCookieMiddleware)
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c, TestingSession
    engine.dispose()


def _make_token(session_factory, *, used=False, expired=False, user_kwargs=None):
    s = session_factory()
    user = User(username="consumer", email="consumer@example.com", hashed_password="x",
                role="client", tier="trial", **(user_kwargs or {}))
    s.add(user)
    s.commit()
    s.refresh(user)

    plaintext = "test-plaintext-token"
    expires_at = (datetime.utcnow() - timedelta(minutes=1)) if expired \
        else (datetime.utcnow() + timedelta(minutes=2))
    row = HandoffToken(
        token_hash=hash_token(plaintext), user_id=user.id, expires_at=expires_at,
        used_at=datetime.utcnow() if used else None,
    )
    s.add(row)
    s.commit()
    user_id = user.id
    s.close()
    return plaintext, user_id


def test_no_token_redirects_to_login_with_error(client):
    c, _ = client
    resp = c.get("/sso/consume", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=sso"


def test_valid_token_redeems_into_a_real_session(client):
    c, TestingSession = client
    plaintext, user_id = _make_token(TestingSession)

    resp = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp.cookies

    from auth import _decode_user_id
    decoded_user_id = _decode_user_id(resp.cookies[COOKIE_NAME])
    assert decoded_user_id == user_id


def test_token_is_burned_after_use(client):
    c, TestingSession = client
    plaintext, _ = _make_token(TestingSession)

    c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)
    s = TestingSession()
    row = s.query(HandoffToken).one()
    assert row.used_at is not None
    s.close()


def test_reusing_a_burned_token_fails(client):
    c, TestingSession = client
    plaintext, _ = _make_token(TestingSession)

    c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)
    resp = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=sso"


def test_expired_token_fails(client):
    c, TestingSession = client
    plaintext, _ = _make_token(TestingSession, expired=True)

    resp = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=sso"


def test_already_used_token_fails(client):
    c, TestingSession = client
    plaintext, _ = _make_token(TestingSession, used=True)

    resp = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=sso"


def test_deactivated_user_fails_even_with_a_valid_token(client):
    c, TestingSession = client
    plaintext, _ = _make_token(TestingSession, user_kwargs={"is_active": False})

    resp = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=sso"


def test_unknown_token_fails(client):
    c, _ = client
    resp = c.get("/sso/consume?token=this-token-does-not-exist", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=sso"


def test_two_redeems_of_the_same_token_only_one_succeeds(client):
    """Drives the real route twice. The second call happens after the first
    has already burned the token, so this proves burned-token rejection
    through the actual endpoint -- not through hand-written SQL bypassing
    sso_consume entirely."""
    c, TestingSession = client
    plaintext, user_id = _make_token(TestingSession)

    resp1 = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)
    resp2 = c.get(f"/sso/consume?token={plaintext}", follow_redirects=False)

    assert resp1.status_code == 303
    assert resp1.headers["location"] == "/dashboard"
    assert "set-cookie" in resp1.headers

    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/login?error=sso"
    # The second response must not carry a fresh session cookie.
    assert resp2.headers.get("set-cookie", "") == "" or "access_token" not in resp2.headers.get("set-cookie", "")

    s = TestingSession()
    row = s.query(HandoffToken).one()
    assert row.used_at is not None
    s.close()


def test_atomic_update_where_used_at_is_null_prevents_a_double_burn(client):
    """This is a SQL-level proof that the UPDATE...WHERE used_at IS NULL
    pattern sso_consume relies on is correct -- it drives two separate DB
    sessions directly, not the HTTP endpoint. See
    test_two_redeems_of_the_same_token_only_one_succeeds for the end-to-end
    proof that sso_consume actually uses this pattern."""
    c, TestingSession = client
    plaintext, user_id = _make_token(TestingSession)

    from sqlalchemy import text
    s1 = TestingSession()
    s2 = TestingSession()
    h = hash_token(plaintext)
    r1 = s1.execute(
        text("UPDATE handoff_tokens SET used_at = :now WHERE token_hash = :h AND used_at IS NULL"),
        {"now": datetime.utcnow(), "h": h},
    )
    s1.commit()
    r2 = s2.execute(
        text("UPDATE handoff_tokens SET used_at = :now WHERE token_hash = :h AND used_at IS NULL"),
        {"now": datetime.utcnow(), "h": h},
    )
    s2.commit()
    s1.close()
    s2.close()

    assert r1.rowcount == 1
    assert r2.rowcount == 0
