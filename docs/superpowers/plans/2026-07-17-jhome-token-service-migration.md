# Gootier → Jhome Token Service Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Gootier's homegrown `CreditLedger` billing system with the shared Jhome Token Service (JTS), so AI/media generation is metered against a JTS wallet instead of a local ledger, while tier subscriptions and post/blast/connection quotas stay untouched.

**Architecture:** A new `services/jts_client.py` (raw HTTP, httpx, mirrors the pattern already used in `jhome_affiliates.py` and in Site Agent/Studio-wan's `token_client.py`) talks to JTS's wallet/debit API. A thin `services/token_wallet.py` wraps it with Gootier-specific ergonomics (`ensure_wallet(db, user)`, `balance_tokens(db, user)`, `debit_after_success(db, user, model_key, request_id, **usage)`), caching `wallet_id` on `users.jts_wallet_id`. Every media-generation call site is changed from "spend credits before the call, refund on failure" to "check balance softly before the call, debit real usage only after the call succeeds" — removing the refund path entirely. The old top-up-pack Stripe flow is replaced by JTS's embeddable widget.

**Tech Stack:** FastAPI, SQLAlchemy (declarative `Base`, no Alembic — Gootier uses `_safe_add_column` + `Base.metadata.create_all` in `models.py::init_db()`), httpx, pytest (in-memory SQLite fixture in `tests/conftest.py`).

**Scoping note (read before starting):** Gootier currently has **no test coverage for `services/credits.py` or `routes/media_routes.py`** (`tests/` only contains `test_affiliates_integration.py` and `test_billing_period.py`, plus `conftest.py`). This plan writes real unit tests for the new/changed pure logic (the JTS client, the wallet wrapper, cost-estimation helpers) using the existing `db` fixture and a fake HTTP transport, matching the level of testing that already exists in this repo. It does **not** invent a new FastAPI `TestClient` route-testing harness from scratch — that would be a separate, larger undertaking than this migration. Task 14 covers manual end-to-end verification against the real (or a scratch) JTS deployment instead, the same way Studio-wan and Site Agent's integrations were live-verified.

---

### Task 0: Register Gootier as a JTS app (manual — no code)

**Files:** none (JTS admin UI + central credentials file)

- [ ] **Step 1: Log into JTS admin**

Go to `https://jhome-token-service-production.up.railway.app/admin/login`, log in with the `ADMIN_PASSWORD` from `/Users/jaymevsmith/Documents/Claude/env-template.md` (JTS section).

- [ ] **Step 2: Register the app**

On the admin dashboard (`/admin`), use the "Register App" form:
- `slug`: `gootier`
- `name`: `Gootier`
- `accent_color`: `#5b6ee1`
- `trial_tokens`: `2000000`

Submit. The response page shows the plaintext API key **exactly once** — copy it immediately.

- [ ] **Step 3: Save the key to the central credentials file**

Open `/Users/jaymevsmith/Documents/Claude/env-template.md` and add, under a `# ==== GOOTIER — JHOME TOKEN SERVICE ====` header (create it if it doesn't exist):

```
GOOTIER_TOKEN_SERVICE_API_KEY=<the jts_... key from step 2>
TOKEN_SERVICE_URL=https://jhome-token-service-production.up.railway.app
```

Do not echo the key value into any terminal command output — paste it directly into the file with the Edit tool.

- [ ] **Step 4: Add to Gootier's local `.env`**

Add the same two values to Gootier's `.env` (not committed to git):

```
TOKEN_SERVICE_API_KEY=<the jts_... key>
TOKEN_SERVICE_URL=https://jhome-token-service-production.up.railway.app
```

---

### Task 1: Register JTS Rate rows for Gootier's models

**Files:**
- Create: `/Users/jaymevsmith/Documents/Claude/Projects/Jhome-Token-Service/scripts/register_gootier_rates.py`

Real per-model costs (verified via fal.ai's pricing API 2026-07-17):

| model_key | provider | kind | unit | price_per_unit_usd | source |
|---|---|---|---|---|---|
| `fal-nano-banana-2` | fal | unit | image | 0.08 | `fal-ai/gemini-3.1-flash-image-preview/edit` |
| `fal-nano-banana-pro` | fal | unit | image | 0.15 | `fal-ai/gemini-3-pro-image-preview/edit` |
| `fal-flux-pro-ultra` | fal | unit | image | 0.06 | `fal-ai/flux-pro/v1.1-ultra` |
| `fal-kling-2.1-master` | fal | unit | second | 0.28 | `fal-ai/kling-video/v2.1/master/image-to-video` |
| `fal-kling-2.1-pro` | fal | unit | second | 0.098 | `fal-ai/kling-video/v2.1/pro/image-to-video` |
| `fal-veo-3.1` | fal | unit | second | 0.40 | `fal-ai/veo3.1/image-to-video` |
| `fal-veo-3.1-fast` | fal | unit | second | 0.15 | `fal-ai/veo3.1/fast/image-to-video` |
| `fal-elevenlabs-tts-turbo` | fal | unit | character | 0.00005 | `fal-ai/elevenlabs/tts/turbo-v2.5` ($0.05 / 1000 chars) |
| `fal-stable-audio-music` | fal | unit | second | 0.00125 | `fal-ai/stable-audio` (compute-seconds) |

The AI-plan Sonnet call reuses the **already-registered** `anthropic-sonnet-5` rate (input $3/1M, output $15/1M) — no new row needed for it.

- [ ] **Step 1: Write the registration script**

```python
"""One-off script: register Gootier's fal.ai model rates in JTS.

Run via `railway run --service jhome-token-service python3 scripts/register_gootier_rates.py`
so it picks up the production DATABASE_URL. Idempotent — safe to re-run.
"""
import sys
sys.path.insert(0, ".")

from sqlmodel import Session, select
from app.db import engine
from app.models import Rate

GOOTIER_RATES = [
    dict(provider="fal", model_key="fal-nano-banana-2",
         display_name="Nano Banana 2 (Gemini 3.1 Flash Image Edit)",
         kind="unit", unit="image", price_per_unit_usd=0.08),
    dict(provider="fal", model_key="fal-nano-banana-pro",
         display_name="Nano Banana Pro (Gemini 3 Pro Image Edit)",
         kind="unit", unit="image", price_per_unit_usd=0.15),
    dict(provider="fal", model_key="fal-flux-pro-ultra",
         display_name="Flux 1.1 Pro Ultra",
         kind="unit", unit="image", price_per_unit_usd=0.06),
    dict(provider="fal", model_key="fal-kling-2.1-master",
         display_name="Kling 2.1 Master (image-to-video)",
         kind="unit", unit="second", price_per_unit_usd=0.28),
    dict(provider="fal", model_key="fal-kling-2.1-pro",
         display_name="Kling 2.1 Pro (image-to-video)",
         kind="unit", unit="second", price_per_unit_usd=0.098),
    dict(provider="fal", model_key="fal-veo-3.1",
         display_name="Veo 3.1 (image-to-video)",
         kind="unit", unit="second", price_per_unit_usd=0.40),
    dict(provider="fal", model_key="fal-veo-3.1-fast",
         display_name="Veo 3.1 Fast (image-to-video)",
         kind="unit", unit="second", price_per_unit_usd=0.15),
    dict(provider="fal", model_key="fal-elevenlabs-tts-turbo",
         display_name="ElevenLabs TTS Turbo v2.5",
         kind="unit", unit="character", price_per_unit_usd=0.00005),
    dict(provider="fal", model_key="fal-stable-audio-music",
         display_name="Stable Audio (music generation)",
         kind="unit", unit="second", price_per_unit_usd=0.00125),
]


def main() -> None:
    with Session(engine) as session:
        created, skipped = 0, 0
        for spec in GOOTIER_RATES:
            existing = session.exec(
                select(Rate).where(Rate.model_key == spec["model_key"])
            ).first()
            if existing:
                skipped += 1
                continue
            session.add(Rate(**spec))
            created += 1
        session.commit()
        print(f"[register_gootier_rates] created={created} skipped_existing={skipped}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it against production**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/Jhome-Token-Service
railway run --service jhome-token-service python3 scripts/register_gootier_rates.py
```

Expected output: `[register_gootier_rates] created=9 skipped_existing=0` (or fewer created / more skipped on a re-run).

- [ ] **Step 3: Verify via the public catalog-adjacent rates**

```bash
railway run --service jhome-token-service python3 -c "
from sqlmodel import Session, select
from app.db import engine
from app.models import Rate
with Session(engine) as s:
    for r in s.exec(select(Rate).where(Rate.provider == 'fal')).all():
        print(r.model_key, r.kind, r.unit, r.price_per_unit_usd)
"
```

Expected: all 9 `fal-*` rows from the table above are listed, plus any pre-existing ones from other apps.

- [ ] **Step 4: Commit the script**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/Jhome-Token-Service
git add scripts/register_gootier_rates.py
git commit -m "Register Gootier fal.ai model rates"
```

---

### Task 2: `services/jts_client.py` — raw HTTP client

**Files:**
- Create: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/services/jts_client.py`
- Test: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/tests/test_jts_client.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_jts_client.py"""
import httpx
import pytest

from services.jts_client import JTSClient, JTSError, InsufficientTokensError


def _client(handler) -> JTSClient:
    transport = httpx.MockTransport(handler)
    return JTSClient(base_url="https://jts.test", api_key="jts_test", transport=transport)


def test_ensure_wallet_returns_wallet_id():
    def handler(request):
        assert request.headers["X-API-Key"] == "jts_test"
        assert request.url.path == "/wallets"
        return httpx.Response(200, json={"wallet_id": 42, "balance_tokens": 2000000})
    wallet_id = _client(handler).ensure_wallet(external_user_id="7", email="a@b.com")
    assert wallet_id == 42


def test_get_balance_returns_raw_tokens():
    def handler(request):
        assert request.url.path == "/wallets/42/balance"
        return httpx.Response(200, json={"wallet_id": 42, "balance_tokens": 500000, "balance_display": 500})
    balance = _client(handler).get_balance(42)
    assert balance == 500000


def test_debit_sends_model_key_usage_and_request_id():
    captured = {}
    def handler(request):
        captured["body"] = httpx.Request(request.method, request.url, content=request.content).content
        import json as _json
        captured["json"] = _json.loads(request.content)
        return httpx.Response(200, json={
            "tokens_charged": 8100, "tokens_charged_display": 8,
            "balance_tokens": 99991900, "balance_display": 99992,
        })
    result = _client(handler).debit(
        wallet_id=42, model_key="fal-nano-banana-2", request_id="gootier-mediajob-9",
        units=1,
    )
    assert captured["json"] == {
        "model_key": "fal-nano-banana-2", "units": 1, "request_id": "gootier-mediajob-9",
    }
    assert result["tokens_charged"] == 8100


def test_debit_raises_insufficient_tokens_on_402():
    def handler(request):
        return httpx.Response(402, json={
            "error": "insufficient tokens", "balance_tokens": 100, "balance_display": 0,
            "tokens_required": 8100, "tokens_required_display": 8,
        })
    with pytest.raises(InsufficientTokensError) as exc_info:
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)
    assert exc_info.value.balance_tokens == 100
    assert exc_info.value.tokens_required == 8100


def test_debit_raises_jts_error_on_unexpected_status():
    def handler(request):
        return httpx.Response(500, text="boom")
    with pytest.raises(JTSError):
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier
pytest tests/test_jts_client.py -v
```
Expected: `ModuleNotFoundError: No module named 'services.jts_client'`

- [ ] **Step 3: Write `services/jts_client.py`**

```python
"""Raw HTTP client for the Jhome Token Service (JTS) wallet/debit API.

Deliberately thin — no fail-open/fail-closed policy here (that lives in
services/token_wallet.py). This module talks HTTP and raises typed errors;
callers decide what to do with them."""
from typing import Optional

import httpx

from services.env_config import get_env


class JTSError(Exception):
    """Any non-2xx JTS response that isn't a typed insufficient-tokens case."""


class InsufficientTokensError(JTSError):
    def __init__(self, balance_tokens: int, tokens_required: int,
                 balance_display: int, tokens_required_display: int):
        self.balance_tokens = balance_tokens
        self.tokens_required = tokens_required
        self.balance_display = balance_display
        self.tokens_required_display = tokens_required_display
        super().__init__(
            f"insufficient tokens: have {balance_display}, need {tokens_required_display}"
        )


class JTSClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = (base_url or get_env(
            "TOKEN_SERVICE_URL", "https://jhome-token-service-production.up.railway.app"
        )).rstrip("/")
        self.api_key = api_key or get_env("TOKEN_SERVICE_API_KEY", "")
        self._client = httpx.Client(transport=transport, timeout=10)

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    def ensure_wallet(self, external_user_id: str, email: str = "") -> int:
        resp = self._client.post(
            f"{self.base_url}/wallets",
            headers=self._headers(),
            json={"external_user_id": external_user_id, "email": email},
        )
        if resp.status_code != 200:
            raise JTSError(f"ensure_wallet failed: {resp.status_code} {resp.text}")
        return int(resp.json()["wallet_id"])

    def get_balance(self, wallet_id: int) -> int:
        resp = self._client.get(
            f"{self.base_url}/wallets/{wallet_id}/balance",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise JTSError(f"get_balance failed: {resp.status_code} {resp.text}")
        return int(resp.json()["balance_tokens"])

    def debit(self, wallet_id: int, model_key: str, request_id: str, **usage) -> dict:
        resp = self._client.post(
            f"{self.base_url}/wallets/{wallet_id}/debit",
            headers=self._headers(),
            json={"model_key": model_key, "request_id": request_id, **usage},
        )
        if resp.status_code == 402:
            body = resp.json().get("detail", resp.json())
            raise InsufficientTokensError(
                balance_tokens=body["balance_tokens"],
                tokens_required=body["tokens_required"],
                balance_display=body["balance_display"],
                tokens_required_display=body["tokens_required_display"],
            )
        if resp.status_code != 200:
            raise JTSError(f"debit failed: {resp.status_code} {resp.text}")
        return resp.json()
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_jts_client.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add services/jts_client.py tests/test_jts_client.py
git commit -m "Add JTS HTTP client (wallet ensure/balance/debit)"
```

---

### Task 3: `services/token_wallet.py` — app-facing wrapper + `jts_wallet_id` column

**Files:**
- Create: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/services/token_wallet.py`
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/models.py:439-448` (`_upgrade_users`)
- Test: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/tests/test_token_wallet.py`

This mirrors `services/credits.py`'s call shape (`(db, user, ...)`) so call sites read the same way as before.

- [ ] **Step 1: Add the `jts_wallet_id` column**

In `models.py`, add to the `User` class (near `stripe_customer_id`, around line 43):

```python
    jts_wallet_id = Column(Integer, nullable=True)
```

And add to `_upgrade_users` (models.py:439-448):

```python
def _upgrade_users(conn):
    _safe_add_column(conn, "users", "stripe_customer_id",        "VARCHAR")
    _safe_add_column(conn, "users", "subscribed_until",          "TIMESTAMP")
    _safe_add_column(conn, "users", "reset_token",               "VARCHAR")
    _safe_add_column(conn, "users", "reset_token_expires_at",    "TIMESTAMP")
    _safe_add_column(conn, "users", "verify_token",              "VARCHAR")
    _safe_add_column(conn, "users", "verify_token_expires_at",   "TIMESTAMP")
    _safe_add_column(conn, "users", "calendar_token",            "VARCHAR")
    _safe_add_column(conn, "users", "google_sub",                "VARCHAR")
    _safe_add_column(conn, "users", "referral_code",             "VARCHAR")
    _safe_add_column(conn, "users", "jts_wallet_id",             "INTEGER")
```

- [ ] **Step 2: Write the failing tests**

```python
"""tests/test_token_wallet.py"""
import pytest
from fastapi import HTTPException

from models import User
from services import token_wallet


class FakeJTSClient:
    def __init__(self, balance=2_000_000):
        self.balance = balance
        self.debit_calls = []
        self.ensure_wallet_calls = []

    def ensure_wallet(self, external_user_id, email=""):
        self.ensure_wallet_calls.append(external_user_id)
        return 999

    def get_balance(self, wallet_id):
        return self.balance

    def debit(self, wallet_id, model_key, request_id, **usage):
        self.debit_calls.append({"wallet_id": wallet_id, "model_key": model_key,
                                  "request_id": request_id, **usage})
        return {"tokens_charged": 8100, "balance_tokens": self.balance - 8100}


def _user(db) -> User:
    u = User(username="u1", email="u1@test.com", hashed_password="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_ensure_wallet_caches_id_on_user(db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    wallet_id = token_wallet.ensure_wallet(db, user)

    assert wallet_id == 999
    assert user.jts_wallet_id == 999
    assert fake.ensure_wallet_calls == [str(user.id)]

    # second call doesn't hit JTS again
    token_wallet.ensure_wallet(db, user)
    assert fake.ensure_wallet_calls == [str(user.id)]


def test_check_sufficient_raises_402_when_estimate_exceeds_balance(db, monkeypatch):
    fake = FakeJTSClient(balance=1000)
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    with pytest.raises(HTTPException) as exc_info:
        token_wallet.check_sufficient(db, user, estimated_tokens=5000)
    assert exc_info.value.status_code == 402


def test_check_sufficient_passes_when_balance_covers_estimate(db, monkeypatch):
    fake = FakeJTSClient(balance=2_000_000)
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    token_wallet.check_sufficient(db, user, estimated_tokens=5000)  # should not raise


def test_debit_after_success_passes_through_usage_and_request_id(db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    result = token_wallet.debit_after_success(
        db, user, model_key="fal-nano-banana-2",
        request_id="gootier-mediajob-123", units=1,
    )

    assert result["tokens_charged"] == 8100
    assert fake.debit_calls == [{
        "wallet_id": 999, "model_key": "fal-nano-banana-2",
        "request_id": "gootier-mediajob-123", "units": 1,
    }]
```

- [ ] **Step 3: Run to verify it fails**

```bash
pytest tests/test_token_wallet.py -v
```
Expected: `ModuleNotFoundError: No module named 'services.token_wallet'`

- [ ] **Step 4: Write `services/token_wallet.py`**

```python
"""App-facing wrapper around services/jts_client.py.

Signature convention mirrors the old services/credits.py: (db, user, ...).
Wallet id is cached on User.jts_wallet_id after first lookup."""
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import User
from services.jts_client import InsufficientTokensError, JTSClient, JTSError

log = logging.getLogger("gootier.token_wallet")

_client_instance: Optional[JTSClient] = None


def _client() -> JTSClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JTSClient()
    return _client_instance


def ensure_wallet(db: Session, user: User) -> int:
    """Get-or-create the user's JTS wallet id, caching it on the User row."""
    if user.jts_wallet_id:
        return user.jts_wallet_id
    wallet_id = _client().ensure_wallet(external_user_id=str(user.id), email=user.email)
    user.jts_wallet_id = wallet_id
    db.commit()
    return wallet_id


def balance_tokens(db: Session, user: User) -> int:
    wallet_id = ensure_wallet(db, user)
    return _client().get_balance(wallet_id)


def check_sufficient(db: Session, user: User, estimated_tokens: int) -> None:
    """Soft pre-flight gate — not atomic, purely UX (see design doc 'Debit timing').
    Raises 402 if the current balance clearly can't cover the estimate."""
    current = balance_tokens(db, user)
    if current < estimated_tokens:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient tokens: you have {current // 1000}, this needs "
                   f"about {estimated_tokens // 1000}. Top up at /billing.",
        )


def debit_after_success(db: Session, user: User, model_key: str, request_id: str,
                         **usage) -> Optional[dict]:
    """Call only after the AI/media call has already succeeded. Never raises —
    a debit failure must not undo or block a result the user already has;
    it's logged loudly instead so it can be reconciled manually."""
    wallet_id = ensure_wallet(db, user)
    try:
        return _client().debit(wallet_id, model_key, request_id, **usage)
    except InsufficientTokensError:
        log.error("token debit found insufficient balance after success: "
                  "user=%s model_key=%s request_id=%s", user.id, model_key, request_id)
        return None
    except JTSError:
        log.exception("token debit failed: user=%s model_key=%s request_id=%s",
                      user.id, model_key, request_id)
        return None
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
pytest tests/test_token_wallet.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add services/token_wallet.py tests/test_token_wallet.py models.py
git commit -m "Add token_wallet wrapper + jts_wallet_id column"
```

---

### Task 4: Image job — pre-flight check, debit-after-success, remove refund

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/media_routes.py:247-330` (`create_image_job`, `_refund_and_fail`)
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/media_routes.py:1021-1070` (`fal_webhook`)

`MEDIA_MODEL_CATALOG["image"]` keys map 1:1 to the rate keys from Task 1 (`nano-banana-2` → `fal-nano-banana-2`, etc.) — add that mapping once and reuse it for both image and video.

- [ ] **Step 1: Add the model-key → JTS rate-key map and a cost estimator**

In `services/media.py`, near `MEDIA_MODEL_CATALOG` (after line 97):

```python
# Maps this catalog's short keys to the JTS Rate.model_key registered for
# them (see Jhome-Token-Service/scripts/register_gootier_rates.py).
JTS_RATE_KEY = {
    "nano-banana-2":   "fal-nano-banana-2",
    "nano-banana-pro": "fal-nano-banana-pro",
    "flux-pro-ultra":  "fal-flux-pro-ultra",
    "kling-2.1-master": "fal-kling-2.1-master",
    "kling-2.1-pro":    "fal-kling-2.1-pro",
    "veo-3.1":          "fal-veo-3.1",
    "veo-3.1-fast":     "fal-veo-3.1-fast",
}

# Real USD price per unit, mirrored from the same script — used only for the
# soft pre-flight balance estimate (never for the actual debit, which always
# reports real usage to JTS).
JTS_PRICE_PER_UNIT_USD = {
    "nano-banana-2":   0.08,
    "nano-banana-pro": 0.15,
    "flux-pro-ultra":  0.06,
    "kling-2.1-master": 0.28,
    "kling-2.1-pro":    0.098,
    "veo-3.1":          0.40,
    "veo-3.1-fast":     0.15,
}


def estimate_tokens(model_key: str, units: float = 1) -> int:
    """USD cost estimate -> raw JTS tokens (1 token = $0.000001), for the
    soft pre-flight balance check only."""
    usd = JTS_PRICE_PER_UNIT_USD[model_key] * units
    return int(usd * 1_000_000)
```

- [ ] **Step 2: Update `create_image_job`**

In `routes/media_routes.py`, replace lines 275-276:

```python
    cost = int(model["credits"])
    credits_spend(db, user, cost, reason="image_gen", detail=f"model={model['key']}")
```

with:

```python
    from services.media import JTS_RATE_KEY, estimate_tokens
    from services.token_wallet import check_sufficient
    check_sufficient(db, user, estimate_tokens(model["key"]))
```

Then remove the now-unused `cost_credits=cost,` field when constructing `job = MediaJob(...)` a few lines below (keep the rest of the constructor as-is) — `MediaJob.cost_credits` stays in the schema (harmless, no longer written by this path) since it's still read by `_serialize_job` for old rows.

- [ ] **Step 3: Replace `_refund_and_fail` with `_mark_failed` (no refund)**

Replace lines 321-329:

```python
def _refund_and_fail(db: Session, user: User, job: MediaJob, error: str) -> None:
    job.status = "failed"
    job.error = error
    job.completed_at = datetime.utcnow()
    db.commit()
    if job.cost_credits:
        credits_grant(db, user, job.cost_credits,
                      reason=f"refund_failed_{job.id}",
                      detail=f"Auto-refund for failed media job #{job.id}: {error[:120]}")
```

with:

```python
def _mark_failed(db: Session, user: User, job: MediaJob, error: str) -> None:
    """No refund needed — under JTS billing, nothing is charged until the job
    succeeds (see fal_webhook), so a failure before that point never spent
    anything."""
    job.status = "failed"
    job.error = error
    job.completed_at = datetime.utcnow()
    db.commit()
```

Update the two call sites in `create_image_job` (lines 310-315) and `create_video_job` (lines 1006-1011, changed in Task 5) from `_refund_and_fail(...)` to `_mark_failed(...)`.

- [ ] **Step 4: Debit in `fal_webhook` on success**

In `fal_webhook` (routes/media_routes.py:1051-1063), after `job.status = "done"` / `db.commit()` and before the `log_action` call, add:

```python
            from services.media import JTS_RATE_KEY
            from services.token_wallet import debit_after_success
            if job.kind == "video":
                units = float(job.duration_seconds or 5)
            else:
                units = 1
            debit_after_success(
                db, job.user, model_key=JTS_RATE_KEY[job.model_key],
                request_id=f"gootier-mediajob-{job.id}",
                units=units,
            )
```

Full updated block:

```python
    if status == "OK":
        try:
            if job.kind == "video":
                url = extract_video_url(body)
            else:
                url = extract_first_image_url(body)
            job.result_url = url
            job.thumbnail_url = url
            job.status = "done"
            job.completed_at = datetime.utcnow()
            db.commit()
            from services.media import JTS_RATE_KEY
            from services.token_wallet import debit_after_success
            if job.kind == "video":
                units = float(job.duration_seconds or 5)
            else:
                units = 1
            debit_after_success(
                db, job.user, model_key=JTS_RATE_KEY[job.model_key],
                request_id=f"gootier-mediajob-{job.id}",
                units=units,
            )
            log_action(db, job.user, "UPDATE", "MediaJob", str(job.id),
                       detail="completed via webhook")
        except Exception as e:
            _mark_failed(db, job.user, job, f"webhook result parse failed: {e}")
    elif status == "ERROR":
        err = payload.get("error") or "fal reported ERROR"
        _mark_failed(db, job.user, job, str(err))
```

Note: `job.model_key` is only set for fal-hosted jobs (`"nano-banana-2"`, `"kling-2.1-master"`, etc.) — compose jobs use `model_key="compose"` and never reach this branch of `fal_webhook` (they complete via `_run_compose_job`, Task 6), so the `JTS_RATE_KEY[job.model_key]` lookup is safe here.

- [ ] **Step 5: Manually verify no leftover references**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier
grep -n "credits_spend\|credits_grant\|_refund_and_fail" routes/media_routes.py
```
Expected at this point: only the video/compose/music/ai-plan call sites (not yet migrated — Tasks 5-8) still show up. No `_refund_and_fail` should remain (renamed everywhere it's referenced in image/video).

- [ ] **Step 6: Commit**

```bash
git add routes/media_routes.py services/media.py
git commit -m "Migrate image job billing to JTS (debit-after-success)"
```

---

### Task 5: Video job — same pattern

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/media_routes.py:950-1014` (`create_video_job`)

- [ ] **Step 1: Update `create_video_job`**

Replace lines 967-968:

```python
    cost = int(model["credits"])
    credits_spend(db, user, cost, reason="video_gen", detail=f"model={model['key']}")
```

with:

```python
    from services.media import estimate_tokens
    from services.token_wallet import check_sufficient
    check_sufficient(db, user, estimate_tokens(model["key"], units=payload.duration_seconds or 5))
```

- [ ] **Step 2: Rename the failure call site**

Line 1007 and 1010: change `_refund_and_fail(db, user, job, ...)` to `_mark_failed(db, user, job, ...)` (this completes the rename started in Task 4 — `_refund_and_fail` should no longer exist anywhere after this step).

- [ ] **Step 3: Verify**

```bash
grep -n "_refund_and_fail\|credits_spend\|credits_grant" routes/media_routes.py
```
Expected: no matches for `create_video_job`/`create_image_job` anymore (compose/music/ai-plan still pending).

- [ ] **Step 4: Commit**

```bash
git add routes/media_routes.py
git commit -m "Migrate video job billing to JTS (debit-after-success)"
```

---

### Task 6: Compose job — TTS-only real cost, no charge for ffmpeg compute

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/media_routes.py:490-700` (`create_compose_job`, `_run_compose_job`)

Per the approved design, the ffmpeg compositing step itself has no external per-call USD cost to register as a JTS rate (unlike image/video/music, which call a priced fal endpoint) — only the optional ElevenLabs narration is billed. A compose job with no narration and no music surcharge costs 0 tokens.

- [ ] **Step 1: Update `create_compose_job`**

Replace lines 562-582:

```python
    # Credit cost: flat 50 for the compose + TTS surcharge if narration set.
    cost = 50
    tts_endpoint = None
    voice_id = None
    if payload.narration_script:
        script = (payload.narration_script or "").strip()
        if len(script) < 4:
            raise HTTPException(status_code=400, detail="Narration script too short.")
        if len(script) > 1500:
            raise HTTPException(status_code=400, detail="Narration script too long (max 1500 chars).")
        cat = tts_catalog()
        default = next((k for k, v in cat.items() if v.get("default")), None)
        chosen = payload.narration_voice or "Rachel"
        model = cat.get(default) or next(iter(cat.values()))
        tts_endpoint = model["endpoint"]
        voice_map = dict(model["voices"])
        voice_id = voice_map.get(chosen) or list(voice_map.values())[0]
        cost += int((len(script) / 100) * model["credits_per_100_chars"]) + 1

    credits_spend(db, user, cost, reason="video_compose",
                  detail=f"clips={len(ordered_clips)} tts={'yes' if tts_endpoint else 'no'} music={'yes' if payload.music_url else 'no'}")
```

with:

```python
    tts_endpoint = None
    voice_id = None
    narration_chars = 0
    if payload.narration_script:
        script = (payload.narration_script or "").strip()
        if len(script) < 4:
            raise HTTPException(status_code=400, detail="Narration script too short.")
        if len(script) > 1500:
            raise HTTPException(status_code=400, detail="Narration script too long (max 1500 chars).")
        cat = tts_catalog()
        default = next((k for k, v in cat.items() if v.get("default")), None)
        chosen = payload.narration_voice or "Rachel"
        model = cat.get(default) or next(iter(cat.values()))
        tts_endpoint = model["endpoint"]
        voice_map = dict(model["voices"])
        voice_id = voice_map.get(chosen) or list(voice_map.values())[0]
        narration_chars = len(script)

    if narration_chars:
        from services.token_wallet import check_sufficient
        check_sufficient(db, user, int(narration_chars * 0.00005 * 1_000_000))
```

- [ ] **Step 2: Drop `cost_credits=cost,` from the `MediaJob(...)` constructor** (lines 584-602) — leave every other field as-is.

- [ ] **Step 3: Update `_run_compose_job`'s success and failure paths**

Replace the success block (lines 663-677):

```python
            url = await compose(
                clip_urls,
                narration_path=narration_path,
                music_path=music_path,
                keep_original_audio=keep_original,
                clip_options=clip_options,
                transitions=transitions,
                text_overlays=text_overlays,
            )
            job.result_url = url
            job.thumbnail_url = url
            job.status = "done"
            job.completed_at = _dt.utcnow()
            db.commit()
            log.info("compose job %s done -> %s", job.id, url)
```

with:

```python
            url = await compose(
                clip_urls,
                narration_path=narration_path,
                music_path=music_path,
                keep_original_audio=keep_original,
                clip_options=clip_options,
                transitions=transitions,
                text_overlays=text_overlays,
            )
            job.result_url = url
            job.thumbnail_url = url
            job.status = "done"
            job.completed_at = _dt.utcnow()
            db.commit()
            if narration_chars:
                from services.token_wallet import debit_after_success
                user = db.query(User).filter(User.id == user_id).first()
                debit_after_success(
                    db, user, model_key="fal-elevenlabs-tts-turbo",
                    request_id=f"gootier-mediajob-{job.id}",
                    units=narration_chars,
                )
            log.info("compose job %s done -> %s", job.id, url)
```

(This requires `_run_compose_job`'s signature to also receive `narration_chars` — add it as a parameter alongside the existing `narration_script` argument and pass it through from the call site in Step 1/2's `_asyncio.create_task(_run_compose_job(...))` call.)

Replace the exception handler (lines 682-698):

```python
    except Exception as e:
        log.exception("compose job %s failed: %s", job_id, e)
        try:
            job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
            if job and job.status != "done":
                from services.credits import grant as _grant
                user = db.query(User).filter(User.id == user_id).first()
                if user and job.cost_credits:
                    _grant(db, user, job.cost_credits,
                           reason=f"refund_failed_{job.id}",
                           detail=f"Auto-refund — compose failed: {e}")
                job.status = "failed"
                job.error = str(e)[:1000]
                job.completed_at = _dt.utcnow()
                db.commit()
        except Exception:
            log.exception("refund/finalise also failed for job %s", job_id)
```

with:

```python
    except Exception as e:
        log.exception("compose job %s failed: %s", job_id, e)
        try:
            job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
            if job and job.status != "done":
                job.status = "failed"
                job.error = str(e)[:1000]
                job.completed_at = _dt.utcnow()
                db.commit()
        except Exception:
            log.exception("finalise also failed for job %s", job_id)
```

- [ ] **Step 4: Update the `_asyncio.create_task(_run_compose_job(...))` call site** (lines 613-619) to pass `narration_chars` (computed in Step 1) as a new positional/keyword argument matching the new `_run_compose_job` signature.

- [ ] **Step 5: Verify**

```bash
grep -n "credits_spend\|credits_grant\|from services.credits import" routes/media_routes.py
```
Expected: only the music (Task 7) and ai-plan (Task 8) call sites remain.

- [ ] **Step 6: Commit**

```bash
git add routes/media_routes.py
git commit -m "Migrate compose job billing to JTS (TTS-only real cost)"
```

---

### Task 7: Music job — real fal stable-audio cost

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/media_routes.py:876-947` (`create_music_job`, `_run_music_job`)

- [ ] **Step 1: Update `create_music_job`**

Replace lines 888-891:

```python
    cost = 8
    credits_spend(db, user, cost, reason="music_generate",
                  detail=f"seconds={payload.seconds} prompt_chars={len(payload.prompt)}")
```

with:

```python
    from services.token_wallet import check_sufficient
    check_sufficient(db, user, int(payload.seconds * 0.00125 * 1_000_000))
```

Drop `cost_credits=cost,` from the `MediaJob(...)` constructor a few lines below.

- [ ] **Step 2: Update `_run_music_job`'s success/failure paths**

Replace lines 925-945:

```python
        try:
            url = await generate_music(prompt, seconds=seconds)
            if not url:
                raise RuntimeError("Music generator returned no URL.")
            job.result_url = url
            job.status = "done"
            job.completed_at = _dt.utcnow()
            db.commit()
            log.info("music job %s done -> %s", job.id, url)
        except Exception as e:
            log.exception("music job %s failed: %s", job_id, e)
            from services.credits import grant as _grant
            user = db.query(User).filter(User.id == user_id).first()
            if user and job.cost_credits:
                _grant(db, user, job.cost_credits,
                       reason=f"refund_failed_{job.id}",
                       detail=f"Auto-refund — music gen failed: {e}")
            job.status = "failed"
            job.error = str(e)[:1000]
            job.completed_at = _dt.utcnow()
            db.commit()
```

with:

```python
        try:
            url = await generate_music(prompt, seconds=seconds)
            if not url:
                raise RuntimeError("Music generator returned no URL.")
            job.result_url = url
            job.status = "done"
            job.completed_at = _dt.utcnow()
            db.commit()
            from services.token_wallet import debit_after_success
            user = db.query(User).filter(User.id == user_id).first()
            debit_after_success(
                db, user, model_key="fal-stable-audio-music",
                request_id=f"gootier-mediajob-{job.id}",
                units=seconds,
            )
            log.info("music job %s done -> %s", job.id, url)
        except Exception as e:
            log.exception("music job %s failed: %s", job_id, e)
            job.status = "failed"
            job.error = str(e)[:1000]
            job.completed_at = _dt.utcnow()
            db.commit()
```

- [ ] **Step 3: Verify**

```bash
grep -n "credits_spend\|credits_grant\|from services.credits import" routes/media_routes.py
```
Expected: only the ai-plan call site remains (Task 8).

- [ ] **Step 4: Commit**

```bash
git add routes/media_routes.py
git commit -m "Migrate music job billing to JTS (real per-second cost)"
```

---

### Task 8: `compose/ai-plan` — real Anthropic token usage

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/services/ai_generator.py:110-152` (`plan_video_compose`)
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/media_routes.py:718-774` (`compose_ai_plan`)

- [ ] **Step 1: Make `plan_video_compose` return usage alongside the plan**

Replace lines 139-152:

```python
    response = _client().messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": _VIDEO_PLANNER_SYSTEM}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
```

with:

```python
    response = _client().messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": _VIDEO_PLANNER_SYSTEM}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        plan = json.loads(cleaned)
    return {**plan, "_usage": usage}
```

Note: `_usage` is popped off by the caller before the plan is returned to the client (Step 2) — it must never leak into the API response.

- [ ] **Step 2: Update `compose_ai_plan`**

Replace lines 755-774:

```python
    credits_spend(db, user, 2, reason="video_compose_ai_plan",
                  detail=f"clips={len(ordered_clips)} brief_chars={len(payload.brief)}")

    # 2) Call Sonnet for the plan.
    from services.ai_generator import plan_video_compose
    try:
        plan = plan_video_compose(
            brief=payload.brief,
            clip_meta=clip_meta,
            want_music=payload.want_music,
            want_text=payload.want_text,
            style=payload.style,
        )
    except Exception as e:
        # Refund the 2 credits on failure — same pattern as compose worker.
        from services.credits import grant as _grant
        _grant(db, user, 2,
               reason="refund_ai_plan_failed",
               detail=f"AI plan failed: {str(e)[:200]}")
        raise HTTPException(status_code=502, detail=f"AI plan failed: {e}")
```

with:

```python
    from services.token_wallet import check_sufficient, debit_after_success
    # Conservative pre-flight estimate: ~2000 input + 2048 max output tokens
    # at anthropic-sonnet-5 blended pricing ($3/$15 per 1M). Real debit below
    # always uses the actual reported usage, never this estimate.
    check_sufficient(db, user, estimated_tokens=40_000)

    # 2) Call Sonnet for the plan.
    from services.ai_generator import plan_video_compose
    import secrets as _py_secrets
    try:
        result = plan_video_compose(
            brief=payload.brief,
            clip_meta=clip_meta,
            want_music=payload.want_music,
            want_text=payload.want_text,
            style=payload.style,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI plan failed: {e}")

    usage = result.pop("_usage")
    plan = result
    debit_after_success(
        db, user, model_key="anthropic-sonnet-5",
        request_id=f"gootier-aiplan-{_py_secrets.token_urlsafe(12)}",
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
    )
```

Note: this endpoint has no persisted job row to derive a stable `request_id` from (unlike media jobs), so a fresh random suffix is used — a double-charge on client retry is possible here in the rare case of a network blip between the Anthropic call succeeding and the debit call landing. This is the same tradeoff already accepted for the pre-flight check (see design doc); flagged here rather than silently accepted.

- [ ] **Step 3: Verify no `services.credits` imports remain in either file**

```bash
grep -rn "from services.credits import\|services\.credits\." routes/media_routes.py services/ai_generator.py
```
Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add services/ai_generator.py routes/media_routes.py
git commit -m "Migrate compose/ai-plan billing to JTS (real Sonnet usage)"
```

---

### Task 9: Wallet creation at signup

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/auth_routes.py:130-143` (`signup_submit`)

- [ ] **Step 1: Call `ensure_wallet` right after the new user is committed**

In `signup_submit` (routes/auth_routes.py), replace lines 140-143:

```python
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(db, user, "SIGNUP", "User", str(user.id))
```

with:

```python
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(db, user, "SIGNUP", "User", str(user.id))

    from services.token_wallet import ensure_wallet
    ensure_wallet(db, user)
```

This creates the wallet (and grants the 2,000,000-token trial per Task 0's registration) at signup time rather than lazily on first AI use — matching the old system's behavior of tier-based credits being available immediately. It's called after `log_action` deliberately, so a JTS outage at signup time can't prevent the account itself from being created (a wallet-less user just gets `ensure_wallet`'d lazily on first `check_sufficient`/`balance_tokens` call instead).

- [ ] **Step 2: Commit**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier
git add routes/auth_routes.py
git commit -m "Create JTS wallet at signup"
```

---

### Task 10: Billing page — replace top-up UI with JTS widget

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/templates/billing.html:35-95`
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/stripe_routes.py:110-127` (`billing_page`) — the credits checkout route + topup webhook handler are removed in Steps 4-6 below
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/web_routes.py:129-140` (`dashboard` — `credit_balance` also renders on the dashboard stat tile, not just `/billing`)
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/routes/web_routes.py:254-257` (`studio` — same balance shown in the studio header)
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/templates/dashboard.html:44`
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/templates/studio.html:12`

`credit_balance` is computed via `credits_balance(db, user)` in three places, not just the billing route — all three need to switch to the JTS balance.

- [ ] **Step 1: Update `billing_page` (routes/stripe_routes.py:110-127)**

Replace lines 119-127:

```python
    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "tiers": tiers_view,
        "stripe_configured": bool(_stripe_secret()),
        "has_subscription": bool(user.stripe_customer_id),
        "credit_balance": credits_balance(db, user),
        "credit_history": credits_history(db, user, limit=20),
        "topup_packs": list_topup_packs(db),
    })
```

with:

```python
    from services.token_wallet import balance_tokens
    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "tiers": tiers_view,
        "stripe_configured": bool(_stripe_secret()),
        "has_subscription": bool(user.stripe_customer_id),
        "token_balance_display": balance_tokens(db, user) // 1000,
        "token_service_url": get_env("TOKEN_SERVICE_URL", "https://jhome-token-service-production.up.railway.app"),
        "token_service_app_slug": "gootier",
    })
```

- [ ] **Step 2: Update the dashboard render (routes/web_routes.py:129-140)**

Replace line 129 (`credit_balance = credits_balance(db, user)`) with:

```python
    from services.token_wallet import balance_tokens
    credit_balance = balance_tokens(db, user) // 1000
```

(The variable name `credit_balance` is kept as the template context key here to minimize the diff — it now holds a token-display value, not a legacy credit count. Update `templates/dashboard.html:44`'s neighboring label text — check the surrounding markup for a "credits" caption near the `stat-value` div and change it to "tokens".)

- [ ] **Step 3: Update the studio page render (routes/web_routes.py:254-257)**

Replace line 255:

```python
        user, clips=clips, credit_balance=credits_balance(db, user),
```

with:

```python
        user, clips=clips, credit_balance=balance_tokens(db, user) // 1000,
```

(add `from services.token_wallet import balance_tokens` near the top of this function if not already imported by Step 2's edit in the same file). Update `templates/studio.html:12` — replace `{{ credit_balance|default(0) }} credits` with `{{ credit_balance|default(0) }} tokens`.

- [ ] **Step 4: Remove the now-dead `services.credits` imports in `web_routes.py`**

Remove line 13 (`from services.credits import balance as credits_balance`) and the redundant local import at line 243 (`from services.credits import balance as credits_balance`, inside the studio route function) — both are superseded by the `services.token_wallet.balance_tokens` imports added in Steps 2-3.

- [ ] **Step 5: Replace the billing.html template section**

Replace `templates/billing.html` lines 35-95 (the "Credit balance" card + top-up pack grid + "Recent credit activity" table) with:

```html
<div class="card-x mb-4">
  <div class="card-x-header">
    <h6><i class="fas fa-coins me-2 text-muted"></i>Token balance</h6>
    <span class="ms-auto small text-muted">Used for image, video, and AI generation</span>
  </div>
  <div class="card-x-body">
    <div class="display-5 fw-bold mb-3">{{ token_balance_display }} <span class="small-meta">tokens</span></div>
    <div id="jhome-tokens"></div>
    <script src="{{ token_service_url }}/widget.js" data-app="{{ token_service_app_slug }}"></script>
    <script>
      window.onJhomeTokenBuy = async function (bundleName) {
        const resp = await TJ.api.post('/api/billing/tokens/checkout', { bundle_name: bundleName });
        if (resp && resp.checkout_url) {
          window.location.href = resp.checkout_url;
        }
      };
    </script>
  </div>
</div>
```

- [ ] **Step 6: Add the checkout-redirect backend route**

In `routes/stripe_routes.py`, replace the existing `/api/billing/credits/checkout` route (lines ~230-284, `create_credits_checkout` + its `pack_key`/`get_topup_pack` usage) with a route that proxies to JTS's own `/checkout`:

```python
@router.post("/api/billing/tokens/checkout")
async def create_tokens_checkout(
    payload: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from services.token_wallet import ensure_wallet
    wallet_id = ensure_wallet(db, user)
    bundle_name = payload.get("bundle_name")
    if not bundle_name:
        raise HTTPException(status_code=400, detail="bundle_name is required")

    import httpx
    from services.env_config import get_env
    base_url = get_env("TOKEN_SERVICE_URL", "https://jhome-token-service-production.up.railway.app")
    resp = httpx.post(
        f"{base_url}/checkout",
        headers={"X-API-Key": get_env("TOKEN_SERVICE_API_KEY", "")},
        json={
            "wallet_id": wallet_id,
            "bundle_name": bundle_name,
            "success_url": f"{_app_url()}/billing?credits_added=1",
            "cancel_url": f"{_app_url()}/billing?credits_cancelled=1",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not start checkout")
    return {"checkout_url": resp.json()["checkout_url"]}
```

- [ ] **Step 7: Remove the old topup webhook handler**

In `routes/stripe_routes.py`, remove `_handle_topup_completed` and its dispatch from `stripe_webhook`'s `checkout.session.completed` branch (the `if`/`else` that currently routes between `_handle_topup_completed` and `_handle_checkout_completed` — keep only `_handle_checkout_completed`, since tier-subscription checkout is unaffected). JTS's own webhook now handles token-purchase credits entirely out of band.

- [ ] **Step 8: Remove now-dead imports** at the top of `stripe_routes.py`:

```python
    balance as credits_balance, get_topup_pack, grant as credits_grant,
    list_topup_packs, recent_history as credits_history,
```

- [ ] **Step 9: Manual verification** (this is a UI/checkout-redirect change with no existing route-test harness — see plan header scoping note)

Start the app locally, log in, visit `/billing`, `/dashboard`, and `/studio`, confirm:
- The token balance renders on all three pages (should show the trial-grant display value for a fresh signup)
- The JTS widget script loads on `/billing` without a console error
- Clicking a bundle redirects to a Stripe checkout URL

- [ ] **Step 10: Commit**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier
git add templates/billing.html templates/dashboard.html templates/studio.html routes/stripe_routes.py routes/web_routes.py
git commit -m "Replace top-up pack UI with JTS widget + checkout proxy"
```

---

### Task 11: Remove the old credit-ledger system

**Files:**
- Delete: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/services/credits.py`
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/models.py` (`CreditLedger`, `TopupPackConfig`, `TierConfig.monthly_credit_grant`, `_seed_topup_packs`, `init_db`)

Per the approved design: pre-launch, no live balances, so this is a clean removal. The `credit_ledger` / `topup_pack_configs` tables and `tier_configs.monthly_credit_grant` column are left in place in the database (Gootier's migration helpers are additive-only — `_safe_add_column`/`create_all`, no drop-column/drop-table helper exists in this codebase, and adding one is unnecessary risk for zero rows of real data). Only the Python-level classes and code paths are removed.

- [ ] **Step 1: Confirm nothing outside `services/credits.py` still imports it**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier
grep -rln "from services.credits import\|services\.credits\." --include="*.py" .
```
Expected: no matches (Tasks 4-10 should have removed every call site — if this shows a hit, resolve it before continuing).

- [ ] **Step 2: Delete the file**

```bash
git rm services/credits.py
```

- [ ] **Step 3: Remove `_seed_topup_packs` and its `init_db()` call**

In `models.py`, remove the `_seed_topup_packs(db)` line from `init_db()` (line 679) and delete the `_seed_topup_packs` function definition.

- [ ] **Step 4: Remove the `TopupPackConfig` and `CreditLedger` classes**

Delete the `TopupPackConfig` class (models.py:122-138) and the `CreditLedger` class (models.py:292-304) from `models.py`. Leave `MediaJob.cost_credits` as-is (still populated as `0`/unused going forward, harmless legacy column, not worth a migration for).

- [ ] **Step 5: Remove `monthly_credit_grant` from the tier-display editor**

```bash
grep -rn "monthly_credit_grant" --include="*.py" --include="*.html" .
```

Remove any admin-panel form field / display for `monthly_credit_grant` found (the column itself stays in the DB per the note above — just stop reading/writing it from Python and templates). Leave `_upgrade_tier_configs`'s `_safe_add_column(conn, "tier_configs", "monthly_credit_grant", "INTEGER")` line alone (it's a no-op for existing installs and harmless to keep).

- [ ] **Step 6: Run the full test suite**

```bash
pytest -v
```
Expected: all tests pass, no import errors from the deleted module.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Remove local credit-ledger system, superseded by JTS"
```

---

### Task 12: STATUS.md + central credentials file

**Files:**
- Modify: `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/STATUS.md` (create if absent)
- Already updated in Task 0: `/Users/jaymevsmith/Documents/Claude/env-template.md`

- [ ] **Step 1: Check for an existing STATUS.md**

```bash
cat /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier/STATUS.md 2>/dev/null || echo "none"
```

- [ ] **Step 2: Add/update the `connects_to` frontmatter field**

Add or merge into the existing frontmatter:

```yaml
connects_to:
  - to: Jhome-Token-Service
    via: "Token wallet/debit API"
```

Update `milestone`/`progress` to reflect that the JTS migration is in progress or complete, per whatever else is already in the file — leave unrelated fields untouched.

- [ ] **Step 3: Commit**

```bash
git add STATUS.md
git commit -m "Note Jhome Token Service connection in STATUS.md"
```

---

### Task 13: End-to-end manual verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite one more time**

```bash
cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier
pytest -v
```
Expected: all green.

- [ ] **Step 2: Verify wallet creation against the real JTS deployment**

With `TOKEN_SERVICE_API_KEY`/`TOKEN_SERVICE_URL` set in `.env` (Task 0), sign up a fresh test user locally and confirm in JTS's admin console (`/admin/wallets/{id}`) that a wallet was created for `gootier` with the 2,000,000-token trial grant.

- [ ] **Step 3: Verify a real debit end-to-end**

Generate one real image (cheapest model, `nano-banana-2`) as that test user. Confirm:
- The job completes and the image renders
- The JTS admin wallet view shows a new ledger entry: `model_key=fal-nano-banana-2`, real `tokens_charged` matching `0.08 usd × 1,000,000 = 80,000` tokens (up to fal's own rounding)
- `/billing` in Gootier shows the reduced balance

- [ ] **Step 4: Verify the failure path takes no charge**

Temporarily point `FAL_KEY` (via `/admin/env`) to an invalid value, attempt one image generation, confirm it fails and the JTS wallet balance is unchanged (no debit call fired). Restore the real `FAL_KEY` afterward.

- [ ] **Step 5: Verify the purchase flow**

From `/billing`, click a token bundle, complete a real (or Stripe test-mode) checkout, confirm the balance increases and the JTS webhook fired.
