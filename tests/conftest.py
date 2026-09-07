"""Shared pytest fixtures: an isolated in-memory SQLite database.

Isolation here has to cover two separate ways request-handling code reaches
the database, because covering only the first one is what made
tests/test_affiliates_integration.py's signup tests depend on whether a
developer happened to have a `./gootier.db` file lying around:

1. The injected `Session` — routes take `db: Session = Depends(get_db)`, and
   tests swap that out with `app.dependency_overrides[get_db]`.

2. `database.SessionLocal()` opened directly — `services/env_config.get_env()`
   (and therefore anything reading a runtime-configurable key: `_app_url()`,
   `JTSClient.__init__`, the SMTP helpers) opens its *own* session rather than
   taking one as an argument. `dependency_overrides` cannot reach that, so
   without the `_isolate_session_local` fixture below those reads go to the
   module-level engine bound to `DATABASE_URL` — i.e. the developer's real
   `sqlite:///./gootier.db`. In a checkout that has that file the tests
   silently read (and could write) live local data; in a fresh clone or a git
   worktree, where the file doesn't exist, SQLite creates an empty one and
   every such read blows up with `no such table: env_configs`.

`SessionLocal` is a `sessionmaker`, and `configure()` mutates it in place, so
rebinding it here reaches every module that did `from database import
SessionLocal` at import time.
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
from database import Base, get_db
import models  # noqa: F401 — ensures all models register on Base.metadata
from routes import auth_routes
from services.csrf import CSRFCookieMiddleware


@pytest.fixture
def test_engine():
    """One in-memory SQLite database per test, with the full schema created.

    `StaticPool` + `check_same_thread=False` keep every connection pointed at
    the same in-memory database, so the request session, the `db` fixture and
    any `SessionLocal()` opened deep inside the code under test all see the
    same rows (and so TestClient's worker thread can use it).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _isolate_session_local(test_engine):
    """Point `database.SessionLocal` at the test database for every test.

    Autouse on purpose: this is a safety net, not an opt-in. Any code path
    that opens its own session must not be able to reach `./gootier.db`, and
    a test author cannot be expected to know which transitive call does that.
    """
    original_bind = database.SessionLocal.kw.get("bind")
    database.SessionLocal.configure(bind=test_engine)
    try:
        yield
    finally:
        database.SessionLocal.configure(bind=original_bind)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail loudly on any real outbound HTTP instead of letting it through.

    `JTSClient` defaults `TOKEN_SERVICE_URL` to the *production* Jhome Token
    Service, so an unpatched `ensure_wallet()` in a test posts a wallet
    creation for a fake user to the live service — and then passes. Blocking
    the real transport turns that into an obvious failure. `httpx.MockTransport`
    never goes through `HTTPTransport`, so the tests that fake JTS responses
    are unaffected.
    """
    def _blocked(self, request, *args, **kwargs):
        raise RuntimeError(
            f"Real network call attempted in a test: {request.method} {request.url}. "
            "Patch the client (see tests/test_token_wallet.py) or use "
            "httpx.MockTransport (see tests/test_jts_client.py)."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


@pytest.fixture
def db(test_engine):
    Session = sessionmaker(bind=test_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(test_engine):
    """A minimal app wired with just auth_routes, the shared per-test in-memory
    DB, and the CSRF cookie middleware (auth_routes' signup/login POSTs
    require Depends(verify_csrf)).

    Deliberately not the full main.py app, which starts a background scheduler
    on lifespan. Uses `test_engine` rather than building its own, so the request
    session and the sessions `get_env()` opens internally (via
    `_isolate_session_local`) are the same database — see the module docstring
    above for why that matters.
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


@pytest.fixture
def unavailable_config_db(test_engine, _isolate_session_local):
    """Make every `get_env()` lookup fail the way a real database blip would.

    `get_env()` reads config through one of two sessions — the caller's, when
    one is passed, or a short-lived `SessionLocal()` when not — so this closes
    both routes:

    1. `env_configs` is dropped from `test_engine`, so the read fails on the
       request's own session.
    2. `database.SessionLocal` is pointed at a schema-less engine, so it fails
       for callers that pass no session.

    Doing only (2) is a trap. It was enough while every `get_env()` opened its
    own session, but once `_app_url()` and `_smtp_config()` started taking
    `db=`, the request path stopped touching `SessionLocal` at all — and three
    tests in this suite silently went vacuous, passing with their handler's
    try/except deleted. Keep both halves.
    """
    models.EnvConfig.__table__.drop(bind=test_engine)

    orphan = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # No create_all() on purpose — that is the whole point of this engine.
    original_bind = database.SessionLocal.kw.get("bind")
    database.SessionLocal.configure(bind=orphan)
    try:
        yield
    finally:
        database.SessionLocal.configure(bind=original_bind)
        orphan.dispose()
        models.EnvConfig.__table__.create(bind=test_engine)


@pytest.fixture
def orphaned_session_local():
    """Break `SessionLocal` only — leave `test_engine` healthy.

    Narrower than `unavailable_config_db`, which breaks both routes into
    `env_configs`. Use this one to prove a *supplied* session makes
    `SessionLocal` irrelevant: exactly one of the two has to be broken for that
    to mean anything.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_bind = database.SessionLocal.kw.get("bind")
    database.SessionLocal.configure(bind=engine)
    try:
        yield
    finally:
        database.SessionLocal.configure(bind=original_bind)
        engine.dispose()
