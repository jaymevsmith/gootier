"""tests/test_internal_handoff.py

Follows this repo's established route-test convention (see
tests/test_affiliates_integration.py): a minimal FastAPI() app wired with
just the router under test, an isolated in-memory SQLite DB via
dependency_overrides, driven with TestClient. No CSRF middleware here --
/internal/handoff is a machine-to-machine JSON API authenticated by
X-Internal-Key, not a browser form.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
import models  # noqa: F401
from models import User
from routes import internal_routes
from services.env_config import set_env


@pytest.fixture
def client(db):
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
    app.include_router(internal_routes.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c, TestingSession
    engine.dispose()


def _configure(monkeypatch):
    """Every test in this file needs both keys set: GOOTIER_INTERNAL_KEY for
    auth, APP_URL to build the consume_url. Auth-failure tests still call
    this (rather than a narrower key-only variant) since the auth check
    happens before the handler body ever reads APP_URL -- having APP_URL
    set doesn't change their outcome, and one helper is simpler than two."""
    monkeypatch.setattr(
        "routes.internal_routes.get_env",
        lambda key, default="": {
            "GOOTIER_INTERNAL_KEY": "test-internal-key",
            "APP_URL": "https://gootier.example.com",
        }.get(key, default),
    )


def test_handoff_requires_the_internal_key(client, monkeypatch):
    c, _ = client
    _configure(monkeypatch)
    resp = c.post("/internal/handoff", json={"email": "a@example.com"})
    assert resp.status_code == 401


def test_handoff_rejects_the_wrong_key(client, monkeypatch):
    c, _ = client
    _configure(monkeypatch)
    resp = c.post(
        "/internal/handoff",
        json={"email": "a@example.com"},
        headers={"X-Internal-Key": "wrong"},
    )
    assert resp.status_code == 401


def test_handoff_for_an_existing_user_returns_a_consume_url(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "jane@example.com", "name": "Jane", "jhome_sub": "sub-1"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["consume_url"].startswith("https://gootier.example.com/sso/consume?token=")
    assert resp.headers["cache-control"] == "no-store"


def test_admin_account_handoff_is_refused(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="theadmin", email="admin@example.com", hashed_password="x",
                role="admin", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "admin@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 403


def test_handoff_fails_closed_when_the_key_is_unconfigured(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(
        "routes.internal_routes.get_env",
        lambda key, default="": "" if key == "GOOTIER_INTERNAL_KEY" else default,
    )
    resp = c.post(
        "/internal/handoff",
        json={"email": "a@example.com"},
        headers={"X-Internal-Key": ""},
    )
    assert resp.status_code == 401


def test_handoff_rejects_a_non_ascii_key_without_raising(client, monkeypatch):
    c, _ = client
    _configure(monkeypatch)
    # httpx.Headers only accepts non-ASCII str header values via a raw bytes
    # value (it otherwise ascii-encodes str values itself, before the
    # request ever leaves the client) -- passing utf-8 bytes here mirrors
    # what Starlette actually decodes as latin-1 on the wire.
    resp = c.post(
        "/internal/handoff",
        json={"email": "a@example.com"},
        headers={"X-Internal-Key": "café".encode("utf-8")},
    )
    assert resp.status_code == 401


def test_email_lookup_is_case_insensitive(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="mixedcase", email="MixedCase@Example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "mixedcase@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200


def test_case_variant_duplicate_accounts_are_refused_not_crashed(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="dupe1", email="Dupe@Example.com", hashed_password="x",
                role="client", tier="trial"))
    s.add(User(username="dupe2", email="dupe@example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "dupe@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
