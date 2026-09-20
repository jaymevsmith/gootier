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


from models import SocialConnection


def test_social_connections_lists_only_this_users_active_connections(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-sc")
    other = User(username="bob", email="bob@example.com", hashed_password="x",
                role="client", tier="trial")
    s.add_all([owner, other])
    s.commit()
    s.add_all([
        SocialConnection(user_id=owner.id, platform="facebook", account_name="Jane's Page",
                         access_token="t", is_active=True),
        SocialConnection(user_id=owner.id, platform="linkedin", account_name="Jane LI",
                         access_token="t", is_active=False),
        SocialConnection(user_id=other.id, platform="facebook", account_name="Bob's Page",
                         access_token="t", is_active=True),
    ])
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/social-connections",
        json={"email": "jane@example.com", "jhome_sub": "sub-sc", "email_verified": True},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    conns = resp.json()["connections"]
    assert len(conns) == 1
    assert conns[0]["platform"] == "facebook"
    assert conns[0]["display_name"] == "Jane's Page"


from unittest.mock import AsyncMock, patch

from models import SocialPost, TierConfig


def _seed_bronze_tier(s, **quota_overrides):
    import json
    quotas = {"posts_per_month": 100, "blasts_per_month": 10, "blast_recipients": 500,
              "ai_generations_per_month": 20, **quota_overrides}
    s.add(TierConfig(tier="bronze", perms_json=json.dumps({
        "marketing.social_post": True, "marketing.email_blast": True,
        "marketing.ai_generate": True,
    }), quotas_json=json.dumps(quotas)))
    s.commit()


def test_schedule_post_rejects_a_connection_the_user_does_not_own(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-sp1")
    s.add(owner)
    s.commit()
    other_conn = SocialConnection(user_id=999, platform="facebook", account_name="Not yours",
                                  access_token="t", is_active=True)
    s.add(other_conn)
    s.commit()
    conn_id = other_conn.id
    s.close()

    resp = c.post(
        "/internal/mcp/schedule-post",
        json={"email": "jane@example.com", "jhome_sub": "sub-sp1", "email_verified": True,
             "content": "hello", "connection_ids": [conn_id]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_connections"


@patch("routes.internal_routes.publish_to_connections", new_callable=AsyncMock)
def test_schedule_post_publishes_immediately_when_unscheduled(mock_publish, client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-sp2")
    s.add(owner)
    s.commit()
    conn = SocialConnection(user_id=owner.id, platform="facebook", account_name="Jane's Page",
                            access_token="t", is_active=True)
    s.add(conn)
    s.commit()
    conn_id = conn.id
    s.close()

    mock_publish.return_value = {conn_id: {"success": True}}

    resp = c.post(
        "/internal/mcp/schedule-post",
        json={"email": "jane@example.com", "jhome_sub": "sub-sp2", "email_verified": True,
             "content": "hello world", "connection_ids": [conn_id]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"

    s = TestingSession()
    post = s.query(SocialPost).filter(SocialPost.id == resp.json()["id"]).first()
    assert post.status == "published"
    s.close()


def test_schedule_post_enforces_the_monthly_quota(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s, posts_per_month=0)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-sp3")
    s.add(owner)
    s.commit()
    conn = SocialConnection(user_id=owner.id, platform="facebook", account_name="Jane's Page",
                            access_token="t", is_active=True)
    s.add(conn)
    s.commit()
    conn_id = conn.id
    s.close()

    resp = c.post(
        "/internal/mcp/schedule-post",
        json={"email": "jane@example.com", "jhome_sub": "sub-sp3", "email_verified": True,
             "content": "hello", "connection_ids": [conn_id]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "posts_quota_exceeded"


def test_schedule_post_rejects_an_empty_connection_list(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-sp4")
    s.add(owner)
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/schedule-post",
        json={"email": "jane@example.com", "jhome_sub": "sub-sp4", "email_verified": True,
             "content": "hello", "connection_ids": []},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_connections"


from models import EmailBlast


@patch("routes.internal_routes.send_blast_email")
def test_schedule_email_blast_sends_immediately_when_unscheduled(mock_send, client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-eb1")
    s.add(owner)
    s.commit()
    s.close()

    mock_send.return_value = (2, 0)

    resp = c.post(
        "/internal/mcp/schedule-email-blast",
        json={"email": "jane@example.com", "jhome_sub": "sub-eb1", "email_verified": True,
             "subject": "Hi", "body_html": "<p>hi</p>",
             "recipients": ["a@x.com", "b@x.com"]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"

    s = TestingSession()
    blast = s.query(EmailBlast).filter(EmailBlast.id == resp.json()["id"]).first()
    assert blast.sent_count == 2
    s.close()


def test_schedule_email_blast_enforces_the_recipient_cap(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s, blast_recipients=1)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-eb2")
    s.add(owner)
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/schedule-email-blast",
        json={"email": "jane@example.com", "jhome_sub": "sub-eb2", "email_verified": True,
             "subject": "Hi", "body_html": "<p>hi</p>",
             "recipients": ["a@x.com", "b@x.com"]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "recipient_cap_exceeded"


def test_schedule_email_blast_refuses_a_trial_tier_caller(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    import json
    s.add(TierConfig(tier="trial", perms_json=json.dumps({"marketing.email_blast": False}),
                     quotas_json=json.dumps({"blasts_per_month": 0, "blast_recipients": 0})))
    s.commit()
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-eb3")
    s.add(owner)
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/schedule-email-blast",
        json={"email": "jane@example.com", "jhome_sub": "sub-eb3", "email_verified": True,
             "subject": "Hi", "body_html": "<p>hi</p>", "recipients": ["a@x.com"]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "plan_upgrade_required"


@patch("routes.internal_routes.generate_campaign")
def test_draft_campaign_returns_the_generated_items(mock_generate, client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-dc1")
    s.add(owner)
    s.commit()
    s.close()

    mock_generate.return_value = {"items": [{"kind": "social_post", "content": "Buy now"}]}

    resp = c.post(
        "/internal/mcp/draft-campaign",
        json={"email": "jane@example.com", "jhome_sub": "sub-dc1", "email_verified": True,
             "plan": "A brand new coffee shop opening downtown next month.",
             "schedule": "weekly", "count": 3, "channels": ["social_post"]},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"kind": "social_post", "content": "Buy now"}]}


def test_draft_campaign_enforces_the_monthly_quota(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s, ai_generations_per_month=0)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-dc2")
    s.add(owner)
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/draft-campaign",
        json={"email": "jane@example.com", "jhome_sub": "sub-dc2", "email_verified": True,
             "plan": "A brand new coffee shop opening downtown next month."},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "ai_generations_quota_exceeded"


def test_mcp_routes_never_link_the_wallet(client, monkeypatch):
    """The design spec is explicit: none of the 5 /internal/mcp/* routes
    bill Jhome tokens this slice, so none of them should link the wallet --
    unlike /internal/handoff, which still must. Found during the Part A
    whole-branch review: resolve_or_create_gootier_user inherited
    handoff()'s unconditional wallet-link, silently contradicting that
    intent for every MCP route."""
    c, TestingSession = client
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "routes.internal_routes.token_wallet.link_wallet_to_customer",
        lambda db, user: calls.append(user.id) or 999,
    )

    resp = c.post(
        "/internal/mcp/ensure-account",
        json={"email": "new-wallet-check@example.com", "jhome_sub": "sub-wallet-1",
             "email_verified": True},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 200
    assert calls == []


def test_schedule_email_blast_rejects_an_empty_recipient_list(client, monkeypatch):
    c, TestingSession = client
    _configure(monkeypatch)
    s = TestingSession()
    _seed_bronze_tier(s)
    owner = User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze", jhome_sub="sub-eb4")
    s.add(owner)
    s.commit()
    s.close()

    resp = c.post(
        "/internal/mcp/schedule-email-blast",
        json={"email": "jane@example.com", "jhome_sub": "sub-eb4", "email_verified": True,
             "subject": "Hi", "body_html": "<p>hi</p>", "recipients": []},
        headers={"X-Internal-Key": "test-mcp-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_recipients"
