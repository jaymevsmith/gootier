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
from sqlalchemy.orm import sessionmaker

from auth import COOKIE_NAME
from database import get_db
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
def client(test_engine):
    """A minimal app wired with just auth_routes, the shared per-test in-memory
    DB, and the CSRF cookie middleware (auth_routes' signup/login POSTs
    require Depends(verify_csrf)).

    Uses conftest's `test_engine` rather than building its own, so the request
    session and the sessions `get_env()` opens internally (via conftest's
    `_isolate_session_local`) are the same database — see that module's
    docstring for why that matters.
    """
    TestingSession = sessionmaker(bind=test_engine)

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


def test_signup_with_ref_stores_code_and_reports_signup(client):
    """`ensure_wallet` is patched for the same reason as the JTS tests below:
    signup calls it unconditionally, and unpatched it builds a real JTSClient
    that posts to the live Token Service (its `TOKEN_SERVICE_URL` default is
    the production URL). Nothing here is about wallets."""
    with patch("services.token_wallet.ensure_wallet"), \
         patch.object(auth_routes.affiliates, "report_signup") as mock_report:
        resp = _signup_form(client, ref="JAYS10", username="withref", email="withref@example.com")

    assert resp.status_code in (200, 303)
    mock_report.assert_called_once()
    args, kwargs = mock_report.call_args
    # report_signup(user.id, ref_code=user.referral_code)
    assert kwargs.get("ref_code") == "JAYS10" or (len(args) > 1 and args[1] == "JAYS10")


def test_signup_without_ref_does_not_report_signup(client):
    with patch("services.token_wallet.ensure_wallet"), \
         patch.object(auth_routes.affiliates, "report_signup") as mock_report:
        resp = _signup_form(client, ref=None, username="noref", email="noref@example.com")

    assert resp.status_code in (200, 303)
    mock_report.assert_not_called()


# ---------------------------------------------------------------- #
# JTS wallet creation at signup (routes/auth_routes.py signup_submit)
# ---------------------------------------------------------------- #

def test_signup_creates_jts_wallet(client):
    """signup_submit should call ensure_wallet right after the new user is
    committed, so the wallet (and trial grant) exists as of signup rather
    than being created lazily on first AI use.

    trigger_verification_email is patched out so the test can't reach real
    SMTP if the developer's environment happens to have SMTP vars exported;
    it is unrelated to what's asserted here."""
    with patch("services.token_wallet.ensure_wallet") as mock_ensure_wallet, \
         patch.object(auth_routes, "trigger_verification_email", return_value=False):
        resp = _signup_form(client, username="walletuser", email="walletuser@example.com")

    assert resp.status_code in (200, 303)
    mock_ensure_wallet.assert_called_once()
    args, kwargs = mock_ensure_wallet.call_args
    # ensure_wallet(db, user) — second positional arg is the just-created user
    called_user = args[1] if len(args) > 1 else kwargs.get("user")
    assert called_user.username == "walletuser"


def test_signup_succeeds_even_if_jts_ensure_wallet_raises(client):
    """A JTS outage at signup time (network blip, JTS deployment down, etc.)
    must not crash the signup request or prevent the account from being
    created — ensure_wallet is called after the user is already committed,
    and any failure there is caught and logged so signup still proceeds.

    trigger_verification_email is patched out for the same SMTP reason as
    test_signup_creates_jts_wallet above."""
    with patch("services.token_wallet.ensure_wallet", side_effect=ConnectionError("boom")) as mock_ensure_wallet, \
         patch.object(auth_routes, "trigger_verification_email", return_value=False):
        resp = _signup_form(client, username="walletfail", email="walletfail@example.com")

    # Full success, not merely "didn't crash": redirected to /dashboard with
    # a session cookie set, proving the account was created and the user was
    # logged in despite ensure_wallet raising. Verified via the response
    # rather than a `db` query: the request session is a different session
    # from the `db` fixture's (same database, but its own identity map).
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp.cookies
    mock_ensure_wallet.assert_called_once()


def _poison_session_ensure_wallet(db, user):
    """Stand-in for services.token_wallet.ensure_wallet that reproduces the
    exact scenario found in commit ef14c7c's code review: the JTS HTTP call
    succeeds (ensure_wallet's own network step is never even reached here —
    doesn't matter, it doesn't touch `db`), but the LOCAL `db.commit()`
    ensure_wallet does afterwards (to persist user.jts_wallet_id) fails.

    Rather than faking the exception, this forces a *real* IntegrityError by
    committing a genuine UNIQUE-constraint violation (duplicate username) on
    the same session the request is using — so SQLAlchemy actually leaves
    that session in its real 'pending rollback' state, the same way a
    transient DB blip / deadlock / pool exhaustion would in production."""
    dup = User(
        username=user.username,
        email="dup-" + user.email,
        hashed_password="x",
        role="client",
        tier="trial",
    )
    db.add(dup)
    db.commit()


def test_signup_succeeds_when_ensure_wallet_local_commit_fails(client):
    """Reproduces the code-review finding on commit ef14c7c: if the failure
    inside ensure_wallet comes from ITS OWN db.commit() (not the JTS HTTP
    call), the session is left needing a rollback. Without db.rollback() in
    signup_submit's except clause, the very next commit-requiring call
    (trigger_verification_email -> create_verification_token -> db.commit())
    hits that poisoned session and raises PendingRollbackError uncaught,
    crashing the whole signup request -- exactly the failure mode the
    ensure_wallet except clause exists to prevent.

    Unlike test_signup_creates_jts_wallet / test_signup_succeeds_even_if_...
    above, trigger_verification_email is deliberately left un-mocked here:
    we need its real db.commit() to run against the SAME session
    ensure_wallet poisoned, to actually prove the rollback fix works. Only
    get_env (for _app_url) and send_email_verification are patched -- neither
    touches the session under test, so patching them doesn't weaken the
    reproduction.

    get_env stays patched here specifically (the other tests no longer need
    to): conftest's in-memory engine uses StaticPool, so every session in a
    test shares one connection, and a real get_env() would open a second
    session on that same connection while the request session is deliberately
    sitting in its pending-rollback state. That's a test-harness artifact --
    in production get_env() gets its own connection -- and mocking it keeps
    the artifact out of the reproduction.

    Before the routes/auth_routes.py fix (db.rollback() added to the
    ensure_wallet except clause), this test fails because the uncaught
    PendingRollbackError propagates through TestClient's default
    raise_server_exceptions=True. After the fix, signup completes normally."""
    with patch("services.token_wallet.ensure_wallet", side_effect=_poison_session_ensure_wallet), \
         patch.object(auth_routes, "get_env", return_value=""), \
         patch.object(auth_routes, "send_email_verification", return_value=False):
        resp = _signup_form(client, username="pendingrollback", email="pendingrollback@example.com")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp.cookies


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
