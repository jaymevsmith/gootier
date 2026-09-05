"""Changing your email must not 500 after the change is already saved.

`routes/api_routes.py::update_profile` commits the new address, then fires a
verification email — the same post-commit shape as signup, and it had the same
gap: `trigger_verification_email()` -> `send_email_verification()` ->
`_smtp_config()` makes five `get_env()` calls, each opening its own
`database.SessionLocal()`. A database blip there raised out of the handler, so
the caller got a 500 for a profile update that *had* been written, and the UI
(templates/profile.html only reloads on a successful response) never showed the
new address.

`tests/conftest.py::unavailable_config_db` simulates the blip.
"""
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from auth import get_current_user
from database import get_db
from models import User
from routes import api_routes, auth_routes


@pytest.fixture
def profile_client(test_engine):
    """api_routes with an authenticated user, resolved from the *request's*
    session the way the real `get_current_user` does — handing the handler a
    User attached to some other session would make its `db.commit()` a no-op
    and quietly invalidate every assertion here."""
    TestingSession = sessionmaker(bind=test_engine)

    seed = TestingSession()
    seed.add(User(
        username="profileuser",
        email="profile@example.com",
        hashed_password="x",
        role="client",
        tier="trial",
        is_active=True,
        is_verified=True,
    ))
    seed.commit()
    seed.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    def override_get_current_user(db: Session = Depends(get_db)):
        return db.query(User).filter(User.username == "profileuser").first()

    app = FastAPI()
    app.include_router(api_routes.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as c:
        yield c


def _current_email(test_engine):
    session = sessionmaker(bind=test_engine)()
    try:
        return session.query(User).filter(User.username == "profileuser").first().email
    finally:
        session.close()


def test_email_change_is_saved_when_the_config_database_is_unavailable(
    profile_client, test_engine, unavailable_config_db,
):
    """The regression: a 200 with the new address persisted, not a 500."""
    resp = profile_client.patch("/api/profile", json={"email": "moved@example.com"})

    assert resp.status_code == 200
    assert resp.json()["changed"] == ["email"]
    assert _current_email(test_engine) == "moved@example.com"


def test_email_change_reports_that_the_verification_email_did_not_go_out(
    profile_client, unavailable_config_db,
):
    """Swallowing the failure silently would be its own bug — the caller would
    sit waiting for an email that was never sent. The response says so, using
    the same vocabulary as /profile/verify-email/resend's `delivered` flag."""
    resp = profile_client.patch("/api/profile", json={"email": "moved@example.com"})

    assert resp.status_code == 200
    assert resp.json()["verification_email_sent"] is False


def test_email_change_reports_a_delivered_verification_email(profile_client):
    """The happy path still reports success, so the flag isn't just hardcoded."""
    with patch.object(auth_routes, "send_email_verification", return_value=True):
        resp = profile_client.patch("/api/profile", json={"email": "moved@example.com"})

    assert resp.status_code == 200
    assert resp.json()["verification_email_sent"] is True


def test_a_nickname_only_change_does_not_claim_anything_about_email(profile_client):
    """No email change means no verification email, so the flag has no meaning
    and must not appear — an always-present `false` would read as a failure."""
    resp = profile_client.patch("/api/profile", json={"nickname": "Jay"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] == ["nickname"]
    assert "verification_email_sent" not in body


def test_resend_endpoint_still_surfaces_failure_rather_than_claiming_success(
    profile_client, test_engine, unavailable_config_db,
):
    """Deliberately *not* wrapped like update_profile: sending is the entire
    point of this request, so a failure has to reach the caller. It may raise
    or report undelivered — it must not return a cheerful `delivered: true`.

    The user has to be marked unverified first, or the handler short-circuits
    on `if user.is_verified` and returns `delivered: True` without ever trying
    to send — which is correct behaviour, just not what this test is about."""
    session = sessionmaker(bind=test_engine)()
    try:
        session.query(User).filter(User.username == "profileuser").first().is_verified = False
        session.commit()
    finally:
        session.close()

    client = TestClient(profile_client.app, raise_server_exceptions=False)
    resp = client.post("/api/profile/verify-email/resend")

    assert not (resp.status_code == 200 and resp.json().get("delivered") is True)
