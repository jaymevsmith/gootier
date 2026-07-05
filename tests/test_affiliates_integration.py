"""Jhome Affiliates integration: referral capture at signup.

Follows this repo's existing test convention (tests/test_billing_period.py):
plain pytest functions using the shared `db` fixture from tests/conftest.py
(isolated in-memory SQLite). For the signup flow specifically we also spin up
a minimal FastAPI app around just routes/auth_routes.router (rather than the
full main.py app, which starts a background scheduler task on lifespan) and
drive it with FastAPI's TestClient. The AffiliatesClient's network methods are
patched with unittest.mock so no real HTTP call is made.
"""
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
import models  # noqa: F401 — ensures all models register on Base.metadata
from models import User
from routes import auth_routes
from services.csrf import CSRFCookieMiddleware


def test_user_model_has_referral_code_column(db):
    """Column exists, defaults to None, and round-trips through the session."""
    user = User(
        username="colref",
        email="colref@example.com",
        hashed_password="x",
        role="client",
        tier="trial",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    assert user.referral_code is None

    user.referral_code = "JAYS10"
    db.commit()
    db.refresh(user)
    assert user.referral_code == "JAYS10"


@pytest.fixture
def client():
    """A minimal app wired with just auth_routes, its own isolated in-memory
    DB, and the CSRF cookie middleware (auth_routes' signup/login POSTs
    require Depends(verify_csrf))."""
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
        yield c

    engine.dispose()


def _signup_form(client, ref=None, username="newuser", email="newuser@example.com"):
    """GET /signup to obtain a CSRF cookie+token, then POST the signup form
    (mirroring the hidden csrf_token / ref fields templates/signup.html
    renders) with that token attached."""
    get_resp = client.get("/signup", params={"ref": ref} if ref else {})
    csrf_token = client.cookies.get("csrf_token")
    assert csrf_token, "CSRFCookieMiddleware should have set a csrf_token cookie"

    form = {
        "username": username,
        "email": email,
        "password": "Passw0rd!",
        "csrf_token": csrf_token,
    }
    if ref:
        form["ref"] = ref
    return client.post("/signup", data=form, follow_redirects=False)


def test_signup_with_ref_stores_code_and_reports_signup(db, client):
    with patch.object(auth_routes.affiliates, "report_signup") as mock_report:
        resp = _signup_form(client, ref="JAYS10", username="withref", email="withref@example.com")

    assert resp.status_code in (200, 303)
    mock_report.assert_called_once()
    args, kwargs = mock_report.call_args
    # report_signup(user.id, ref_code=user.referral_code)
    assert kwargs.get("ref_code") == "JAYS10" or (len(args) > 1 and args[1] == "JAYS10")


def test_signup_without_ref_does_not_report_signup(db, client):
    with patch.object(auth_routes.affiliates, "report_signup") as mock_report:
        resp = _signup_form(client, ref=None, username="noref", email="noref@example.com")

    assert resp.status_code in (200, 303)
    mock_report.assert_not_called()
