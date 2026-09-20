from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from database import Base, get_db
from models import User
from routes import internal_routes


@pytest.fixture
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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
    monkeypatch.setattr(
        "routes.internal_routes.get_env",
        lambda key, default="": {"GOOTIER_MCP_INTERNAL_KEY": "test-mcp-key"}.get(key, default),
    )


def test_ensure_account_without_the_key_is_refused(client):
    c, _ = client
    resp = c.post("/internal/mcp/ensure-account",
                  json={"email": "a@example.com", "jhome_sub": "s", "email_verified": True})
    assert resp.status_code == 401


def test_ensure_account_creates_and_returns_the_user(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)

    resp = c.post(
        "/internal/mcp/ensure-account",
        json={"email": "new@example.com", "jhome_sub": "sub-1", "email_verified": True},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "trial"
    assert resp.headers["cache-control"] == "no-store"

    s = TestingSession()
    user = s.query(User).filter(User.id == body["user_id"]).first()
    assert user.email == "new@example.com"
    s.close()


def test_ensure_account_reuses_an_existing_user_by_email(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    s.add(User(username="jane", email="jane@example.com", hashed_password="x",
               role="client", tier="silver"))
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/ensure-account",
        json={"email": "jane@example.com", "jhome_sub": "sub-2", "email_verified": True},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["tier"] == "silver"
