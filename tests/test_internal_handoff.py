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
        json={"email": "jane@example.com", "name": "Jane", "jhome_sub": "sub-1",
              "email_verified": True},
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
        json={"email": "admin@example.com", "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"error": "admin_account_not_supported"}


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
        json={"email": "mixedcase@example.com", "email_verified": True},
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
        json={"email": "dupe@example.com", "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "ambiguous_identity"}


def test_handoff_for_a_new_email_creates_a_user(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    resp = c.post(
        "/internal/handoff",
        json={"email": "newperson@example.com", "name": "New Person", "jhome_sub": "sub-2"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    s = TestingSession()
    user = s.query(User).filter(User.email == "newperson@example.com").one()
    assert user.username == "newperson"
    assert user.role == "client"
    assert user.tier == "trial"
    assert user.is_active is True
    assert user.is_verified is True
    assert user.jhome_sub == "sub-2"
    assert user.hashed_password  # set, but not to anything guessable
    s.close()


def test_handoff_username_collision_appends_a_suffix(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="sam", email="sam-original@example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "sam@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    s = TestingSession()
    user = s.query(User).filter(User.email == "sam@example.com").one()
    assert user.username == "sam2"
    s.close()


def test_jhome_sub_already_bound_to_a_different_email_is_refused(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="oldemail", email="old@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-moved"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "new@example.com", "jhome_sub": "sub-moved"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "linked_elsewhere"}


def test_derived_username_never_contains_a_dot(client, monkeypatch):
    from auth import validate_username
    c, TestingSession = client
    _configure(monkeypatch)
    resp = c.post(
        "/internal/handoff",
        json={"email": "first.last@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    s = TestingSession()
    user = s.query(User).filter(User.email == "first.last@example.com").one()
    assert validate_username(user.username) is None  # None == valid, per this codebase's convention
    s.close()


def test_password_login_never_matches_a_handoff_created_users_password(client, monkeypatch):
    """The stored hash is real bcrypt, just of a value nobody typed."""
    from auth import verify_password
    c, TestingSession = client
    _configure(monkeypatch)
    c.post(
        "/internal/handoff",
        json={"email": "nopassword@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    s = TestingSession()
    user = s.query(User).filter(User.email == "nopassword@example.com").one()
    for guess in ("", "password", "nopassword@example.com", "12345678"):
        assert not verify_password(guess, user.hashed_password)
    s.close()


def test_deactivated_user_is_refused_with_403_not_401(client, monkeypatch):
    """403, not 401. The Backoffice logs an ERROR-level "handoff misconfigured
    (401)" on a 401 -- a false credential-rotation alarm every time a
    suspended customer clicks the tile. Matches Jhome Auth's own 403 +
    account_inactive for this condition."""
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="gone", email="gone@example.com", hashed_password="x",
                role="client", tier="trial", is_active=False))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "gone@example.com", "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"error": "account_inactive"}


def test_existing_user_jhome_sub_conflict_is_refused(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="linked", email="linked@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-original"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "linked@example.com", "jhome_sub": "sub-different",
              "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "linked_elsewhere"}


def test_matching_jhome_sub_on_an_existing_user_is_not_a_conflict(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="samesub", email="samesub@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-same"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "samesub@example.com", "jhome_sub": "sub-same",
              "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200


def test_unset_jhome_sub_on_an_existing_user_gets_adopted(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="fresh", email="fresh@example.com", hashed_password="x",
                role="client", tier="trial"))  # jhome_sub defaults to None
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "fresh@example.com", "jhome_sub": "sub-newly-linked",
              "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    s = TestingSession()
    user = s.query(User).filter(User.email == "fresh@example.com").one()
    assert user.jhome_sub == "sub-newly-linked"
    s.close()


def test_admin_jhome_sub_adoption_does_not_persist_on_refusal(client, monkeypatch):
    """A refused handoff (admin account) must leave zero side effects --
    including not silently linking jhome_sub before the refusal fires."""
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="adminuser", email="admin2@example.com", hashed_password="x",
                role="admin", tier="trial"))  # jhome_sub unset
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "admin2@example.com", "jhome_sub": "sub-should-not-stick",
              "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 403

    s = TestingSession()
    user = s.query(User).filter(User.email == "admin2@example.com").one()
    assert user.jhome_sub is None  # must NOT have been persisted
    s.close()


def test_wallet_link_failure_does_not_block_the_handoff(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)

    def boom(db, user):
        raise RuntimeError("Token Service is down")

    monkeypatch.setattr("routes.internal_routes.token_wallet.link_wallet_to_customer", boom)

    resp = c.post(
        "/internal/handoff",
        json={"email": "walletfail@example.com", "jhome_sub": "sub-fail"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    assert "consume_url" in resp.json()


def test_wallet_link_is_skipped_when_jhome_sub_is_absent(client, monkeypatch):
    """No customer_ref to link means nothing to call -- confirm link_wallet_to_customer
    is never even invoked, not just that it doesn't block."""
    c, TestingSession = client
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr("routes.internal_routes.token_wallet.link_wallet_to_customer",
                        lambda db, user: calls.append(user.id))

    resp = c.post(
        "/internal/handoff",
        json={"email": "nosub@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    assert calls == []


def test_wallet_grouping_reaches_an_existing_user_with_an_already_cached_wallet(
        client, monkeypatch):
    """End-to-end proof of the wallet-grouping fix: an EXISTING user
    (simulating one who signed up locally before ever connecting via Jhome)
    who ALREADY has a wallet still gets the grouping call when a handoff sets
    their jhome_sub. Under the old ensure_wallet call this user's cached
    wallet id short-circuited the Token Service call entirely, so they were
    never grouped -- the single most common real-world case."""
    c, TestingSession = client
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "routes.internal_routes.token_wallet.link_wallet_to_customer",
        lambda db, user: calls.append((user.id, user.jhome_sub)) or 999,
    )
    s = TestingSession()
    s.add(User(username="preexisting", email="preexisting@example.com", hashed_password="x",
                role="client", tier="trial", jts_wallet_id=42))  # already has a wallet
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "preexisting@example.com", "jhome_sub": "sub-preexisting",
              "email_verified": True},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0][1] == "sub-preexisting"


def test_unverified_caller_email_is_refused_for_an_existing_user(client, monkeypatch):
    """Binding jhome_sub onto an account matched only by email needs the
    Backoffice to have vouched for that address."""
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="existing", email="existing@example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "existing@example.com", "email_verified": False},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "unverified_caller_email"}


def test_omitted_email_verified_fails_closed_for_an_existing_user(client, monkeypatch):
    """The field defaults to False, so an old caller that does not send it at
    all is refused rather than silently vouched for."""
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="omitted", email="omitted@example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "omitted@example.com"},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "unverified_caller_email"}


def test_unverified_caller_email_does_not_bind_jhome_sub(client, monkeypatch):
    """A refusal must leave zero side effects -- the check runs BEFORE the
    adopt/conflict logic, so no sub is written on the way out."""
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="nobind", email="nobind@example.com", hashed_password="x",
                role="client", tier="trial"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/handoff",
        json={"email": "nobind@example.com", "jhome_sub": "sub-should-not-bind",
              "email_verified": False},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 409
    s = TestingSession()
    user = s.query(User).filter(User.email == "nobind@example.com").one()
    assert user.jhome_sub is None
    s.close()


def test_unverified_email_does_not_block_new_user_creation(client, monkeypatch):
    """The gate protects binding to an EXISTING account, not new signups.
    Jhome Auth's own /authorize gate already stops an unverified Backoffice
    session reaching here for a real caller."""
    c, _ = client
    _configure(monkeypatch)
    resp = c.post(
        "/internal/handoff",
        json={"email": "brandnew@example.com", "email_verified": False},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200


def test_full_backoffice_payload_shape_is_handled_without_error(client, monkeypatch):
    """Backoffice's shared client actually sends 5 fields (email, name,
    jhome_sub, domains, email_verified) to every connected app
    unconditionally, even ones like Gootier that don't need domains. Pin that
    this doesn't crash if a future Pydantic config change (e.g.
    extra='forbid') is accidentally introduced."""
    c, TestingSession = client
    _configure(monkeypatch)
    resp = c.post(
        "/internal/handoff",
        json={
            "email": "fullpayload@example.com",
            "name": "Full Payload",
            "jhome_sub": "sub-full",
            "domains": ["example.com", "example.org"],
            "email_verified": True,
        },
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert resp.status_code == 200
    # `domains` is dropped by Pydantic's default extra='ignore' -- confirm it
    # did not leak onto the created row through some other path.
    s = TestingSession()
    user = s.query(User).filter(User.email == "fullpayload@example.com").one()
    assert not hasattr(user, "domains")
    assert user.jhome_sub == "sub-full"
    s.close()
