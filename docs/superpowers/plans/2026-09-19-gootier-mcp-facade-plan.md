# Gootier MCP Façade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Gootier customers the ability to connect their own AI assistant to their Gootier account and draft an AI campaign, schedule a social post, schedule an email blast, or list connected social channels — mirroring the MidCanvas/Jhome-MCP-Server façade already live in production.

**Architecture:** Two repos change. (A) Gootier gains 5 new `/internal/mcp/*` routes guarded by a new `GOOTIER_MCP_INTERNAL_KEY`. (B) A new standalone repo, `Gootier-MCP-Server`, is built from scratch — FastAPI + SQLModel + Postgres, structurally mirroring Jhome-MCP-Server (its own `Customer`/`ConnectionKey` tables, its own Backoffice-handoff entry point, its own `/connect` UI, its own 4 MCP tool registrations) but trimmed of everything Gootier doesn't need this slice (no token wallet linking, no embeddings/memory). (C) Backoffice gets one small additive registry entry so a Gootier customer can reach the new server from their dashboard.

**Tech Stack:** FastAPI, SQLAlchemy ORM (Gootier) / SQLModel (new repo), Postgres, httpx, itsdangerous, `mcp` SDK, pytest.

**Reference spec:** `docs/superpowers/specs/2026-09-15-gootier-mcp-facade-design.md` (this same worktree).

---

## Deviations from the spec, decided during planning

1. **`HTTPGootierClient.TIMEOUT` is 900.0s, not the spec's illustrative 300s.** The MidCanvas precedent (330s) covers one bounded image-generation call. Gootier's own `schedule-post` publishes to each connection *sequentially*, up to 120s per platform (`services/social_publish.py`), and `schedule-email-blast` sends synchronously to up to 25,000 recipients on a Gold-tier account. Both are genuinely slower worst cases than MidCanvas's, so the client timeout is set to match — 900s, not blindly copied. **Known limitation this plan does not fix:** an extreme blast could still exceed even 900s; making that path truly safe means moving Gootier's own blast-send to a background job, which is a Gootier-side architectural change out of scope here. Flagged, not silently absorbed.
2. **Gootier-MCP-Server drops fields/subsystems Jhome-MCP-Server has that this slice doesn't use**, per YAGNI: no `token_wallet_id` on `Customer` (this slice's `ensure-account` deliberately never links a wallet — see spec), no `Memory` table, no `pgvector`/embeddings dependency, no `/billing/tokens` page or token-service client. Copying them in "for consistency" would be speculative scope with nothing to exercise it.
3. **The new repo's own Backoffice-facing `/internal/handoff` mirrors Jhome-MCP-Server's CURRENT `app/internal_api.py` exactly** (the `_admit`/`_create_customer`/`_may_hand_over` design with explicit `unverified_account` gating on binding), not an earlier, less-defended version — this is the most adversarially-reviewed identity-resolution code in the fleet and there is no reason to regress it for a new repo.

---

## File structure

**Gootier repo** (worktree `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/.worktrees/gootier-mcp`, branch `feat/gootier-mcp-facade`):
- Modify: `routes/internal_routes.py` — add `resolve_or_create_gootier_user`, `require_mcp_internal_key`, 5 new routes
- Modify: `models.py` — register `GOOTIER_MCP_INTERNAL_KEY` in `KNOWN_ENV_KEYS`
- Create: `tests/test_resolve_or_create_gootier_user.py`
- Create: `tests/test_internal_mcp_routes.py`

**New repo** (`/Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server`, created fresh — no worktree, this is a brand-new git history):
```
Gootier-MCP-Server/
├── Procfile
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── alembic.ini
├── alembic/{env.py, versions/0001_baseline.py}
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── models.py                 (Customer, ConnectionKey)
│   ├── internal_api.py           (Backoffice → this app handoff)
│   ├── main.py                   (FastAPI app factory, MCP mount)
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── keys.py               (connection-key mint/hash/verify)
│   │   ├── handoff.py            (short-lived /enter token)
│   │   ├── context.py            (contextvar current customer id)
│   │   └── middleware.py         (Authorization: Bearer -> customer_id)
│   ├── billing/
│   │   ├── __init__.py
│   │   └── gootier_client.py     (GootierError/Protocol/HTTP/Fake/get_*)
│   ├── mcp_app/
│   │   ├── __init__.py
│   │   ├── server.py             (register_tools, open_session, current_customer)
│   │   └── tools_gootier.py      (4 tool wrapper functions)
│   └── web/
│       ├── __init__.py
│       ├── routes.py             (/, /enter, /connect, /connect/keys)
│       ├── static/                (copied+adapted from Jhome-MCP-Server)
│       └── templates/             (copied+adapted from Jhome-MCP-Server)
└── tests/                          (mirrors app/ 1:1)
```

**jhome-backoffice repo** (primary checkout, direct commit to `main` — no PR gate on this repo per established convention):
- Modify: `app/config.py` — add `gootier_mcp_internal_url`/`gootier_mcp_internal_key`
- Modify: `app/connected/registry.py` — add the `gootier-mcp` `ConnectedApp` row

---

# PART A — Gootier: 5 new internal routes

## Task 1: Extract `resolve_or_create_gootier_user`, refactor `handoff()` to use it

**Files:**
- Modify: `routes/internal_routes.py`
- Create: `tests/test_resolve_or_create_gootier_user.py`
- Test (existing, must keep passing unchanged): `tests/test_internal_handoff.py`

This is a **behavior-preserving refactor**: `handoff()`'s existing inline identity logic moves into a standalone function so every new `/internal/mcp/*` route can call it too — mirroring MidCanvas's `generate_image_for_mcp` precedent, so the system works even if a customer's AI client never explicitly calls `ensure-account` first.

- [ ] **Step 1: Write the failing tests for the extracted helper**

```python
# tests/test_resolve_or_create_gootier_user.py
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from database import Base
from models import User
from routes.internal_routes import resolve_or_create_gootier_user


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def test_a_brand_new_email_creates_a_user(db):
    user = resolve_or_create_gootier_user(
        db, jhome_sub="sub-1", email="new@example.com", email_verified=True, name="New Person")
    assert user.id is not None
    assert user.email == "new@example.com"
    assert user.jhome_sub == "sub-1"
    assert user.tier == "trial"


def test_an_existing_user_found_by_email_is_reused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze"))
    db.commit()

    user = resolve_or_create_gootier_user(
        db, jhome_sub="sub-2", email="jane@example.com", email_verified=True)
    assert user.username == "jane"
    assert user.tier == "bronze"
    assert user.jhome_sub == "sub-2"


def test_a_jhome_sub_already_bound_to_a_different_user_is_refused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-taken"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-taken", email="someone-else@example.com", email_verified=True)
    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": "linked_elsewhere"}


def test_an_inactive_user_is_refused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial", is_active=False))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-3", email="jane@example.com", email_verified=True)
    assert exc.value.status_code == 403
    assert exc.value.detail == {"error": "account_inactive"}


def test_an_admin_account_is_refused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="admin", tier="trial"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-4", email="jane@example.com", email_verified=True)
    assert exc.value.status_code == 403
    assert exc.value.detail == {"error": "admin_account_not_supported"}


def test_an_unverified_caller_cannot_bind_an_existing_account(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-5", email="jane@example.com", email_verified=False)
    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": "unverified_caller_email"}
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_resolve_or_create_gootier_user.py -v`
Expected: every test errors with `ImportError: cannot import name 'resolve_or_create_gootier_user'`.

- [ ] **Step 3: Extract the helper and refactor `handoff()`**

In `routes/internal_routes.py`, insert the new function directly above `@router.post("/internal/handoff"...)` (after `_create_user`):

```python
def resolve_or_create_gootier_user(
    db: Session, *, jhome_sub: str | None, email: str, email_verified: bool,
    name: str | None = None,
) -> User:
    """Find-or-create a Gootier user by email, mirroring handoff()'s own
    refusal logic exactly -- every one of the 5 /internal/mcp/* routes calls
    this directly (not only ensure-account), so the system works correctly
    even if a customer's AI client never calls ensure-account first,
    matching MidCanvas's generate_image_for_mcp precedent.

    `email` must already be normalized (.strip().lower()) by the caller.
    """
    matches = db.query(User).filter(func.lower(User.email) == email).order_by(User.id).all()
    if len(matches) > 1:
        log.warning("mcp identity refused: %d case-variant accounts for email %s", len(matches), email)
        raise HTTPException(status_code=409, detail={"error": "ambiguous_identity"})
    user = matches[0] if matches else None

    if user is not None and not user.is_active:
        log.warning("mcp identity refused: user %s is deactivated", user.id)
        raise HTTPException(status_code=403, detail={"error": "account_inactive"})

    if user is not None and not email_verified:
        log.warning("mcp identity refused: caller did not assert email_verified for user %s", user.id)
        raise HTTPException(status_code=409, detail={"error": "unverified_caller_email"})

    if user is None and jhome_sub:
        existing_sub_holder = db.query(User).filter(User.jhome_sub == jhome_sub).first()
        if existing_sub_holder is not None:
            log.warning(
                "mcp identity refused: jhome_sub %s already belongs to a different user (%s), "
                "but the request's email does not match that user",
                jhome_sub, existing_sub_holder.id,
            )
            raise HTTPException(status_code=409, detail={"error": "linked_elsewhere"})

    if user is None:
        user = _create_user(db, email, jhome_sub, name)
    elif jhome_sub and not user.jhome_sub:
        user.jhome_sub = jhome_sub
        db.commit()
        db.refresh(user)
    elif jhome_sub and user.jhome_sub and user.jhome_sub != jhome_sub:
        log.warning(
            "mcp identity refused: user %s carried jhome_sub %s but it already has %s",
            user.id, jhome_sub, user.jhome_sub,
        )
        raise HTTPException(status_code=409, detail={"error": "linked_elsewhere"})

    if user.has_role("admin"):
        log.warning("mcp identity refused: user %s has platform admin access", user.id)
        raise HTTPException(status_code=403, detail={"error": "admin_account_not_supported"})

    if user.jhome_sub:
        try:
            token_wallet.link_wallet_to_customer(db, user)
        except Exception:  # noqa: BLE001 -- must never fail an identity resolution on this
            log.exception("could not link wallet for user %s", user.id)

    return user
```

Then replace the body of `handoff()` (everything between the email-normalization check and the `HandoffToken` creation) so it reads:

```python
@router.post("/internal/handoff", dependencies=[Depends(require_internal_key)])
def handoff(req: HandoffRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"

    app_url = get_env("APP_URL", "").rstrip("/")
    if not app_url:
        raise HTTPException(status_code=500, detail="APP_URL is not configured")

    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid email")

    user = resolve_or_create_gootier_user(
        db, jhome_sub=req.jhome_sub, email=email,
        email_verified=req.email_verified, name=req.name,
    )

    token = generate_token()
    db.add(HandoffToken(token_hash=hash_token(token), user_id=user.id,
                         expires_at=default_expiry()))
    db.commit()

    log.info("handoff minted token for user %s", user.id)
    log_action(db, user, "BACKOFFICE_HANDOFF", "User", str(user.id))

    return {"consume_url": f"{app_url}/sso/consume?token={token}"}
```

- [ ] **Step 4: Run both test files, confirm everything passes**

Run: `pytest tests/test_resolve_or_create_gootier_user.py tests/test_internal_handoff.py -v`
Expected: all 6 new tests PASS, and all 23 existing `test_internal_handoff.py` tests PASS UNCHANGED (proving the refactor is behavior-preserving).

- [ ] **Step 5: Commit**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/.worktrees/gootier-mcp
git add routes/internal_routes.py tests/test_resolve_or_create_gootier_user.py
git commit -m "refactor: extract resolve_or_create_gootier_user from handoff()"
```

---

## Task 2: MCP auth guard + `POST /internal/mcp/ensure-account`

**Files:**
- Modify: `routes/internal_routes.py`
- Modify: `models.py` (register the new env key)
- Create: `tests/test_internal_mcp_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_internal_mcp_routes.py
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
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_internal_mcp_routes.py -v`
Expected: FAIL — `404 Not Found` (the route doesn't exist yet); `test_ensure_account_without_the_key_is_refused` fails because 404 != 401.

- [ ] **Step 3: Add the guard, the env key, and the route**

In `models.py`, insert immediately after the `GOOTIER_INTERNAL_KEY` line (line 400) in `KNOWN_ENV_KEYS`:

```python
    ("GOOTIER_MCP_INTERNAL_KEY",  "auth",   True,  False, "Shared secret the Gootier-MCP-Server presents as X-Internal-Key on POST /internal/mcp/*. Empty = every MCP route fails closed (401 on every call)."),
```

In `routes/internal_routes.py`, add to the import block:

```python
from datetime import datetime
from typing import List

from pydantic import BaseModel, Field

from auth import _load_permissions, hash_password
from models import EmailBlast, HandoffToken, SocialConnection, SocialPost, User, log_action
from services.ai_generator import generate_campaign
from services.quotas import check_and_raise, check_per_call
from services.social_publish import publish_to_connections
```
(keep the existing `hash_password`/`HandoffToken, User, log_action` imports — just widen them to include the new names above; do not duplicate the import lines.)

Add the guard function directly below `require_internal_key`:

```python
def require_mcp_internal_key(x_internal_key: str = Header(default="")) -> None:
    expected = get_env("GOOTIER_MCP_INTERNAL_KEY", "")
    if not expected or not secrets.compare_digest(
        x_internal_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid internal key")
```

Add the shared request base and the identity-resolution + error-normalization helpers, directly below `resolve_or_create_gootier_user`:

```python
class _McpIdentityRequest(BaseModel):
    jhome_sub: str | None = None
    email: str
    email_verified: bool = False


def _resolve_mcp_identity(db: Session, req: "_McpIdentityRequest") -> User:
    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail={"error": "invalid_email"})
    user = resolve_or_create_gootier_user(
        db, jhome_sub=req.jhome_sub, email=email,
        email_verified=req.email_verified, name=None,
    )
    # Task 1's implementer found and fixed a real bug: resolve_or_create_gootier_user
    # deliberately does NOT commit a jhome_sub adoption itself (an eager commit
    # there broke handoff()'s own "nothing persists if a LATER check in that
    # function refuses the request" guarantee). By the time this helper's call
    # returns successfully, every identity-related refusal inside
    # resolve_or_create_gootier_user (ambiguous/inactive/unverified/linked_elsewhere/
    # admin) has already passed -- a route-level permission or quota refusal AFTER
    # this point is orthogonal to identity and must not roll the binding back. So
    # this is the right place to commit it, once, for all 5 routes.
    db.commit()
    _load_permissions(db, user)
    return user


def _raise_quota_error(exc: HTTPException, code: str) -> None:
    """Re-raise a plain-string-detail HTTPException from services/quotas.py
    as this router's own structured shape, preserving the original
    human-readable string as `message` so customer-facing copy can still
    reflect Gootier's real limits without the MCP tool layer string-matching
    UI prose."""
    raise HTTPException(status_code=exc.status_code,
                        detail={"error": code, "message": exc.detail}) from exc


class EnsureAccountRequest(_McpIdentityRequest):
    pass


@router.post("/internal/mcp/ensure-account", dependencies=[Depends(require_mcp_internal_key)])
def mcp_ensure_account(req: EnsureAccountRequest, response: Response,
                       db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)
    return {"user_id": user.id, "tier": user.tier}
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_internal_mcp_routes.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add routes/internal_routes.py models.py tests/test_internal_mcp_routes.py
git commit -m "feat: GOOTIER_MCP_INTERNAL_KEY guard + POST /internal/mcp/ensure-account"
```

---

## Task 3: `POST /internal/mcp/social-connections`

**Files:**
- Modify: `routes/internal_routes.py`
- Modify: `tests/test_internal_mcp_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_internal_mcp_routes.py`:

```python
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
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `pytest tests/test_internal_mcp_routes.py::test_social_connections_lists_only_this_users_active_connections -v`
Expected: FAIL — 404 Not Found.

- [ ] **Step 3: Add the route**

In `routes/internal_routes.py`, directly below `mcp_ensure_account`:

```python
class SocialConnectionsRequest(_McpIdentityRequest):
    pass


@router.post("/internal/mcp/social-connections", dependencies=[Depends(require_mcp_internal_key)])
def mcp_social_connections(req: SocialConnectionsRequest, response: Response,
                           db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)
    conns = db.query(SocialConnection).filter(
        SocialConnection.user_id == user.id,
        SocialConnection.is_active == True,  # noqa: E712
    ).all()
    return {"connections": [
        {"id": c.id, "platform": c.platform, "display_name": c.account_name}
        for c in conns
    ]}
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_internal_mcp_routes.py -v`
Expected: all tests PASS (4 total so far).

- [ ] **Step 5: Commit**

```bash
git add routes/internal_routes.py tests/test_internal_mcp_routes.py
git commit -m "feat: POST /internal/mcp/social-connections"
```

---

## Task 4: `POST /internal/mcp/schedule-post`

**Files:**
- Modify: `routes/internal_routes.py`
- Modify: `tests/test_internal_mcp_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_internal_mcp_routes.py`:

```python
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
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_internal_mcp_routes.py -k schedule_post -v`
Expected: FAIL — 404 Not Found on all three.

- [ ] **Step 3: Add the route**

In `routes/internal_routes.py`, directly below `mcp_social_connections`:

```python
class SchedulePostRequest(_McpIdentityRequest):
    content: str = Field(..., min_length=1, max_length=5000)
    connection_ids: List[int]
    image_url: str | None = None
    video_url: str | None = None
    link_url: str | None = None
    scheduled_at: datetime | None = None


@router.post("/internal/mcp/schedule-post", dependencies=[Depends(require_mcp_internal_key)])
async def mcp_schedule_post(req: SchedulePostRequest, response: Response,
                            db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)

    owned = db.query(SocialConnection).filter(
        SocialConnection.id.in_(req.connection_ids),
        SocialConnection.user_id == user.id,
        SocialConnection.is_active == True,  # noqa: E712
    ).all()
    if len(owned) != len(req.connection_ids):
        raise HTTPException(status_code=400,
                            detail={"error": "invalid_connections",
                                    "message": "One or more connections invalid"})

    if not user.perm("marketing.social_post"):
        raise HTTPException(status_code=403,
                            detail={"error": "plan_upgrade_required",
                                    "message": "Requires permission: marketing.social_post"})
    try:
        check_and_raise(db, user, "posts_per_month")
    except HTTPException as exc:
        _raise_quota_error(exc, "posts_quota_exceeded")

    post = SocialPost(
        user_id=user.id,
        content=req.content,
        image_url=req.image_url,
        video_url=req.video_url,
        link_url=req.link_url,
        connection_ids=",".join(str(c.id) for c in owned),
        scheduled_at=req.scheduled_at,
        status="pending",
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    if not req.scheduled_at:
        results = await publish_to_connections(
            owned, post.content, link_url=post.link_url,
            image_url=post.image_url, video_url=post.video_url,
        )
        successes = sum(1 for r in results.values() if r.get("success"))
        post.status = ("published" if successes == len(owned)
                       else "partial" if successes else "failed")
        post.published_at = datetime.utcnow()
        import json as _json
        post.publish_results = _json.dumps({str(k): v for k, v in results.items()})
        db.commit()

    log_action(db, user, "CREATE", "SocialPost", str(post.id), detail="via MCP")
    return {"id": post.id, "status": post.status}
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_internal_mcp_routes.py -v`
Expected: all tests PASS (7 total so far).

- [ ] **Step 5: Commit**

```bash
git add routes/internal_routes.py tests/test_internal_mcp_routes.py
git commit -m "feat: POST /internal/mcp/schedule-post"
```

---

## Task 5: `POST /internal/mcp/schedule-email-blast`

**Files:**
- Modify: `routes/internal_routes.py`
- Modify: `tests/test_internal_mcp_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_internal_mcp_routes.py`:

```python
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
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_internal_mcp_routes.py -k schedule_email_blast -v`
Expected: FAIL — 404 Not Found on all three.

- [ ] **Step 3: Add the route**

In `routes/internal_routes.py`, add `from services.email_utils import send_blast_email` to the import block (module-level, matching where `/api/email-blasts` imports it — actually `/api/email-blasts` imports it lazily inside the function; do the same here for consistency AND because it keeps the `@patch("routes.internal_routes.send_blast_email")` in the tests above working, since a lazy `from services.email_utils import send_blast_email` inside the function body resolves the patched name in `routes.internal_routes` at call time only if the patch target matches the module attribute — **import it at module level instead**, so the test's `@patch("routes.internal_routes.send_blast_email")` has a real attribute to patch):

```python
from services.email_utils import send_blast_email
```

Then, directly below `mcp_schedule_post`:

```python
class ScheduleEmailBlastRequest(_McpIdentityRequest):
    subject: str = Field(..., min_length=1, max_length=200)
    body_html: str = Field(..., min_length=1)
    recipients: List[str]
    scheduled_at: datetime | None = None


@router.post("/internal/mcp/schedule-email-blast", dependencies=[Depends(require_mcp_internal_key)])
def mcp_schedule_email_blast(req: ScheduleEmailBlastRequest, response: Response,
                             db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)

    if not user.perm("marketing.email_blast"):
        raise HTTPException(status_code=403,
                            detail={"error": "plan_upgrade_required",
                                    "message": "Requires permission: marketing.email_blast"})
    try:
        check_and_raise(db, user, "blasts_per_month")
    except HTTPException as exc:
        _raise_quota_error(exc, "blasts_quota_exceeded")
    try:
        check_per_call(db, user, "blast_recipients", len(req.recipients))
    except HTTPException as exc:
        _raise_quota_error(exc, "recipient_cap_exceeded")

    blast = EmailBlast(
        user_id=user.id,
        subject=req.subject,
        body_html=req.body_html,
        recipient_list="\n".join(req.recipients),
        recipient_count=len(req.recipients),
        scheduled_at=req.scheduled_at,
        status="pending",
    )
    db.add(blast)
    db.commit()
    db.refresh(blast)

    if not req.scheduled_at:
        sent, failed = send_blast_email(blast.subject, blast.body_html, req.recipients)
        blast.sent_count = sent
        blast.failed_count = failed
        blast.status = ("sent" if failed == 0 and sent > 0
                        else "partial" if sent > 0 else "failed")
        db.commit()

    log_action(db, user, "CREATE", "EmailBlast", str(blast.id), detail="via MCP")
    return {"id": blast.id, "status": blast.status}
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_internal_mcp_routes.py -v`
Expected: all tests PASS (10 total so far).

- [ ] **Step 5: Commit**

```bash
git add routes/internal_routes.py tests/test_internal_mcp_routes.py
git commit -m "feat: POST /internal/mcp/schedule-email-blast"
```

---

## Task 6: `POST /internal/mcp/draft-campaign`

**Files:**
- Modify: `routes/internal_routes.py`
- Modify: `tests/test_internal_mcp_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_internal_mcp_routes.py`:

```python
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
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_internal_mcp_routes.py -k draft_campaign -v`
Expected: FAIL — 404 Not Found on both.

- [ ] **Step 3: Add the route**

In `routes/internal_routes.py`, directly below `mcp_schedule_email_blast`:

```python
class DraftCampaignRequest(_McpIdentityRequest):
    plan: str = Field(..., min_length=10)
    schedule: str = ""
    count: int = Field(5, ge=1, le=20)
    channels: List[str] = ["social_post", "email_blast"]


@router.post("/internal/mcp/draft-campaign", dependencies=[Depends(require_mcp_internal_key)])
def mcp_draft_campaign(req: DraftCampaignRequest, response: Response,
                       db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    user = _resolve_mcp_identity(db, req)

    if not user.perm("marketing.ai_generate"):
        raise HTTPException(status_code=403,
                            detail={"error": "plan_upgrade_required",
                                    "message": "Requires permission: marketing.ai_generate"})
    try:
        check_and_raise(db, user, "ai_generations_per_month")
    except HTTPException as exc:
        _raise_quota_error(exc, "ai_generations_quota_exceeded")

    try:
        result = generate_campaign(plan=req.plan, schedule=req.schedule,
                                   count=req.count, channels=req.channels)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail={"error": "ai_generation_failed", "message": str(e)})

    log_action(db, user, "AI_GENERATE", "Campaign",
              detail=f"Generated {len((result or {}).get('items', []))} item(s) via MCP")
    return result
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_internal_mcp_routes.py tests/test_internal_handoff.py tests/test_resolve_or_create_gootier_user.py -v`
Expected: all tests PASS (12 new + 6 new + 23 existing unchanged).

- [ ] **Step 5: Commit**

```bash
git add routes/internal_routes.py tests/test_internal_mcp_routes.py
git commit -m "feat: POST /internal/mcp/draft-campaign"
```

---

## Task 7: Whole-branch review, push, PR — STOP for user confirmation

**This task is a checkpoint, not code.**

- [ ] Run the FULL Gootier test suite once more from a clean state: `pytest -v`. Confirm no regressions anywhere in the repo, not just the new files.
- [ ] Run a **whole-branch review** (per `reviewing-whole-branches`) on `origin/main...HEAD` for this worktree's branch — not per-commit. Specifically check: does every one of the 5 new routes call `_resolve_mcp_identity` (no route skips identity resolution)? Does every quota/permission refusal use the structured `{"error", "message"}` shape consistently? Does `resolve_or_create_gootier_user` get called with an already-normalized email from every call site (no route passes raw `req.email`)?
- [ ] **STOP.** Push the branch and open a PR. Do NOT merge, and do NOT deploy Gootier, without the user's explicit go-ahead — Gootier is a live product with real customers, matching the standing discipline from the MidCanvas slice.

```bash
git push -u origin feat/gootier-mcp-facade
gh pr create --title "MCP facade: 5 internal routes for a Gootier-connected AI assistant" --body "..."
```

---

# PART B — Gootier-MCP-Server: the new repo

## Task 8: Scaffold the repo

**Files:**
- Create: `/Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server/` (new directory, new git history)
- Create: `requirements.txt`, `requirements-dev.txt`, `Procfile`, `pytest.ini`, `alembic.ini`
- Create: `app/__init__.py`, `app/config.py`, `app/db.py`, `app/models.py`
- Create: `alembic/env.py`, `alembic/versions/0001_baseline.py`
- Create: `tests/conftest.py`

This is a brand-new repo — no worktree needed (`using-git-worktrees` isolates work in an *existing* repo's history; there is none here yet).

- [ ] **Step 1: Create the directory and dependency files**

```bash
mkdir -p /Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server/app/{auth,billing,mcp_app,web/static,web/templates}
mkdir -p /Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server/tests
mkdir -p /Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server/alembic/versions
cd /Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server
git init
```

`requirements.txt`:
```
fastapi>=0.110
uvicorn[standard]>=0.29
sqlmodel>=0.0.16
jinja2>=3.1
alembic>=1.13
psycopg2-binary>=2.9
httpx>=0.27
python-multipart>=0.0.9
itsdangerous>=2.1
mcp>=2.2,<3
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8
pytest-asyncio>=0.23
```

`Procfile`:
```
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
```

- [ ] **Step 2: `app/__init__.py`, `app/config.py`**

`app/__init__.py`: empty file.

`app/config.py`:
```python
import os


def _env(key: str, default: str = "") -> str:
    """Environment value, falling back on an ABSENT or EMPTY variable.

    os.getenv(key, default) only falls back when the variable is absent: a
    variable set to "" returns "", silently defeating the default. The worst
    case is SESSION_SECRET="" -- itsdangerous signs and verifies with an
    empty key without complaining, so handoff tokens become forgeable and
    nothing anywhere says so.
    """
    return os.getenv(key) or default


class Settings:
    app_base_url: str
    database_url: str
    internal_key: str
    session_secret: str
    gootier_provider: str
    gootier_internal_url: str
    gootier_internal_key: str
    backoffice_url: str
    connected_app_slug: str
    run_migrations: bool

    def __init__(self) -> None:
        self.app_base_url = _env("APP_BASE_URL", "http://localhost:4901").rstrip("/")
        self.database_url = _env("DATABASE_URL", "sqlite:///./gootier_mcp.db")

        # Presented BY Backoffice when it calls our /internal/handoff.
        self.internal_key = _env("INTERNAL_KEY", "")

        # Signs the short-lived handoff token that /enter consumes.
        self.session_secret = _env("SESSION_SECRET", "dev-insecure-secret")

        # Gootier's own /internal/mcp/* routes -- an IMPLEMENTATION NAME, not
        # a mode word. Only the literal "http" selects the real client;
        # anything else, including "true" or "prod", gets the fake, which is
        # the failure mode where this app looks wired up and schedules
        # nothing real. Matches MidCanvasClient's own get_midcanvas_client()
        # convention exactly.
        self.gootier_provider = _env("GOOTIER_PROVIDER", "fake")
        self.gootier_internal_url = _env("GOOTIER_INTERNAL_URL", "").rstrip("/")
        self.gootier_internal_key = _env("GOOTIER_INTERNAL_KEY", "")

        # Where a visitor who is not signed in goes to sign in or sign up.
        # Identity belongs to Backoffice; this app has no signup form.
        self.backoffice_url = _env("BACKOFFICE_URL", "").rstrip("/")

        # MUST equal the `slug` on this app's row in Backoffice's
        # connected-app registry (app/connected/registry.py: slug="gootier-mcp").
        self.connected_app_slug = _env("CONNECTED_APP_SLUG", "gootier-mcp")

        self.run_migrations = _env("RUN_MIGRATIONS", "1") != "0"


settings = Settings()
```

- [ ] **Step 3: `app/db.py`** (verbatim copy of Jhome-MCP-Server's `app/db.py`, no changes needed — it's generic)

```python
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from app.config import settings


def normalized_url(url: str) -> str:
    """Rewrite the legacy `postgres://` scheme to `postgresql+psycopg2://`.

    Managed Postgres providers still hand out `postgres://`, which
    SQLAlchemy 2 rejects with NoSuchModuleError -- the dialect is registered
    as `postgresql`. PUBLIC, and every consumer of `settings.database_url`
    must go through it (engine below, `_apply_migrations()` in app/main.py,
    and both URL sites in alembic/env.py).
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    return url


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        return kwargs
    return {"pool_pre_ping": True}


_database_url = normalized_url(settings.database_url)
engine = create_engine(_database_url, **_engine_kwargs(_database_url))


def get_session():
    """The WEB path's session, committed on a clean response."""
    with Session(engine) as session:
        yield session
        session.commit()
```

- [ ] **Step 4: `app/models.py`** — `Customer` (no `token_wallet_id`, per the YAGNI deviation above) and `ConnectionKey`

```python
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(SQLModel, table=True):
    """One Gootier customer as this app knows them.

    Created by the Backoffice handoff, never by self-signup: there is no
    signup form here, identity belongs to Backoffice. No token_wallet_id --
    unlike Jhome-MCP-Server's Customer, nothing in this slice bills Jhome
    tokens (see the design spec's ensure-account section), so linking a
    wallet here would be speculative scope with nothing to exercise it.
    """

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str | None = Field(default=None)
    # One Jhome identity maps to exactly one customer row here. NULL is
    # exempt -- SQL treats NULLs as distinct under a unique index, so any
    # number of not-yet-bound customers coexist.
    jhome_sub: str | None = Field(default=None, index=True, unique=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False))


class ConnectionKey(SQLModel, table=True):
    """A bearer key one AI client uses to reach the MCP endpoint.

    The plaintext key is never stored. `display_prefix` is a NON-SECRET
    fragment so a customer holding several keys can tell which is which
    without the server being able to reconstruct any of them.
    """

    id: int | None = Field(default=None, primary_key=True)
    customer_id: int = Field(foreign_key="customer.id", index=True)
    key_hash: str = Field(index=True, unique=True)
    display_prefix: str
    label: str = Field(default="", sa_column_kwargs={"server_default": ""})
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False))
    last_used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
```

- [ ] **Step 5: Alembic setup**

`alembic.ini` (minimal, matching Jhome-MCP-Server's own):
```ini
[alembic]
script_location = alembic
sqlalchemy.url =

[loggers]
keys = root,sqlalchemy,alembic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handlers]
keys = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatters]
keys = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

`alembic/env.py`:
```python
from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from app.config import settings
from app.db import normalized_url
from app.models import Customer, ConnectionKey  # noqa: F401 -- registers metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(url=normalized_url(settings.database_url),
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine
    connectable = create_engine(normalized_url(settings.database_url))
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

`alembic/versions/0001_baseline.py`:
```python
"""baseline: customer, connectionkey

Revision ID: 0001
Revises:
Create Date: 2026-09-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("jhome_sub", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_customer_email", "customer", ["email"], unique=True)
    op.create_index("ix_customer_jhome_sub", "customer", ["jhome_sub"], unique=True)

    op.create_table(
        "connectionkey",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customer.id"), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("display_prefix", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_connectionkey_customer_id", "connectionkey", ["customer_id"])
    op.create_index("ix_connectionkey_key_hash", "connectionkey", ["key_hash"], unique=True)


def downgrade() -> None:
    op.drop_table("connectionkey")
    op.drop_table("customer")
```

- [ ] **Step 6: `tests/conftest.py`**

Set `RUN_MIGRATIONS`/`DATABASE_URL` at **module level, before any `app.*` import** — pytest always imports `conftest.py` before any test file in its directory, so this guarantees every test in the suite sees these values on `app.config.settings`'s one-time construction, rather than each test file needing its own fragile reload dance:

```python
import os

os.environ.setdefault("RUN_MIGRATIONS", "0")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    with Session(db_engine) as session:
        yield session
```

- [ ] **Step 7: Verify the scaffold imports cleanly**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server
pip install -r requirements-dev.txt
python3 -c "from app.models import Customer, ConnectionKey; from app.db import engine; print('ok')"
```
Expected: prints `ok`, no import errors.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "chore: scaffold Gootier-MCP-Server (config, db, models, alembic)"
```

---

## Task 9: `app/auth/keys.py` — connection-key mint/hash/verify

**Files:**
- Create: `app/auth/__init__.py` (empty)
- Create: `app/auth/keys.py`
- Create: `tests/test_auth_keys.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth_keys.py
from app.auth.keys import PREFIX, display_prefix_of, hash_key, mint_key, verify_key


def test_mint_key_returns_a_prefixed_plaintext_and_its_hash():
    plaintext, hashed = mint_key()
    assert plaintext.startswith(PREFIX)
    assert hashed == hash_key(plaintext)


def test_verify_key_accepts_the_matching_plaintext():
    plaintext, hashed = mint_key()
    assert verify_key(plaintext, hashed) is True


def test_verify_key_rejects_a_wrong_plaintext():
    _, hashed = mint_key()
    assert verify_key("wrong-key", hashed) is False


def test_verify_key_rejects_none_and_non_strings():
    _, hashed = mint_key()
    assert verify_key(None, hashed) is False


def test_display_prefix_reveals_only_a_short_fragment():
    plaintext, _ = mint_key()
    prefix = display_prefix_of(plaintext)
    assert prefix.startswith(PREFIX)
    assert len(prefix) < len(plaintext)
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_auth_keys.py -v`
Expected: `ModuleNotFoundError: No module named 'app.auth.keys'`.

- [ ] **Step 3: Implement**

`app/auth/__init__.py`: empty file.

`app/auth/keys.py`:
```python
"""Connection key format, hashing, and comparison.

Pure functions, no database. A distinct prefix from Jhome-MCP-Server's own
"jmcp_live_" so keys from the two products are visually distinguishable and
never accidentally cross-accepted.
"""
import hashlib
import secrets

PREFIX = "gtier_live_"
_SECRET_BYTES = 32
_DISPLAY_CHARS = 8


def mint_key() -> tuple[str, str]:
    """Return (plaintext, sha256_hex). The plaintext is never stored."""
    plaintext = PREFIX + secrets.token_urlsafe(_SECRET_BYTES)
    return plaintext, hash_key(plaintext)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8", "surrogatepass")).hexdigest()


def verify_key(plaintext: str | None, key_hash: str) -> bool:
    if not plaintext or not isinstance(plaintext, str):
        return False
    return secrets.compare_digest(hash_key(plaintext), key_hash)


def display_prefix_of(plaintext: str) -> str:
    return plaintext[: len(PREFIX) + _DISPLAY_CHARS]
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_auth_keys.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/auth/__init__.py app/auth/keys.py tests/test_auth_keys.py
git commit -m "feat: connection key mint/hash/verify"
```

---

## Task 10: `app/auth/handoff.py` — the short-lived `/enter` token

**Files:**
- Create: `app/auth/handoff.py`
- Create: `tests/test_auth_handoff.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth_handoff.py
import time

import pytest

from app.auth import handoff as handoff_module
from app.auth.handoff import mint_handoff_token, read_handoff_token


@pytest.fixture(autouse=True)
def _real_secret(monkeypatch):
    monkeypatch.setattr(handoff_module.settings, "session_secret", "test-secret-value")
    monkeypatch.setattr(handoff_module.settings, "app_base_url", "https://gootier-mcp.example.com")


def test_a_minted_token_reads_back_the_same_customer_id():
    token = mint_handoff_token(42)
    assert read_handoff_token(token) == 42


def test_a_tampered_token_is_rejected():
    token = mint_handoff_token(42)
    assert read_handoff_token(token[:-1] + ("x" if token[-1] != "x" else "y")) is None


def test_an_expired_token_is_rejected(monkeypatch):
    token = mint_handoff_token(42)
    monkeypatch.setattr(handoff_module, "MAX_AGE_SECONDS", 0)
    time.sleep(1.1)
    assert read_handoff_token(token) is None


def test_the_dev_secret_is_refused_on_a_non_local_deployment(monkeypatch):
    monkeypatch.setattr(handoff_module.settings, "session_secret", handoff_module.DEV_SECRET)
    monkeypatch.setattr(handoff_module.settings, "app_base_url", "https://gootier-mcp.example.com")
    with pytest.raises(RuntimeError):
        mint_handoff_token(1)
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_auth_handoff.py -v`
Expected: `ModuleNotFoundError: No module named 'app.auth.handoff'`.

- [ ] **Step 3: Implement**

```python
# app/auth/handoff.py
"""The short-lived token that carries a customer from Backoffice to /enter.

Signed and timestamped rather than stored. NOT single-use -- freely
replayable within its 120-second window, matching Jhome-MCP-Server's own
handoff.py exactly. A distinct salt from that app's "jhome-mcp-handoff" so a
token minted for one product can never be replayed against the other.
"""
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_SALT = "gootier-mcp-handoff"
MAX_AGE_SECONDS = 120

DEV_SECRET = "dev-insecure-secret"


def _is_local(base_url: str) -> bool:
    return base_url.startswith(("http://localhost", "http://127.0.0.1"))


def _serializer() -> URLSafeTimedSerializer:
    secret = settings.session_secret
    if secret == DEV_SECRET and not _is_local(settings.app_base_url):
        raise RuntimeError(
            "SESSION_SECRET is still the development default. Handoff tokens "
            "signed with it are forgeable. Set SESSION_SECRET on the service.")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def mint_handoff_token(customer_id: int) -> str:
    return _serializer().dumps({"customer_id": customer_id})


def read_handoff_token(token: str) -> int | None:
    try:
        data = _serializer().loads(token, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    customer_id = data.get("customer_id")
    return customer_id if isinstance(customer_id, int) else None
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_auth_handoff.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/auth/handoff.py tests/test_auth_handoff.py
git commit -m "feat: short-lived handoff token for /enter"
```

---

## Task 11: `app/internal_api.py` — the Backoffice → Gootier-MCP-Server handoff

**Files:**
- Create: `app/internal_api.py`
- Create: `tests/test_internal_api.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_internal_api.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select
import pytest

from app import internal_api
from app.internal_api import router
from app.db import get_session
from app.models import Customer


@pytest.fixture
def client(db_engine, monkeypatch):
    monkeypatch.setattr(internal_api.settings, "internal_key", "test-key")
    monkeypatch.setattr(internal_api.settings, "app_base_url", "https://gootier-mcp.example.com")

    app = FastAPI()
    app.include_router(router)

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as c:
        yield c, db_engine


def test_without_the_key_is_refused(client):
    c, _ = client
    resp = c.post("/internal/handoff", json={"email": "a@example.com"})
    assert resp.status_code == 401


def test_a_brand_new_customer_is_created_and_a_consume_url_returned(client):
    c, engine = client
    resp = c.post("/internal/handoff",
                  json={"email": "new@example.com", "name": "New Person",
                       "jhome_sub": "sub-1", "email_verified": True},
                  headers={"X-Internal-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["consume_url"].startswith("https://gootier-mcp.example.com/enter?t=")

    with Session(engine) as s:
        customer = s.exec(select(Customer).where(Customer.email == "new@example.com")).first()
        assert customer is not None
        assert customer.jhome_sub == "sub-1"


def test_an_existing_customer_reached_by_email_cannot_bind_without_verification(client):
    c, engine = client
    with Session(engine) as s:
        s.add(Customer(email="jane@example.com", name="Jane"))
        s.commit()

    resp = c.post("/internal/handoff",
                  json={"email": "jane@example.com", "jhome_sub": "sub-2",
                       "email_verified": False},
                  headers={"X-Internal-Key": "test-key"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "unverified_account"


def test_a_subject_already_bound_elsewhere_is_refused(client):
    c, engine = client
    with Session(engine) as s:
        s.add(Customer(email="jane@example.com", jhome_sub="sub-taken"))
        s.commit()

    resp = c.post("/internal/handoff",
                  json={"email": "someone-else@example.com", "jhome_sub": "sub-taken",
                       "email_verified": True},
                  headers={"X-Internal-Key": "test-key"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "linked_elsewhere"
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_internal_api.py -v`
Expected: `ModuleNotFoundError: No module named 'app.internal_api'`.

- [ ] **Step 3: Implement** (mirrors Jhome-MCP-Server's current `app/internal_api.py` exactly, renamed to `Customer`/this app's own settings)

```python
# app/internal_api.py
"""The Backoffice handoff. Guarded by a shared internal key."""
import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth.handoff import mint_handoff_token
from app.config import settings
from app.db import get_session
from app.models import Customer

log = logging.getLogger("gootier_mcp")
router = APIRouter()


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    if (not settings.internal_key
            or not x_internal_key.isascii()
            or not secrets.compare_digest(x_internal_key, settings.internal_key)):
        raise HTTPException(status_code=401, detail="bad internal key")


class HandoffRequest(BaseModel):
    email: str
    name: str | None = None
    jhome_sub: str | None = None
    domains: list[str] = []
    email_verified: bool = False
    entitlements: list[str] = []
    landing: str = ""

    @field_validator("jhome_sub", mode="before")
    @classmethod
    def _blank_subject_is_absent(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class HandoffResponse(BaseModel):
    consume_url: str


def _linked_elsewhere() -> HTTPException:
    return HTTPException(status_code=403, detail={
        "error": "linked_elsewhere",
        "message": "That account is connected to a different sign-in.",
    })


def _may_hand_over(row: Customer, presented_sub: str | None) -> bool:
    return row.jhome_sub is None or row.jhome_sub == presented_sub


def _admit(session: Session, req: "HandoffRequest", email: str,
          customer: Customer, by_subject: bool) -> Customer:
    if by_subject:
        if customer.email != email:
            clash = session.exec(
                select(Customer).where(Customer.email == email,
                                       Customer.id != customer.id)).first()
            if clash is not None:
                raise HTTPException(status_code=409, detail={
                    "error": "email_belongs_to_another_account",
                    "message": "That email address is already in use here.",
                })
            customer.email = email
            session.add(customer)
            session.commit()
        return customer

    if not _may_hand_over(customer, req.jhome_sub):
        log.warning("refusing handoff: email %s is customer %s bound to "
                   "jhome_sub %s, but the request carried %s",
                   email, customer.id, customer.jhome_sub, req.jhome_sub)
        raise _linked_elsewhere()

    if req.jhome_sub and customer.jhome_sub is None:
        if not req.email_verified:
            raise HTTPException(status_code=403, detail={
                "error": "unverified_account",
                "message": "This email address has not been verified.",
            })
        customer.jhome_sub = req.jhome_sub
        session.add(customer)
        session.commit()
    return customer


@router.post("/internal/handoff", response_model=HandoffResponse,
            dependencies=[Depends(require_internal_key)])
def handoff(req: HandoffRequest, session: Session = Depends(get_session)):
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail=f"invalid email '{req.email}'")

    customer = None
    by_subject = False
    if req.jhome_sub:
        customer = session.exec(
            select(Customer).where(Customer.jhome_sub == req.jhome_sub)).first()
        by_subject = customer is not None
    if customer is None:
        customer = session.exec(
            select(Customer).where(Customer.email == email)).first()

    if customer is None:
        customer = _create_customer(session, req, email)
    else:
        customer = _admit(session, req, email, customer, by_subject)

    token = mint_handoff_token(customer.id)
    return HandoffResponse(consume_url=f"{settings.app_base_url}/enter?t={token}")


def _create_customer(session: Session, req: "HandoffRequest", email: str) -> Customer:
    customer = Customer(email=email, name=req.name, jhome_sub=req.jhome_sub)
    session.add(customer)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = None
        by_subject = False
        if req.jhome_sub:
            winner = session.exec(
                select(Customer).where(Customer.jhome_sub == req.jhome_sub)).first()
            by_subject = winner is not None
        if winner is None:
            winner = session.exec(
                select(Customer).where(Customer.email == email)).first()
        if winner is None:
            raise
        return _admit(session, req, email, winner, by_subject)
    session.refresh(customer)
    log.info("created customer %s for jhome_sub %s", customer.id, req.jhome_sub)
    return customer
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_internal_api.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/internal_api.py tests/test_internal_api.py
git commit -m "feat: Backoffice-facing POST /internal/handoff"
```

---

## Task 12: `app/auth/context.py` + `app/auth/middleware.py` — connection-key authentication

**Files:**
- Create: `app/auth/context.py`
- Create: `app/auth/middleware.py`
- Create: `tests/test_auth_middleware.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth_middleware.py
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.auth.context import require_customer_id
from app.auth.keys import mint_key
from app.auth.middleware import connection_key_middleware
from app.models import Customer, ConnectionKey


def _app(engine):
    app = FastAPI()
    app.middleware("http")(connection_key_middleware)

    @app.get("/mcp/whoami")
    def whoami():
        return {"customer_id": require_customer_id()}

    @app.get("/unguarded")
    def unguarded():
        return {"ok": True}

    import app.db as db_module
    db_module.engine = engine
    return app


def test_a_request_with_no_authorization_header_is_401(db_engine):
    client = TestClient(_app(db_engine))
    resp = client.get("/mcp/whoami")
    assert resp.status_code == 401


def test_a_valid_key_resolves_the_customer(db_engine):
    with Session(db_engine) as s:
        customer = Customer(email="jane@example.com")
        s.add(customer)
        s.commit()
        s.refresh(customer)
        plaintext, hashed = mint_key()
        s.add(ConnectionKey(customer_id=customer.id, key_hash=hashed, display_prefix="gtier_live_x"))
        s.commit()
        customer_id = customer.id

    client = TestClient(_app(db_engine))
    resp = client.get("/mcp/whoami", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json()["customer_id"] == customer_id


def test_a_revoked_key_is_401(db_engine):
    with Session(db_engine) as s:
        customer = Customer(email="jane@example.com")
        s.add(customer)
        s.commit()
        s.refresh(customer)
        plaintext, hashed = mint_key()
        s.add(ConnectionKey(customer_id=customer.id, key_hash=hashed, display_prefix="gtier_live_x",
                            revoked_at=datetime.now(timezone.utc)))
        s.commit()

    client = TestClient(_app(db_engine))
    resp = client.get("/mcp/whoami", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 401


def test_an_unguarded_path_is_never_touched(db_engine):
    client = TestClient(_app(db_engine))
    resp = client.get("/unguarded")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_auth_middleware.py -v`
Expected: `ModuleNotFoundError: No module named 'app.auth.context'`.

- [ ] **Step 3: Implement**

`app/auth/context.py`:
```python
"""Per-request customer identity, set by connection_key_middleware."""
from contextvars import ContextVar

_customer_id: ContextVar[int | None] = ContextVar("customer_id", default=None)


def set_customer_id(customer_id: int) -> None:
    _customer_id.set(customer_id)


def require_customer_id() -> int:
    value = _customer_id.get()
    if value is None:
        raise RuntimeError("no authenticated customer id in this request context")
    return value
```

`app/auth/middleware.py`:
```python
"""Validates the Authorization: Bearer <connection key> header on /mcp/*."""
import logging
from datetime import timezone

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.auth.context import set_customer_id
from app.auth.keys import hash_key, verify_key
from app.models import Customer, ConnectionKey, utcnow

log = logging.getLogger("gootier_mcp")

GUARDED_PREFIX = "/mcp"


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "Not authenticated"},
                        headers={"WWW-Authenticate": "Bearer"})


async def connection_key_middleware(request: Request, call_next):
    if not request.url.path.startswith(GUARDED_PREFIX):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return _unauthorized()
    presented = header[7:].strip()
    if not presented:
        return _unauthorized()

    import app.db as db_module
    with Session(db_module.engine) as session:
        row = session.exec(
            select(ConnectionKey).where(ConnectionKey.key_hash == hash_key(presented))
        ).first()
        if row is None or row.revoked_at is not None:
            return _unauthorized()
        if not verify_key(presented, row.key_hash):
            return _unauthorized()

        customer = session.get(Customer, row.customer_id)
        if customer is None:
            log.warning("connection key %s has no customer row", row.id)
            return _unauthorized()

        now = utcnow()
        last = row.last_used_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or (now - last).total_seconds() > 60:
            row.last_used_at = now
            session.add(row)
            session.commit()
        customer_id = customer.id

    set_customer_id(customer_id)
    return await call_next(request)
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_auth_middleware.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/auth/context.py app/auth/middleware.py tests/test_auth_middleware.py
git commit -m "feat: connection-key authentication middleware"
```

---

## Task 13: `app/billing/gootier_client.py` — the client into Gootier's new routes

**Files:**
- Create: `app/billing/__init__.py` (empty)
- Create: `app/billing/gootier_client.py`
- Create: `tests/test_gootier_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gootier_client.py
import httpx
import pytest

from app.billing.gootier_client import (FakeGootierClient, GootierError,
                                        HTTPGootierClient, get_gootier_client)
from app.config import settings


def _handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/internal/mcp/ensure-account":
        return httpx.Response(200, json={"user_id": 1, "tier": "trial"})
    if req.url.path == "/internal/mcp/social-connections":
        return httpx.Response(200, json={"connections": []})
    if req.url.path == "/internal/mcp/schedule-post":
        return httpx.Response(200, json={"id": 1, "status": "published"})
    if req.url.path == "/internal/mcp/schedule-email-blast":
        return httpx.Response(200, json={"id": 1, "status": "sent"})
    if req.url.path == "/internal/mcp/draft-campaign":
        return httpx.Response(200, json={"items": []})
    return httpx.Response(404)


@pytest.fixture
def client():
    return HTTPGootierClient(base_url="https://gootier.test", api_key="k",
                             transport=httpx.MockTransport(_handler))


def test_the_timeout_is_generous_enough_for_a_large_blast_or_sequential_publish():
    client = HTTPGootierClient(base_url="https://gootier.test", api_key="k")
    assert client._client.timeout.read >= 900


def test_ensure_account_returns_the_parsed_body(client):
    result = client.ensure_account(jhome_sub="s", email="e@e.com", email_verified=True)
    assert result == {"user_id": 1, "tier": "trial"}


def test_a_refusal_raises_a_typed_error_the_tool_layer_can_catch():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "plan_upgrade_required",
                                        "message": "Requires permission: marketing.email_blast"})
    client = HTTPGootierClient(base_url="https://gootier.test", api_key="k",
                               transport=httpx.MockTransport(handler))
    with pytest.raises(GootierError) as exc:
        client.schedule_email_blast(jhome_sub="s", email="e@e.com", email_verified=True,
                                    subject="hi", body_html="<p>hi</p>", recipients=["a@x.com"])
    assert exc.value.status_code == 403
    assert exc.value.detail_dict()["error"] == "plan_upgrade_required"


def test_a_connection_failure_raises_gootiererror_not_a_raw_httpx_exception():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    client = HTTPGootierClient(base_url="https://gootier.test", api_key="k",
                               transport=httpx.MockTransport(handler))
    with pytest.raises(GootierError) as exc:
        client.ensure_account(jhome_sub="s", email="e@e.com", email_verified=True)
    assert exc.value.status_code is None


def test_the_fake_client_discriminates_on_content():
    fake = FakeGootierClient()
    r1 = fake.schedule_post(jhome_sub="s", email="e@e.com", email_verified=True,
                            content="first post", connection_ids=[1])
    r2 = fake.schedule_post(jhome_sub="s", email="e@e.com", email_verified=True,
                            content="second post", connection_ids=[1])
    assert r1["id"] != r2["id"]


def test_get_gootier_client_selects_real_only_with_provider_key_and_url(monkeypatch):
    monkeypatch.setattr(settings, "gootier_provider", "http")
    monkeypatch.setattr(settings, "gootier_internal_key", "k")
    monkeypatch.setattr(settings, "gootier_internal_url", "https://gootier.test")
    assert isinstance(get_gootier_client(), HTTPGootierClient)

    monkeypatch.setattr(settings, "gootier_provider", "fake")
    assert isinstance(get_gootier_client(), FakeGootierClient)

    monkeypatch.setattr(settings, "gootier_provider", "http")
    monkeypatch.setattr(settings, "gootier_internal_key", "")
    assert isinstance(get_gootier_client(), FakeGootierClient)
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_gootier_client.py -v`
Expected: `ModuleNotFoundError: No module named 'app.billing.gootier_client'`.

- [ ] **Step 3: Implement**

```python
# app/billing/gootier_client.py
"""A client for the 5 Gootier /internal/mcp/* endpoints.

Real vs fake, selected by an IMPLEMENTATION NAME -- matching
MidCanvasClient's own get_midcanvas_client() convention exactly.
"""
import hashlib
from datetime import datetime
from typing import Protocol

import httpx

from app.config import settings


class GootierError(Exception):
    """Raised on any non-2xx response, AND on a connection-level failure --
    always this type, never a raw httpx exception. `status_code` is None
    for a connection-level failure, an int for anything the server actually
    answered with."""

    def __init__(self, status_code: int | None, detail):
        super().__init__(f"Gootier returned {status_code}"
                         if status_code is not None else f"Gootier unreachable: {detail}")
        self.status_code = status_code
        self.detail = detail

    def detail_dict(self) -> dict:
        return self.detail if isinstance(self.detail, dict) else {}


class GootierClient(Protocol):
    def ensure_account(self, *, jhome_sub: str | None, email: str,
                       email_verified: bool) -> dict: ...
    def list_social_connections(self, *, jhome_sub: str | None, email: str,
                                email_verified: bool) -> dict: ...
    def schedule_post(self, *, jhome_sub: str | None, email: str, email_verified: bool,
                      content: str, connection_ids: list[int],
                      image_url: str | None = None, video_url: str | None = None,
                      link_url: str | None = None,
                      scheduled_at: str | None = None) -> dict: ...
    def schedule_email_blast(self, *, jhome_sub: str | None, email: str, email_verified: bool,
                             subject: str, body_html: str, recipients: list[str],
                             scheduled_at: str | None = None) -> dict: ...
    def draft_campaign(self, *, jhome_sub: str | None, email: str, email_verified: bool,
                       plan: str, schedule: str = "", count: int = 5,
                       channels: list[str] | None = None) -> dict: ...


class HTTPGootierClient:
    # Deliberately larger than MidCanvasClient's 330s: schedule-post
    # publishes to each connection SEQUENTIALLY, up to 120s per platform
    # (Gootier's services/social_publish.py), and schedule-email-blast
    # sends synchronously to up to 25,000 recipients on a Gold-tier
    # account. A timeout shorter than either real worst case turns a
    # successful, already-committed action into a lost response -- the
    # exact incident the MidCanvas walk paid for once already. Even 900s
    # is not a hard guarantee for an extreme blast; making that path
    # fully safe means moving Gootier's own blast-send to a background
    # job, which is out of scope for this client.
    TIMEOUT = 900.0

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or settings.gootier_internal_url).rstrip("/")
        self.api_key = api_key or settings.gootier_internal_key
        self._client = httpx.Client(transport=transport, timeout=self.TIMEOUT)

    def _post(self, path: str, json: dict) -> dict:
        try:
            resp = self._client.post(
                f"{self.base_url}{path}",
                headers={"X-Internal-Key": self.api_key},
                json=json)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise GootierError(None, str(exc)) from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise GootierError(resp.status_code, detail)
        return resp.json()

    def ensure_account(self, *, jhome_sub, email, email_verified) -> dict:
        return self._post("/internal/mcp/ensure-account",
                          {"jhome_sub": jhome_sub, "email": email,
                           "email_verified": email_verified})

    def list_social_connections(self, *, jhome_sub, email, email_verified) -> dict:
        return self._post("/internal/mcp/social-connections",
                          {"jhome_sub": jhome_sub, "email": email,
                           "email_verified": email_verified})

    def schedule_post(self, *, jhome_sub, email, email_verified, content, connection_ids,
                      image_url=None, video_url=None, link_url=None, scheduled_at=None) -> dict:
        return self._post("/internal/mcp/schedule-post", {
            "jhome_sub": jhome_sub, "email": email, "email_verified": email_verified,
            "content": content, "connection_ids": connection_ids,
            "image_url": image_url, "video_url": video_url, "link_url": link_url,
            "scheduled_at": scheduled_at})

    def schedule_email_blast(self, *, jhome_sub, email, email_verified, subject, body_html,
                             recipients, scheduled_at=None) -> dict:
        return self._post("/internal/mcp/schedule-email-blast", {
            "jhome_sub": jhome_sub, "email": email, "email_verified": email_verified,
            "subject": subject, "body_html": body_html, "recipients": recipients,
            "scheduled_at": scheduled_at})

    def draft_campaign(self, *, jhome_sub, email, email_verified, plan, schedule="",
                       count=5, channels=None) -> dict:
        return self._post("/internal/mcp/draft-campaign", {
            "jhome_sub": jhome_sub, "email": email, "email_verified": email_verified,
            "plan": plan, "schedule": schedule, "count": count,
            "channels": channels or ["social_post", "email_blast"]})


class FakeGootierClient:
    """Deterministic, no network, and DISCRIMINATING -- different content
    must produce different fake ids, matching MidCanvasClient's own fake."""

    def ensure_account(self, *, jhome_sub, email, email_verified) -> dict:
        return {"user_id": 1, "tier": "trial"}

    def list_social_connections(self, *, jhome_sub, email, email_verified) -> dict:
        return {"connections": [{"id": 1, "platform": "facebook", "display_name": "Fake Page"}]}

    def _digest(self, *parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]

    def schedule_post(self, *, jhome_sub, email, email_verified, content, connection_ids,
                      image_url=None, video_url=None, link_url=None, scheduled_at=None) -> dict:
        return {"id": int(self._digest(content), 16) % 100000, "status": "published"}

    def schedule_email_blast(self, *, jhome_sub, email, email_verified, subject, body_html,
                             recipients, scheduled_at=None) -> dict:
        return {"id": int(self._digest(subject, body_html), 16) % 100000, "status": "sent"}

    def draft_campaign(self, *, jhome_sub, email, email_verified, plan, schedule="",
                       count=5, channels=None) -> dict:
        return {"items": [{"kind": "social_post", "content": f"Fake draft for: {plan[:40]}",
                          "scheduled_at": None} for _ in range(count)]}


_fake_client = FakeGootierClient()
_http_client: HTTPGootierClient | None = None


def get_gootier_client() -> GootierClient:
    global _http_client
    if (settings.gootier_provider == "http"
            and settings.gootier_internal_key
            and settings.gootier_internal_url):
        if _http_client is None:
            _http_client = HTTPGootierClient()
        return _http_client
    return _fake_client
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_gootier_client.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/billing/__init__.py app/billing/gootier_client.py tests/test_gootier_client.py
git commit -m "feat: GootierClient (real + fake) for the 5 internal routes"
```

---

## Task 14: `app/mcp_app/tools_gootier.py` — the 4 tool wrapper functions

**Files:**
- Create: `app/mcp_app/__init__.py` (empty)
- Create: `app/mcp_app/tools_gootier.py`
- Create: `tests/test_tools_gootier.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools_gootier.py
from app.billing.gootier_client import FakeGootierClient, GootierError
from app.mcp_app import tools_gootier
from app.models import Customer


def _customer():
    c = Customer(email="jane@example.com", jhome_sub="sub-1")
    c.id = 1
    return c


def test_draft_campaign_renders_readable_text_not_raw_json():
    text = tools_gootier.draft_campaign(_customer(), "A new coffee shop", "weekly", 3,
                                        ["social_post"], gootier=FakeGootierClient())
    assert "coffee shop" in text.lower()


def test_list_social_connections_renders_platform_and_id():
    text = tools_gootier.list_social_connections(_customer(), gootier=FakeGootierClient())
    assert "facebook" in text.lower()


def test_schedule_post_renders_a_confirmation_with_status():
    text = tools_gootier.schedule_post(_customer(), "hello world", [1],
                                       gootier=FakeGootierClient())
    assert "published" in text.lower()


def test_schedule_email_blast_renders_a_confirmation_with_status():
    text = tools_gootier.schedule_email_blast(_customer(), "Hi", "<p>hi</p>", ["a@x.com"],
                                              gootier=FakeGootierClient())
    assert "sent" in text.lower()


def test_a_plan_upgrade_required_refusal_gets_its_own_sentence():
    class Refusing:
        def schedule_email_blast(self, **kwargs):
            raise GootierError(403, {"error": "plan_upgrade_required",
                                     "message": "Requires permission: marketing.email_blast"})
    text = tools_gootier.schedule_email_blast(_customer(), "Hi", "<p>hi</p>", ["a@x.com"],
                                              gootier=Refusing())
    assert "upgrade" in text.lower()


def test_a_quota_exceeded_refusal_gets_its_own_sentence():
    class Refusing:
        def schedule_post(self, **kwargs):
            raise GootierError(403, {"error": "posts_quota_exceeded",
                                     "message": "You've hit your bronze plan's posts limit (100/100)."})
    text = tools_gootier.schedule_post(_customer(), "hello", [1], gootier=Refusing())
    assert "limit" in text.lower() or "quota" in text.lower()


def test_an_unrecognized_refusal_falls_back_to_the_servers_own_message():
    class Refusing:
        def draft_campaign(self, **kwargs):
            raise GootierError(502, {"error": "ai_generation_failed", "message": "model overloaded"})
    text = tools_gootier.draft_campaign(_customer(), "plan text here", "", 5, None,
                                        gootier=Refusing())
    assert "model overloaded" in text
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_tools_gootier.py -v`
Expected: `ModuleNotFoundError: No module named 'app.mcp_app.tools_gootier'`.

- [ ] **Step 3: Implement**

```python
# app/mcp_app/tools_gootier.py
"""The 4 Gootier tools -- a facade over the 5 Gootier /internal/mcp/*
routes (ensure-account is called implicitly by identity resolution
elsewhere, never directly by a tool).

Mirrors tools_media.py's shape: each `_*` inner function does the work and
raises GootierError; the outer function is the thin wrapper that catches
it and renders a sentence.
"""
import logging

from app.billing.gootier_client import GootierClient, GootierError, get_gootier_client
from app.models import Customer

logger = logging.getLogger("gootier_mcp")

# One sentence per structured error code, not one generic catch-all --
# matching tools_media.py's principle of never relaying the backend's own
# internal error text to a customer.
_SENTENCES = {
    "plan_upgrade_required": "Your current plan doesn't include this. {message}",
    "posts_quota_exceeded": "{message}",
    "blasts_quota_exceeded": "{message}",
    "recipient_cap_exceeded": "{message}",
    "ai_generations_quota_exceeded": "{message}",
    "invalid_connections": "One or more of those connections isn't valid for this account.",
}
_FALLBACK = "Could not complete that right now. Try again shortly."


def _render_refusal(exc: GootierError) -> str:
    detail = exc.detail_dict()
    code = detail.get("error")
    message = detail.get("message", "")
    template = _SENTENCES.get(code)
    if template:
        return template.format(message=message)
    if message:
        # An error code this file has no specific sentence for yet -- the
        # server's own `message` is still customer-safe (Gootier's routes
        # only ever put a human-readable string there, never a stack
        # trace), so it's a better fallback than a generic sentence.
        return message
    logger.exception("unexpected GootierError with no message")
    return _FALLBACK


def draft_campaign(customer: Customer, plan: str, schedule: str, count: int,
                   channels: list[str] | None, gootier: GootierClient | None = None) -> str:
    client = gootier or get_gootier_client()
    try:
        result = client.draft_campaign(
            jhome_sub=customer.jhome_sub, email=customer.email, email_verified=True,
            plan=plan, schedule=schedule, count=count, channels=channels)
    except GootierError as exc:
        return _render_refusal(exc)
    items = result.get("items", [])
    if not items:
        return "No campaign items were generated. Try a more specific plan description."
    lines = [f"Drafted {len(items)} campaign item(s):"]
    for i, item in enumerate(items, 1):
        kind = item.get("kind", "item")
        content = item.get("content") or item.get("subject") or ""
        lines.append(f"{i}. [{kind}] {content}")
    return "\n".join(lines)


def list_social_connections(customer: Customer, gootier: GootierClient | None = None) -> str:
    client = gootier or get_gootier_client()
    try:
        result = client.list_social_connections(
            jhome_sub=customer.jhome_sub, email=customer.email, email_verified=True)
    except GootierError as exc:
        return _render_refusal(exc)
    connections = result.get("connections", [])
    if not connections:
        return "No social channels are connected yet."
    lines = ["Connected channels:"]
    for c in connections:
        lines.append(f"- id {c['id']}: {c['platform']} ({c['display_name']})")
    return "\n".join(lines)


def schedule_post(customer: Customer, content: str, connection_ids: list[int],
                  image_url: str | None = None, video_url: str | None = None,
                  link_url: str | None = None, scheduled_at: str | None = None,
                  gootier: GootierClient | None = None) -> str:
    client = gootier or get_gootier_client()
    try:
        result = client.schedule_post(
            jhome_sub=customer.jhome_sub, email=customer.email, email_verified=True,
            content=content, connection_ids=connection_ids, image_url=image_url,
            video_url=video_url, link_url=link_url, scheduled_at=scheduled_at)
    except GootierError as exc:
        return _render_refusal(exc)
    return f"Post {result['id']} is {result['status']}."


def schedule_email_blast(customer: Customer, subject: str, body_html: str,
                         recipients: list[str], scheduled_at: str | None = None,
                         gootier: GootierClient | None = None) -> str:
    client = gootier or get_gootier_client()
    try:
        result = client.schedule_email_blast(
            jhome_sub=customer.jhome_sub, email=customer.email, email_verified=True,
            subject=subject, body_html=body_html, recipients=recipients,
            scheduled_at=scheduled_at)
    except GootierError as exc:
        return _render_refusal(exc)
    return f"Email blast {result['id']} is {result['status']}."
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_tools_gootier.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_app/__init__.py app/mcp_app/tools_gootier.py tests/test_tools_gootier.py
git commit -m "feat: the 4 Gootier tool wrapper functions"
```

---

## Task 15: `app/mcp_app/server.py` — tool registration

**Files:**
- Create: `app/mcp_app/server.py`
- Create: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_server.py
from sqlmodel import Session

from app.auth.context import set_customer_id
from app.mcp_app.server import build_mcp_server, current_customer, open_session
from app.models import Customer


def test_build_mcp_server_registers_all_4_tools():
    server = build_mcp_server()
    import asyncio
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"gootier_draft_campaign", "gootier_list_social_connections",
                     "gootier_schedule_post", "gootier_schedule_email_blast"}


def test_current_customer_resolves_from_the_request_context(db_engine):
    with Session(db_engine) as s:
        c = Customer(email="jane@example.com")
        s.add(c)
        s.commit()
        s.refresh(c)
        customer_id = c.id

    import app.db as db_module
    db_module.engine = db_engine
    set_customer_id(customer_id)
    with open_session() as session:
        customer = current_customer(session)
        assert customer.email == "jane@example.com"
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_mcp_server.py -v`
Expected: `ModuleNotFoundError: No module named 'app.mcp_app.server'`.

- [ ] **Step 3: Implement**

```python
# app/mcp_app/server.py
"""Builds a fresh MCPServer per app instance and registers the 4 tools.

A fresh server per app, never a module singleton: StreamableHTTPSessionManager.run()
can only be entered once per instance, matching Jhome-MCP-Server's own
build_mcp_server() precedent.
"""
import contextlib

from mcp.server.fastmcp import FastMCP as MCPServer
from sqlmodel import Session

from app.auth.context import require_customer_id
from app.models import Customer


def current_customer(session: Session) -> Customer:
    customer = session.get(Customer, require_customer_id())
    if customer is None:                       # pragma: no cover - guarded upstream
        raise RuntimeError("authenticated customer id has no row")
    return customer


@contextlib.contextmanager
def open_session():
    import app.db as db_module
    with Session(db_module.engine) as session:
        yield session
        session.commit()


def register_tools(server: MCPServer) -> None:
    @server.tool()
    def gootier_draft_campaign(plan: str, schedule: str = "", count: int = 5,
                               channels: str = "") -> str:
        """Draft an AI marketing campaign from a plan description.

        Preview only -- nothing is scheduled or persisted. Use it when the
        customer wants ideas for posts or email content before committing
        to anything.
        """
        from app.mcp_app import tools_gootier
        channel_list = [c.strip() for c in channels.split(",") if c.strip()] or None
        with open_session() as session:
            return tools_gootier.draft_campaign(
                current_customer(session), plan, schedule, count, channel_list)

    @server.tool()
    def gootier_list_social_connections() -> str:
        """List this customer's connected social channels.

        Use it before scheduling a post, to find the connection ids to
        target.
        """
        from app.mcp_app import tools_gootier
        with open_session() as session:
            return tools_gootier.list_social_connections(current_customer(session))

    @server.tool()
    def gootier_schedule_post(content: str, connection_ids: str, image_url: str = "",
                              video_url: str = "", link_url: str = "",
                              scheduled_at: str = "") -> str:
        """Schedule (or publish immediately) a social post.

        `connection_ids` is a comma-separated list of ids from
        gootier_list_social_connections. Leave `scheduled_at` empty to
        publish right away.
        """
        from app.mcp_app import tools_gootier
        ids = [int(x.strip()) for x in connection_ids.split(",") if x.strip()]
        with open_session() as session:
            return tools_gootier.schedule_post(
                current_customer(session), content, ids,
                image_url or None, video_url or None, link_url or None,
                scheduled_at or None)

    @server.tool()
    def gootier_schedule_email_blast(subject: str, body_html: str, recipients: str,
                                     scheduled_at: str = "") -> str:
        """Schedule (or send immediately) an email blast.

        `recipients` is a comma-separated list of email addresses. Leave
        `scheduled_at` empty to send right away.
        """
        from app.mcp_app import tools_gootier
        recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
        with open_session() as session:
            return tools_gootier.schedule_email_blast(
                current_customer(session), subject, body_html, recipient_list,
                scheduled_at or None)


def build_mcp_server() -> MCPServer:
    server = MCPServer(name="gootier", title="Gootier MCP Server",
                       instructions="Draft AI campaigns and schedule social posts "
                                   "and email blasts for this Gootier account.")
    register_tools(server)
    return server
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `pytest tests/test_mcp_server.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_app/server.py tests/test_mcp_server.py
git commit -m "feat: register the 4 Gootier MCP tools"
```

---

## Task 16: `app/main.py` — the FastAPI app factory and MCP mount

**Files:**
- Create: `app/main.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: Write the failing test**

`app/main.py` builds `app = create_app()` eagerly at import time (matching Jhome-MCP-Server's own `app/main.py` exactly), so this test relies on `tests/conftest.py` (Task 8, Step 6) having already set `RUN_MIGRATIONS=0` and `DATABASE_URL=sqlite://` in the environment before `app.config.settings` is constructed for the first time in the test process — no per-test reload needed:

```python
# tests/test_main.py
from fastapi.testclient import TestClient

from app.main import app


def test_healthz_responds_ok():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `pytest tests/test_main.py -v`
Expected: `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Implement**

```python
# app/main.py
import contextlib
import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.sessions import SessionMiddleware

from app.auth.middleware import connection_key_middleware
from app.config import settings
from app.internal_api import router as internal_router
from app.mcp_app.server import build_mcp_server
from app.web.routes import html_error_handler, router as web_router


def configure_app_logging() -> None:
    """Root logger defaults to WARNING; without this, INFO logs from this
    app's own loggers are silently dropped in production."""
    logging.getLogger("gootier_mcp").setLevel(logging.INFO)


def _is_local(base_url: str) -> bool:
    return base_url.startswith(("http://localhost", "http://127.0.0.1"))


def _mcp_transport_security() -> TransportSecuritySettings:
    """REQUIRED. Without it, the mcp library's default DNS-rebinding
    protection only allows 127.0.0.1/localhost, so every real deployed
    request gets a 421 Invalid Host header."""
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    deployed_host = urlsplit(settings.app_base_url).netloc
    if deployed_host and not _is_local(settings.app_base_url):
        allowed_hosts += [deployed_host, f"{deployed_host}:*"]
        allowed_origins.append(settings.app_base_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _apply_migrations() -> None:
    if not settings.run_migrations:
        return
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    command.upgrade(cfg, "head")


def create_app() -> FastAPI:
    configure_app_logging()
    mcp_server = build_mcp_server()
    mcp_asgi_app = mcp_server.streamable_http_app(
        streamable_http_path="/", transport_security=_mcp_transport_security())
    session_manager = mcp_server.session_manager

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        _apply_migrations()
        async with session_manager.run():
            yield

    application = FastAPI(title="Gootier MCP Server", lifespan=lifespan)
    application.middleware("http")(connection_key_middleware)
    application.include_router(internal_router)
    application.mount("/mcp", mcp_asgi_app)
    application.state.mcp_session_manager = session_manager

    @application.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=not _is_local(settings.app_base_url),
        max_age=60 * 60 * 24 * 2,
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static")
    application.include_router(web_router)
    application.add_exception_handler(StarletteHTTPException, html_error_handler)

    return application


app = create_app()
```

- [ ] **Step 4: Run test, confirm it passes**

Run: `pytest tests/test_main.py -v`
Expected: PASS. (This step will fail until Task 17 provides `app/web/routes.py` — if run before Task 17 is done, expect `ModuleNotFoundError: No module named 'app.web.routes'`; that's fine, complete Task 17 first if executing strictly in order, or treat Tasks 16 and 17 as a single unit if a subagent naturally does both together.)

- [ ] **Step 5: Commit** (after Task 17's files exist so this actually runs green)

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: FastAPI app factory, MCP mount, transport security"
```

---

## Task 17: `app/web/routes.py` + templates — `/`, `/enter`, `/connect`

**Files:**
- Create: `app/web/__init__.py` (empty)
- Create: `app/web/routes.py`
- Create: `app/web/templates/{base.html, signed_out.html, connect.html, error.html, offline.html}`
- Create: `app/web/static/{app.css, sw.js, icons/, manifest.webmanifest}`
- Create: `tests/test_web_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_routes.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session
import pytest

from app.auth.handoff import mint_handoff_token
from app.db import get_session
from app.models import Customer, ConnectionKey
from app.web import routes as routes_module
from app.web.routes import router


@pytest.fixture
def client(db_engine, monkeypatch):
    monkeypatch.setattr(routes_module.settings, "session_secret", "test-secret")
    monkeypatch.setattr(routes_module.settings, "app_base_url", "https://gootier-mcp.example.com")

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(router)

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as c:
        yield c, db_engine


def test_the_signed_out_index_renders(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200


def test_enter_with_a_valid_token_establishes_a_session_and_redirects(client):
    c, engine = client
    with Session(engine) as s:
        customer = Customer(email="jane@example.com")
        s.add(customer)
        s.commit()
        s.refresh(customer)
        customer_id = customer.id
    token = mint_handoff_token(customer_id)

    resp = c.get(f"/enter?t={token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/connect"


def test_enter_with_an_invalid_token_is_403(client):
    c, _ = client
    resp = c.get("/enter?t=garbage")
    assert resp.status_code == 403


def test_create_key_mints_a_new_connection_key(client):
    c, engine = client
    with Session(engine) as s:
        customer = Customer(email="jane@example.com")
        s.add(customer)
        s.commit()
        s.refresh(customer)
        customer_id = customer.id
    token = mint_handoff_token(customer_id)
    c.get(f"/enter?t={token}")

    connect_resp = c.get("/connect")
    # Extract the form_token from the rendered page is brittle for a unit
    # test -- instead verify the key-count effect directly via the session
    # cookie already established by /enter, bypassing the CSRF form_token
    # by calling create_key with the one the page issued.
    import re
    match = re.search(r'name="form_token" value="([^"]+)"', connect_resp.text)
    assert match, "connect.html must render a form_token hidden field"
    form_token = match.group(1)

    resp = c.post("/connect/keys", data={"form_token": form_token}, follow_redirects=False)
    assert resp.status_code == 200  # _render_connect renders directly, not a redirect

    with Session(engine) as s:
        keys = s.exec(
            __import__("sqlmodel").select(ConnectionKey).where(ConnectionKey.customer_id == customer_id)
        ).all()
        assert len(keys) == 1
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `pytest tests/test_web_routes.py -v`
Expected: `ModuleNotFoundError: No module named 'app.web.routes'`.

- [ ] **Step 3: Implement `app/web/routes.py`** (Jhome-MCP-Server's own `app/web/routes.py`, with the `/billing/tokens*` routes and all token-wallet imports removed — out of scope per the spec)

```python
# app/web/routes.py
"""The customer-facing pages: entry and connection credentials."""
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth.handoff import read_handoff_token
from app.auth.keys import display_prefix_of, mint_key
from app.config import settings
from app.db import get_session
from app.models import ConnectionKey, Customer, utcnow

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC = Path(__file__).parent / "static"

SESSION_KEY = "customer_id"
FORM_TOKEN_KEY = "form_tokens"
_MAX_FORM_TOKENS = 8


def issue_form_token(request: Request) -> str:
    tokens = request.session.get(FORM_TOKEN_KEY, [])
    token = secrets.token_urlsafe(16)
    tokens.append(token)
    request.session[FORM_TOKEN_KEY] = tokens[-_MAX_FORM_TOKENS:]
    return token


def consume_form_token(request: Request, token: str) -> bool:
    tokens = request.session.get(FORM_TOKEN_KEY, [])
    if token not in tokens:
        return False
    tokens.remove(token)
    request.session[FORM_TOKEN_KEY] = tokens
    return True


def current_customer(request: Request, session: Session) -> Customer:
    customer_id = request.session.get(SESSION_KEY)
    if not customer_id:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return customer


def sign_in_context() -> dict:
    base = settings.backoffice_url
    if not base:
        return {"sign_in_url": "", "create_account_url": ""}
    launch_path = f"/services/{settings.connected_app_slug}"
    return {
        "sign_in_url": f"{base}{launch_path}",
        "create_account_url": f"{base}/signup?next={quote(launch_path, safe='')}",
    }


@router.get("/")
def index(request: Request):
    if request.session.get(SESSION_KEY):
        return RedirectResponse("/connect", status_code=303)
    return templates.TemplateResponse(request, "signed_out.html", sign_in_context())


@router.get("/enter")
def enter(request: Request, t: str = "", session: Session = Depends(get_session)):
    customer_id = read_handoff_token(t)
    if customer_id is None or session.get(Customer, customer_id) is None:
        raise HTTPException(status_code=403, detail="This sign-in link is not valid.")
    request.session[SESSION_KEY] = customer_id
    return RedirectResponse("/connect", status_code=303)


def _render_connect(request: Request, session: Session, customer: Customer,
                    new_key: str | None = None):
    keys = session.exec(
        select(ConnectionKey)
        .where(ConnectionKey.customer_id == customer.id,
               ConnectionKey.revoked_at == None)          # noqa: E711 - SQL NULL
        .order_by(ConnectionKey.created_at.desc())
    ).all()
    response = templates.TemplateResponse(request, "connect.html", {
        "customer": customer,
        "keys": keys,
        "new_key": new_key,
        "endpoint": f"{settings.app_base_url}/mcp/",
        "form_token": issue_form_token(request),
    })
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/connect")
def connect(request: Request, session: Session = Depends(get_session)):
    return _render_connect(request, session, current_customer(request, session))


@router.post("/connect/keys")
def create_key(request: Request, form_token: str = Form(""),
              session: Session = Depends(get_session)):
    customer = current_customer(request, session)
    if not consume_form_token(request, form_token):
        return RedirectResponse("/connect", status_code=303)
    plaintext, hashed = mint_key()
    session.add(ConnectionKey(customer_id=customer.id, key_hash=hashed,
                              display_prefix=display_prefix_of(plaintext)))
    session.commit()
    return _render_connect(request, session, customer, new_key=plaintext)


@router.post("/connect/keys/{key_id}/revoke")
def revoke_key(key_id: int, request: Request, form_token: str = Form(""),
              session: Session = Depends(get_session)):
    customer = current_customer(request, session)
    if not consume_form_token(request, form_token):
        return RedirectResponse("/connect", status_code=303)
    key = session.exec(
        select(ConnectionKey).where(ConnectionKey.id == key_id,
                                    ConnectionKey.customer_id == customer.id)
    ).first()
    if key is None:
        raise HTTPException(status_code=404, detail="No such key.")
    key.revoked_at = utcnow()
    session.add(key)
    session.commit()
    return RedirectResponse("/connect", status_code=303)


_ERROR_COPY = {
    403: ("This sign-in link is not valid",
         "Sign-in links expire a short time after they are created. Open "
         "this service from your Gootier dashboard again to get a fresh one.",
         None, None),
    404: ("That page is not here",
         "The link may be out of date, or the thing it pointed at may have "
         "already been removed.",
         "/connect", "Go to your connection details"),
}
_ERROR_FALLBACK = ("Something went wrong",
                   "Try again, and if it keeps happening the details above "
                   "are what support will ask for.",
                   "/connect", "Go to your connection details")


def wants_html_error(request: Request, status_code: int) -> bool:
    if status_code < 400:
        return False
    path = request.url.path
    if path.startswith("/internal") or path.startswith("/mcp"):
        return False
    return "text/html" in request.headers.get("accept", "")


async def html_error_handler(request: Request, exc: HTTPException):
    if not wants_html_error(request, exc.status_code):
        return await http_exception_handler(request, exc)
    known = _ERROR_COPY.get(exc.status_code)
    heading, message, action_href, action_label = known or _ERROR_FALLBACK
    if known is None and isinstance(exc.detail, str) and exc.detail.endswith("."):
        message = exc.detail
    response = templates.TemplateResponse(
        request, "error.html",
        {"heading": heading, "message": message,
         "action_href": action_href, "action_label": action_label},
        status_code=exc.status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(_STATIC / "sw.js", media_type="application/javascript")


@router.get("/offline", include_in_schema=False)
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})
```

- [ ] **Step 4: Create the templates and static assets**

Copy `base.html`, `signed_out.html`, `connect.html`, `error.html`, `offline.html`, `app.css`, `sw.js`, `manifest.webmanifest`, and the `icons/` directory verbatim from `/Users/jaymevsmith/Documents/Claude/Projects/Jhome-MCP-Server/app/web/templates/` and `app/web/static/` into this repo's equivalent paths, then apply these exact substitutions:

- Every occurrence of "Jhome MCP Server" / "Jhome" (product name) → "Gootier MCP Server" / "Gootier"
- `connect.html`: **remove** the token-balance display block (`balance_display`, `purchase_url` — this template context no longer supplies them; Task 17's `_render_connect` above does not pass them) and any "Buy tokens" / `/billing/tokens` link
- `manifest.webmanifest`: `name`/`short_name` → "Gootier MCP Server" / "Gootier MCP", `theme_color`/`background_color` → Gootier's own accent (do not invent one — read it from Gootier's own `static/` CSS custom properties, mirroring the myweb-design house rule that accent always matches the host product)
- App icons: generate fresh 192px/512px/512px-maskable/apple-touch icons using Gootier's own product mark, not a copy of Jhome-MCP-Server's `⇄` glyph — matches the Backoffice registry icon-uniqueness convention (`test_no_two_rows_share_an_icon`) extended to this app's own PWA identity
- `app.css`: keep the dark-chrome structure verbatim (per the mobile-nav/PWA house rules already governing this fleet); the only required change is the accent token

Every one of the 6 Jhome-MCP-Server browser-facing statuses this page can reach (403, 404) must still have copy in `_ERROR_COPY` above — already ported.

- [ ] **Step 5: Run all tests, confirm they pass**

Run: `pytest tests/test_web_routes.py tests/test_main.py -v`
Expected: all tests PASS (Task 16's `test_main.py` now succeeds too, since `app.web.routes` exists).

- [ ] **Step 6: Commit**

```bash
git add app/web/ tests/test_web_routes.py app/main.py tests/test_main.py
git commit -m "feat: web routes, templates, and static assets"
```

---

## Task 18: Local repo finalized — STOP before creating a GitHub remote

**This task is a checkpoint, not code.**

- [ ] Run the FULL new-repo test suite from a clean state: `cd /Users/jaymevsmith/Documents/Claude/Projects/Gootier-MCP-Server && pytest -v`. Confirm everything passes together (not just per-file).
- [ ] Write `HANDOFF.md` and `STATUS.md` for this new repo (per the user's own standing rules — every project gets both), documenting: what this repo is, its relationship to Gootier and to Jhome-MCP-Server (the template it mirrors), what's built, and what's still open (deploy, Backoffice wiring).
- [ ] **STOP.** Creating a new GitHub repository and pushing this code publicly (even to a private repo) is a hard-to-reverse, externally-visible action. Ask the user explicitly before running `gh repo create` or `git push`.

---

## Task 19: Whole-branch review of Gootier-MCP-Server

**This task is a checkpoint, not code.**

- [ ] Run a **whole-branch review** (per `reviewing-whole-branches`) over the ENTIRE new repo's history so far, not per-commit — this repo has no `origin/main` yet, so review `git log --all -p` or the working tree as a whole. Specifically check:
  - Does `connection_key_middleware` correctly reject every malformed/missing/revoked-key case with an indistinguishable 401 (no timing or message difference that would let a key be enumerated)?
  - Does `app/internal_api.py`'s `_admit`/`_create_customer` logic match Jhome-MCP-Server's current version EXACTLY (not a regressed earlier draft) — specifically the `unverified_account` gate on binding?
  - Does every one of the 4 MCP tools in `server.py` go through `open_session()`/`current_customer()` (no tool bypasses session-scoped customer resolution)?
  - Is there any place `settings.gootier_internal_key` or `settings.internal_key` could leak into a log line or an error response?

---

# PART C — Cross-repo wiring and rollout

## Task 20: Backoffice registry entry

**Files (jhome-backoffice repo, primary checkout, direct commit to `main`):**
- Modify: `app/config.py`
- Modify: `app/connected/registry.py`
- Modify (or create): a registry test file, mirroring whatever test currently enforces `test_no_two_rows_share_an_icon`

- [ ] **Step 1: Confirm the icon-uniqueness test exists and find its file**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/jhome-backoffice
grep -rl "test_no_two_rows_share_an_icon" tests/
```

- [ ] **Step 2: Add the settings**

In `app/config.py`, directly below the `jhome_mcp_internal_url`/`jhome_mcp_internal_key` lines:

```python
    gootier_mcp_internal_url: str = os.getenv("GOOTIER_MCP_INTERNAL_URL", "")
    gootier_mcp_internal_key: str = os.getenv("GOOTIER_MCP_INTERNAL_KEY", "")
```

- [ ] **Step 3: Add the registry row**

In `app/connected/registry.py`, add a new `ConnectedApp` entry to `CONNECTED_APPS` (pick any unused icon glyph — confirmed unused against the existing list: `▣ ◍ ☎ ⚑ ◰ ❒ ⌖ ✉ ☁ ⚿ ◫ ▦ ◪ ▤ ◐ ▩ ⇄ ◱`):

```python
    ConnectedApp(
        slug="gootier-mcp",
        label="Gootier MCP Server",
        nav_label_override="Gootier MCP",
        description="Connect your own AI assistant to your Gootier account.",
        icon="◈",
        base_url_setting="gootier_mcp_internal_url",
        key_setting="gootier_mcp_internal_key",
        category="dev-ops",
    ),
```

- [ ] **Step 4: Run the existing registry test suite, confirm nothing regresses and the new row passes the icon-uniqueness check**

Run: `pytest tests/ -k registry -v` (or the exact file found in Step 1)
Expected: all PASS, including icon uniqueness with the new row included.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/connected/registry.py
git commit -m "feat: add Gootier MCP Server to the connected-app catalog"
```

This repo has no PR gate (established convention) — this commits directly to `main`, but per the standing discipline, **do not deploy jhome-backoffice** as part of this task. Deployment is bundled into Task 22 below, with its own explicit stop.

---

## Task 21: Provision the Gootier-MCP-Server service — STOP, user does this

**This task is a checkpoint, not code — every item here is either a hard-to-reverse infrastructure action or a real secret. Per the credential-handling rules, Claude does not create the service or type any secret value.**

- [ ] Ask the user to create a new Railway service for `Gootier-MCP-Server` (or their preferred host) and a Postgres database for it.
- [ ] Ask the user to set these variables on that service themselves (Claude verifies presence/length only, never values):
  - `DATABASE_URL` (from the provisioned Postgres)
  - `APP_BASE_URL` (the service's public URL)
  - `INTERNAL_KEY` (new secret — Backoffice will present this on its own `/internal/handoff` calls; must equal what's set as Backoffice's own `GOOTIER_MCP_INTERNAL_KEY` env var from Task 20)
  - `SESSION_SECRET` (new secret, signs the `/enter` handoff token)
  - `GOOTIER_PROVIDER=http`, `GOOTIER_INTERNAL_URL` (Gootier's own public base URL), `GOOTIER_INTERNAL_KEY` (new secret — must equal what's set as Gootier's own `GOOTIER_MCP_INTERNAL_KEY` from Part A Task 2)
  - `BACKOFFICE_URL` (Backoffice's public URL)
  - `CONNECTED_APP_SLUG=gootier-mcp`
- [ ] Ask the user to set `GOOTIER_MCP_INTERNAL_URL` and `GOOTIER_MCP_INTERNAL_KEY` on the Backoffice service to match the new Gootier-MCP-Server deployment.
- [ ] Ask the user to set `GOOTIER_MCP_INTERNAL_KEY` on Gootier itself to match what `Gootier-MCP-Server`'s own `GOOTIER_INTERNAL_KEY` was set to (these two names refer to the same shared secret from Gootier's side vs. Gootier-MCP-Server's side — restate this pairing plainly when asking, since the names are easy to cross).

---

## Task 22: Deploy all three — STOP for explicit user go-ahead

**This task is a checkpoint, not code.** Do not push, merge, or deploy anything in this task without the user's specific go-ahead for each repo — Gootier and jhome-backoffice are live products with real customers.

- [ ] Push Part A's branch, open a PR on Gootier (if not already done in Task 7), get it merged, and deploy Gootier from a fresh clone of `origin/main` (per the standing `railway up` discipline) — only after explicit confirmation.
- [ ] Deploy Gootier-MCP-Server for the first time (its first-ever deploy) — only after explicit confirmation and after Task 21's variables are confirmed set.
- [ ] Deploy jhome-backoffice with Task 20's registry change — only after explicit confirmation.
- [ ] After each deploy, verify in-container (not just the build log): the right process is running (`/proc/1/cmdline`), `get_gootier_client()` returns the expected type given the configured provider, and `/healthz` responds on Gootier-MCP-Server.

---

## Task 23: End-to-end real customer walk

**This task is a checkpoint, not code.** Mirrors the MidCanvas façade's own Task 11 — the step that found two real production bugs no unit test caught.

- [ ] Create a real throwaway test account (not a fixture) and drive the full path for real: from Backoffice, click through to Gootier-MCP-Server, mint a connection key, connect an MCP client, and for each of the 4 tools:
  - `gootier_draft_campaign`: draft a real campaign and confirm the rendered text is sensible, not raw JSON.
  - `gootier_list_social_connections`: confirm the IDs returned match what's actually connected.
  - `gootier_schedule_post`: schedule a real post to a real connected channel and confirm it actually publishes (check the channel itself, not just the response).
  - `gootier_schedule_email_blast`: send a real blast to a throwaway address and confirm delivery.
- [ ] Deliberately exhaust a quota on the throwaway account (e.g. set its tier's `posts_per_month` to something already met) and confirm the refusal renders the correct, specific sentence — not a generic fallback.
- [ ] Document findings in both repos' `HANDOFF.md`, exactly as the MidCanvas walk did — including anything that didn't work as expected, with root cause and fix.
