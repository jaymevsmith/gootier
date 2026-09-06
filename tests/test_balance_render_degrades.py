"""tests/test_balance_render_degrades.py

A Token Service outage must not 500 a page.

On 2026-09-03 the Jhome Token Service served empty-body 404s for ~30 minutes
between a broken deploy (08:46 UTC) and the "Restore token service" deploy
(09:16 UTC). A customer arriving from the Backoffice handoff was signed in
correctly by /sso/consume, then landed on /dashboard, where the unguarded
`balance_tokens(db, user) // 1000` raised JTSError straight out of the route
handler -- an Internal Server Error immediately after a successful sign-in.

An unreachable Token Service means the balance is UNKNOWN, not zero (the same
fail-open rule services/token_wallet.debit_after_success already follows), so
these renders show a placeholder instead of dying. Spend GATES are deliberately
NOT covered by this: check_sufficient must keep raising, because failing open on
a label and failing open on an authorization are different decisions.
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import get_current_user, get_current_user_optional
from database import Base, get_db
import models  # noqa: F401
from models import User
from services import token_wallet
from services.jts_client import JTSError


class BrokenJTSClient:
    """Every call fails the way the outage failed: a non-2xx from JTS."""

    def ensure_wallet(self, external_user_id, email="", customer_ref=None):
        raise JTSError("ensure_wallet failed: 404 ")

    def get_balance(self, wallet_id):
        raise JTSError("get_balance failed: 404 ")


class UnreachableJTSClient:
    """The transport never even completes -- NOT a JTSError subclass."""

    def ensure_wallet(self, external_user_id, email="", customer_ref=None):
        raise httpx.ConnectError("connection refused")

    def get_balance(self, wallet_id):
        raise httpx.ConnectError("connection refused")


class HealthyJTSClient:
    def ensure_wallet(self, external_user_id, email="", customer_ref=None):
        return 320

    def get_balance(self, wallet_id):
        return 250_000


def _user(db) -> User:
    u = User(username="degrader", email="degrader@test.com", hashed_password="x",
             role="client", tier="trial", jts_wallet_id=320)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# --------------------------------------------------------------------------- #
# services/token_wallet.balance_tokens_or_none
# --------------------------------------------------------------------------- #

def test_balance_or_none_returns_the_balance_when_jts_answers(db, monkeypatch):
    monkeypatch.setattr(token_wallet, "_client", lambda: HealthyJTSClient())
    assert token_wallet.balance_tokens_or_none(db, _user(db)) == 250_000


def test_balance_or_none_is_none_when_jts_errors(db, monkeypatch):
    monkeypatch.setattr(token_wallet, "_client", lambda: BrokenJTSClient())
    assert token_wallet.balance_tokens_or_none(db, _user(db)) is None


def test_balance_or_none_is_none_when_jts_is_unreachable(db, monkeypatch):
    """httpx.ConnectError is not a JTSError, and it is what a real outage
    looks like once the host stops answering at all."""
    monkeypatch.setattr(token_wallet, "_client", lambda: UnreachableJTSClient())
    assert token_wallet.balance_tokens_or_none(db, _user(db)) is None


def test_check_sufficient_still_raises_when_jts_errors(db, monkeypatch):
    """The spend gate must NOT inherit the fail-open behaviour above: a balance
    we cannot read is not a balance we may authorize a charge against."""
    monkeypatch.setattr(token_wallet, "_client", lambda: BrokenJTSClient())
    with pytest.raises(JTSError):
        token_wallet.check_sufficient(db, _user(db), 1_000)


# --------------------------------------------------------------------------- #
# The pages themselves
# --------------------------------------------------------------------------- #

@pytest.fixture
def app_client(monkeypatch):
    """Minimal app carrying the three routers that render a balance, with the
    JTS client broken for every call. Same TestClient pattern as
    tests/test_sso_consume.py."""
    from routes import media_routes, stripe_routes, web_routes

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    session = TestingSession()
    user = _user(session)
    session.close()

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    def override_user():
        s = TestingSession()
        try:
            return s.query(User).filter(User.id == user.id).first()
        finally:
            s.close()

    monkeypatch.setattr(token_wallet, "_client", lambda: BrokenJTSClient())

    # /billing also reads TOKEN_SERVICE_URL through services.env_config.get_env,
    # which opens the app's real SessionLocal -- and this suite's DB has no
    # env_configs table (the same pre-existing gap that fails
    # tests/test_affiliates_integration.py). Stub it so the assertion below is
    # about the balance render and not about that unrelated hole.
    from services import env_config
    monkeypatch.setattr(env_config, "get_env", lambda key, default="": default)

    app = FastAPI()
    app.include_router(web_routes.router)
    app.include_router(media_routes.router)
    app.include_router(stripe_routes.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_optional] = override_user
    app.dependency_overrides[get_current_user] = override_user

    with TestClient(app) as c:
        yield c
    engine.dispose()


@pytest.mark.parametrize("path", ["/dashboard", "/studio", "/assets", "/billing"])
def test_pages_render_when_the_token_service_is_down(app_client, path):
    resp = app_client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


@pytest.mark.parametrize("path", ["/dashboard", "/studio", "/assets", "/billing"])
def test_unknown_balance_renders_a_placeholder_not_a_zero(app_client, path):
    """`0` is a lie that reads as "you are out of tokens" and would push the
    customer at the purchase page for no reason; `None` renders as "None"."""
    body = app_client.get(path).text
    assert "—" in body, f"{path} did not render the em-dash placeholder"
    assert ">None" not in body and "None tokens" not in body


def test_media_catalog_answers_with_a_null_balance_when_jts_is_down(app_client):
    """The generation modals in /compose and /ai-builder open on this call --
    a 500 here takes the whole modal down, not just the balance line."""
    resp = app_client.get("/api/media/catalog")
    assert resp.status_code == 200
    assert resp.json()["balance"] is None
