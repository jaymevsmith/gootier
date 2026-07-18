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
import stripe
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import COOKIE_NAME
from database import Base, get_db
import models  # noqa: F401 — ensures all models register on Base.metadata
from models import User
from routes import auth_routes, stripe_routes
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


# ---------------------------------------------------------------- #
# JTS wallet creation at signup (routes/auth_routes.py signup_submit)
# ---------------------------------------------------------------- #

def test_signup_creates_jts_wallet(db, client):
    """signup_submit should call ensure_wallet right after the new user is
    committed, so the wallet (and trial grant) exists as of signup rather
    than being created lazily on first AI use.

    get_env and trigger_verification_email are patched out here because
    _app_url() -> get_env(), and (separately) trigger_verification_email() ->
    send_email_verification() -> get_env(), each open their own
    SessionLocal() DB session rather than using the test's overridden get_db
    dependency — a pre-existing, unrelated issue (same root cause as the 2
    baseline failures in this file,
    test_signup_with_ref_stores_code_and_reports_signup /
    test_signup_without_ref_does_not_report_signup) that is out of scope
    for this task."""
    with patch("services.token_wallet.ensure_wallet") as mock_ensure_wallet, \
         patch.object(auth_routes, "get_env", return_value=""), \
         patch.object(auth_routes, "trigger_verification_email", return_value=False):
        resp = _signup_form(client, username="walletuser", email="walletuser@example.com")

    assert resp.status_code in (200, 303)
    mock_ensure_wallet.assert_called_once()
    args, kwargs = mock_ensure_wallet.call_args
    # ensure_wallet(db, user) — second positional arg is the just-created user
    called_user = args[1] if len(args) > 1 else kwargs.get("user")
    assert called_user.username == "walletuser"


def test_signup_succeeds_even_if_jts_ensure_wallet_raises(db, client):
    """A JTS outage at signup time (network blip, JTS deployment down, etc.)
    must not crash the signup request or prevent the account from being
    created — ensure_wallet is called after the user is already committed,
    and any failure there is caught and logged so signup still proceeds.

    get_env and trigger_verification_email are patched out for the same
    unrelated get_env/SessionLocal reason as test_signup_creates_jts_wallet
    above."""
    with patch("services.token_wallet.ensure_wallet", side_effect=ConnectionError("boom")) as mock_ensure_wallet, \
         patch.object(auth_routes, "get_env", return_value=""), \
         patch.object(auth_routes, "trigger_verification_email", return_value=False):
        resp = _signup_form(client, username="walletfail", email="walletfail@example.com")

    # Full success, not merely "didn't crash": redirected to /dashboard with
    # a session cookie set, proving the account was created and the user was
    # logged in despite ensure_wallet raising. (Note: this test's `client`
    # fixture uses its own isolated DB/engine, separate from the `db`
    # fixture, so we verify via the response rather than a `db` query.)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp.cookies
    mock_ensure_wallet.assert_called_once()


# ---------------------------------------------------------------- #
# Checkout discount / coupon logic (routes/stripe_routes.py)
# ---------------------------------------------------------------- #

def test_hub_down_discount_code_yields_no_coupon():
    """When the affiliates hub is down/unreachable, validate_code() fails
    closed with {"valid": False} (see jhome_affiliates.py). The coupon
    lookup helper must treat that as "no discount" rather than raising —
    proving checkout degrades gracefully instead of failing when the hub
    is unavailable."""
    with patch.object(stripe_routes.affiliates, "validate_code", return_value={"valid": False}):
        code_info = stripe_routes.affiliates.validate_code("JAYS10")
        coupon_id = stripe_routes._coupon_id_for_affiliate_code(code_info)

    assert coupon_id is None


def test_coupon_create_race_falls_through_to_none():
    """If Stripe's Coupon.create() raises because the deterministic coupon
    id was created concurrently by another request (or any other
    InvalidRequestError from create), _coupon_id_for_affiliate_code must
    catch it and return None instead of propagating and breaking checkout."""
    code_info = {
        "valid": True,
        "code": "JAYS10",
        "percent_off": 10,
        "duration": "once",
    }

    with patch.object(
        stripe.Coupon, "retrieve",
        side_effect=stripe.error.InvalidRequestError("No such coupon", param="id"),
    ), patch.object(
        stripe.Coupon, "create",
        side_effect=stripe.error.InvalidRequestError("Coupon already exists", param="id"),
    ):
        coupon_id = stripe_routes._coupon_id_for_affiliate_code(code_info)

    assert coupon_id is None


# ---------------------------------------------------------------- #
# First-payment reporting (routes/stripe_routes.py —
# _handle_checkout_completed)
# ---------------------------------------------------------------- #

def _make_user(db, username="checkout", email="checkout@example.com"):
    user = User(
        username=username,
        email=email,
        hashed_password="x",
        role="client",
        tier="trial",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_checkout_completed_reports_payment_under_invoice_id(db):
    """Subscription-mode Checkout Sessions carry `invoice` (in_...).
    charge.refunded reports charge["invoice"], and the hub's record_refund
    matches Conversion.invoice_id exactly — so the first payment must be
    reported under the invoice id, not the cs_... session id, or a refunded
    first payment never reverses the commission."""
    user = _make_user(db)
    session_obj = {
        "id": "cs_test_first",
        "invoice": "in_first_123",
        "amount_total": 2900,
        "metadata": {"user_id": str(user.id)},
    }

    with patch.object(stripe_routes.affiliates, "report_payment") as mock_report:
        stripe_routes._handle_checkout_completed(db, session_obj)

    mock_report.assert_called_once_with(user.id, 2900, "in_first_123")


def test_checkout_completed_falls_back_to_session_id_without_invoice(db):
    """When the session carries no invoice (e.g. payment-mode sessions),
    the handler keeps reporting under the cs_... session id."""
    user = _make_user(db, username="noinv", email="noinv@example.com")
    session_obj = {
        "id": "cs_test_noinv",
        "invoice": None,
        "amount_total": 2900,
        "metadata": {"user_id": str(user.id)},
    }

    with patch.object(stripe_routes.affiliates, "report_payment") as mock_report:
        stripe_routes._handle_checkout_completed(db, session_obj)

    mock_report.assert_called_once_with(user.id, 2900, "cs_test_noinv")


# ---------------------------------------------------------------- #
# Refund webhook (routes/stripe_routes.py — _handle_charge_refunded)
# ---------------------------------------------------------------- #

def test_charge_refunded_webhook_reports_refund_with_invoice_id(db):
    """A representative charge.refunded payload (with an attached invoice)
    should be reported to the affiliates hub using the invoice id."""
    charge_obj = {
        "id": "ch_test123",
        "invoice": "in_test456",
        "amount_refunded": 500,
    }

    with patch.object(stripe_routes.affiliates, "report_refund") as mock_report:
        stripe_routes._handle_charge_refunded(db, charge_obj)

    mock_report.assert_called_once_with("in_test456")


def test_charge_refunded_webhook_falls_back_to_charge_id_without_invoice(db):
    """When a charge has no attached invoice (e.g. a one-off payment_intent
    refund), the handler falls back to the charge id so the refund still
    gets reported."""
    charge_obj = {
        "id": "ch_test789",
        "invoice": None,
        "amount_refunded": 1000,
    }

    with patch.object(stripe_routes.affiliates, "report_refund") as mock_report:
        stripe_routes._handle_charge_refunded(db, charge_obj)

    mock_report.assert_called_once_with("ch_test789")
