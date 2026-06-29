# Yearly Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a yearly subscription option (priced at 10× the monthly price) for every paid tier, with a Monthly/Yearly toggle on the `/billing` page and an admin-managed yearly Stripe Price ID per tier.

**Architecture:** One new nullable column `yearly_stripe_price_id` on `TierConfig`. Checkout helpers gain a `period` argument; the billing page renders both prices and a JS pill toggle picks which one to show and which price ID to send to checkout. The Stripe webhook's price-ID→tier reverse-map is extended to include yearly IDs so yearly subscriptions resolve to the right tier. Yearly display price is computed (`monthly × 10`), never stored.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Jinja2, vanilla JS (TJ.api helpers), Stripe Checkout, pytest (newly added for the helper unit tests).

---

## File Structure

- `models.py` — add `yearly_stripe_price_id` column to `TierConfig`; add a migration line in `_upgrade_tier_configs`.
- `routes/stripe_routes.py` — `period`-aware price lookup, yearly fields in the billing-tiers view, yearly IDs in the webhook reverse-map, `period` handling in the checkout route.
- `routes/admin_routes.py` — `TierUpdate.yearly_stripe_price_id`, persist it in `update_tier`, expose it in `_serialize_tier`.
- `templates/admin_plans.html` — yearly Price ID input + helper text; include it in the save payload.
- `templates/billing.html` — Monthly/Yearly pill toggle, dual price rendering per card, toggle JS, pass `period` to checkout.
- `requirements.txt` — add `pytest` (dev/test dependency).
- `tests/conftest.py` (new) — in-memory SQLite session fixture.
- `tests/test_billing_period.py` (new) — unit tests for the period-aware helpers.

All paths below are relative to `/Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier`.

---

## Task 1: Add the `yearly_stripe_price_id` column + migration

**Files:**
- Modify: `models.py` (class `TierConfig`, ~line 92; function `_upgrade_tier_configs`, ~line 454)

- [ ] **Step 1: Add the column to `TierConfig`**

In `models.py`, immediately after the existing `stripe_price_id` line in `class TierConfig`:

```python
    stripe_price_id = Column(String, nullable=True)         # "price_..."
    yearly_stripe_price_id = Column(String, nullable=True)  # "price_..." (yearly interval; display = monthly_price_cents * 10)
```

- [ ] **Step 2: Add the migration line**

In `_upgrade_tier_configs`, after the `stripe_price_id` migration line:

```python
    _safe_add_column(conn, "tier_configs", "stripe_price_id",        "VARCHAR")
    _safe_add_column(conn, "tier_configs", "yearly_stripe_price_id", "VARCHAR")
```

- [ ] **Step 3: Verify the model imports and migration run cleanly**

Run: `python -c "import models; models.init_db(); print('ok')"`
Expected: prints `ok` with no traceback (migration is idempotent; safe to run against the existing `gootier.db`).

- [ ] **Step 4: Verify the column exists**

Run: `python -c "import sqlite3; c=sqlite3.connect('gootier.db'); print([r[1] for r in c.execute('PRAGMA table_info(tier_configs)')])"`
Expected: the printed list includes `yearly_stripe_price_id`.

- [ ] **Step 5: Commit**

```bash
git add models.py
git commit -m "feat(billing): add yearly_stripe_price_id column to TierConfig"
```

---

## Task 2: Period-aware checkout helpers (with tests)

This task adds pytest and tests the three pure-ish helpers in `routes/stripe_routes.py`.

**Files:**
- Modify: `requirements.txt`
- Create: `tests/conftest.py`
- Create: `tests/test_billing_period.py`
- Modify: `routes/stripe_routes.py` (`_stripe_price_id_for_tier` ~line 41, `_load_billing_tiers` ~line 55, `_price_id_to_tier_key` ~line 81)

- [ ] **Step 1: Add pytest to requirements**

Append to `requirements.txt`:

```
pytest
```

- [ ] **Step 2: Install pytest**

Run: `pip install pytest`
Expected: pytest installs successfully (or "Requirement already satisfied").

- [ ] **Step 3: Create the test session fixture**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures: an isolated in-memory SQLite session."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
import models  # noqa: F401 — ensures all models register on Base.metadata


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
```

- [ ] **Step 4: Write the failing tests**

Create `tests/test_billing_period.py`:

```python
"""Unit tests for period-aware billing helpers in routes/stripe_routes.py."""
from models import TierConfig
from routes import stripe_routes as sr


def _make_tier(db, tier, monthly_cents, monthly_pid=None, yearly_pid=None):
    row = TierConfig(
        tier=tier,
        monthly_price_cents=monthly_cents,
        stripe_price_id=monthly_pid,
        yearly_stripe_price_id=yearly_pid,
        is_active=True,
        sort_order=0,
    )
    db.add(row)
    db.commit()
    return row


def test_price_id_monthly_returns_monthly(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid="price_y_bronze")
    assert sr._stripe_price_id_for_tier(db, "bronze", "monthly") == "price_m_bronze"


def test_price_id_yearly_returns_yearly(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid="price_y_bronze")
    assert sr._stripe_price_id_for_tier(db, "bronze", "yearly") == "price_y_bronze"


def test_price_id_yearly_unconfigured_returns_none(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid=None)
    assert sr._stripe_price_id_for_tier(db, "bronze", "yearly") is None


def test_load_billing_tiers_computes_yearly(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid="price_y_bronze")
    tiers = sr._load_billing_tiers(db)
    assert len(tiers) == 1
    t = tiers[0]
    assert t["yearly_price_cents"] == 9000          # 900 * 10
    assert t["yearly_price_id"] == "price_y_bronze"
    assert t["yearly_configured"] is True


def test_load_billing_tiers_yearly_unconfigured(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid=None)
    t = sr._load_billing_tiers(db)[0]
    assert t["yearly_price_cents"] == 9000
    assert t["yearly_configured"] is False


def test_price_id_to_tier_key_includes_yearly(db):
    _make_tier(db, "gold", 4900, monthly_pid="price_m_gold", yearly_pid="price_y_gold")
    rev = sr._price_id_to_tier_key(db)
    assert rev["price_m_gold"] == "gold"
    assert rev["price_y_gold"] == "gold"
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `python -m pytest tests/test_billing_period.py -v`
Expected: tests for yearly behavior FAIL (e.g. `TypeError` — `_stripe_price_id_for_tier()` takes 2 positional args, or `KeyError: 'yearly_price_cents'`). The two monthly-ID assertions may pass.

- [ ] **Step 6: Implement `period` in `_stripe_price_id_for_tier`**

In `routes/stripe_routes.py`, replace the `_stripe_price_id_for_tier` function:

```python
def _stripe_price_id_for_tier(db: Session, tier_key: str, period: str = "monthly") -> Optional[str]:
    """Look up a tier's Stripe Price ID for the given billing period.

    Monthly reads `tier_configs.stripe_price_id` first (admin-editable on
    /admin/plans), then falls back to the legacy `STRIPE_PRICE_BRONZE/SILVER/GOLD`
    env keys so older installs keep working.  Yearly reads
    `tier_configs.yearly_stripe_price_id` only — no env fallback, the feature is new.
    """
    row = db.query(TierConfig).filter(TierConfig.tier == tier_key).first()
    if period == "yearly":
        return (row.yearly_stripe_price_id or None) if row else None
    if row and row.stripe_price_id:
        return row.stripe_price_id
    legacy = get_env(f"STRIPE_PRICE_{tier_key.upper()}", "")
    return legacy or None
```

- [ ] **Step 7: Implement yearly fields in `_load_billing_tiers`**

In `_load_billing_tiers`, replace the `out.append({...})` block with:

```python
        monthly_cents = r.monthly_price_cents or 0
        out.append({
            "key": r.tier,
            "name": r.display_name or r.tier.title(),
            "blurb": r.blurb or "",
            "monthly_price_cents": monthly_cents,
            "yearly_price_cents": monthly_cents * 10,
            "features": r.features_list(),
            "price_id": price_id,
            "price_id_env": legacy_env,   # kept so the disabled-button tooltip can still hint
            "configured": bool(price_id),
            "yearly_price_id": r.yearly_stripe_price_id or None,
            "yearly_configured": bool(r.yearly_stripe_price_id),
        })
```

- [ ] **Step 8: Implement yearly IDs in `_price_id_to_tier_key`**

Replace the loop body in `_price_id_to_tier_key`:

```python
def _price_id_to_tier_key(db: Session) -> dict:
    """Reverse lookup for the Stripe webhook: price_id -> tier_key.

    Includes both the monthly and yearly price IDs so a yearly subscription
    resolves to the same tier as its monthly counterpart.
    """
    out: dict = {}
    for r in db.query(TierConfig).filter(TierConfig.tier.in_(_BILLING_TIER_KEYS)).all():
        monthly_pid = r.stripe_price_id or get_env(f"STRIPE_PRICE_{r.tier.upper()}", "")
        if monthly_pid:
            out[monthly_pid] = r.tier
        if r.yearly_stripe_price_id:
            out[r.yearly_stripe_price_id] = r.tier
    return out
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `python -m pytest tests/test_billing_period.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 10: Commit**

```bash
git add requirements.txt tests/conftest.py tests/test_billing_period.py routes/stripe_routes.py
git commit -m "feat(billing): period-aware Stripe price lookup + yearly webhook mapping"
```

---

## Task 3: Accept `period` in the checkout route

**Files:**
- Modify: `routes/stripe_routes.py` (`create_checkout_session` ~line 115)

- [ ] **Step 1: Update the checkout route to read and validate `period`**

In `routes/stripe_routes.py`, replace the body of `create_checkout_session` from `body = await request.json()` through the `stripe.checkout.Session.create(...)` call:

```python
    _ensure_stripe_key()
    body = await request.json()
    tier = body.get("tier")
    period = body.get("period", "monthly")
    if period not in ("monthly", "yearly"):
        raise HTTPException(status_code=400, detail=f"Invalid billing period: {period!r}")
    price_id = _stripe_price_id_for_tier(db, tier, period) if tier in _BILLING_TIER_KEYS else None
    if not price_id:
        raise HTTPException(status_code=400, detail=f"Unknown or unconfigured {period} tier: {tier}")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer=user.stripe_customer_id or None,
            customer_email=None if user.stripe_customer_id else user.email,
            client_reference_id=str(user.id),
            metadata={"user_id": str(user.id), "tier": tier, "period": period},
            subscription_data={"metadata": {"user_id": str(user.id), "tier": tier, "period": period}},
            success_url=f"{_app_url()}/billing?upgraded=1",
            cancel_url=f"{_app_url()}/billing?cancelled=1",
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"url": session.url}
```

- [ ] **Step 2: Verify the app imports cleanly**

Run: `python -c "import routes.stripe_routes; print('ok')"`
Expected: prints `ok` with no traceback.

- [ ] **Step 3: Commit**

```bash
git add routes/stripe_routes.py
git commit -m "feat(billing): checkout accepts monthly|yearly period"
```

---

## Task 4: Admin — yearly Price ID field on /admin/plans

**Files:**
- Modify: `routes/admin_routes.py` (`TierUpdate` ~line 345, `update_tier` ~line 457, `_serialize_tier` ~line 380)
- Modify: `templates/admin_plans.html` (tier card inputs ~line 164, save payload ~line 226)

- [ ] **Step 1: Add the field to the `TierUpdate` payload model**

In `routes/admin_routes.py`, in `class TierUpdate`, after `stripe_price_id`:

```python
    stripe_price_id: Optional[str] = None
    yearly_stripe_price_id: Optional[str] = None
```

- [ ] **Step 2: Persist it in `update_tier`**

In `update_tier`, after the `stripe_price_id` block (right after `changed.append("stripe_price_id")`):

```python
    if payload.yearly_stripe_price_id is not None:
        row.yearly_stripe_price_id = payload.yearly_stripe_price_id.strip()[:120] or None
        changed.append("yearly_stripe_price_id")
```

- [ ] **Step 3: Expose it in `_serialize_tier`**

In `_serialize_tier`, after the `"stripe_price_id"` entry:

```python
        "stripe_price_id": t.stripe_price_id or "",
        "yearly_stripe_price_id": t.yearly_stripe_price_id or "",
        "yearly_price_display": f"${((t.monthly_price_cents or 0) * 10) / 100:.2f}",
```

- [ ] **Step 4: Add the input to the admin tier card template**

In `templates/admin_plans.html`, replace the existing Stripe Price ID block (the `<div class="col-md-4">` containing `data-field="stripe_price_id"`) with both the monthly and yearly inputs:

```html
        <div class="col-md-4">
          <label>Stripe Price ID (monthly)</label>
          <input type="text" class="form-control" data-field="stripe_price_id" value="${esc(t.stripe_price_id)}"
                 placeholder="price_1Q...">
        </div>
        <div class="col-md-4">
          <label>Stripe Price ID (yearly)</label>
          <input type="text" class="form-control" data-field="yearly_stripe_price_id" value="${esc(t.yearly_stripe_price_id)}"
                 placeholder="price_1Q...">
          <div class="form-text small text-muted">Displays as ${t.yearly_price_display}/year (10× monthly)</div>
        </div>
```

Note: the `Order` and `Active` columns that follow remain unchanged — they will wrap to the next grid row, which is fine.

- [ ] **Step 5: Include the field in the save payload**

In `templates/admin_plans.html`, in the `wireTierCard` save `payload` object, after the `stripe_price_id` line:

```javascript
      stripe_price_id: get('stripe_price_id').value,
      yearly_stripe_price_id: get('yearly_stripe_price_id').value,
```

- [ ] **Step 6: Verify imports clean**

Run: `python -c "import routes.admin_routes; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Manual verification of the admin field**

Run the app: `python -m uvicorn main:app --port 8002` (or use the existing `gootier-local` container), open `http://localhost:8002/admin/plans`, confirm each tier card now shows a "Stripe Price ID (yearly)" input with the "Displays as $.../year (10× monthly)" helper text, enter a test value, click Save, reload, and confirm it persisted.
Expected: value saves and reloads correctly. Stop the server when done.

- [ ] **Step 8: Commit**

```bash
git add routes/admin_routes.py templates/admin_plans.html
git commit -m "feat(admin): yearly Stripe Price ID field on /admin/plans"
```

---

## Task 5: Billing page — Monthly/Yearly toggle + dual prices

**Files:**
- Modify: `templates/billing.html` (tier grid ~line 100-137, scripts ~line 144)

- [ ] **Step 1: Add the toggle markup above the tier grid**

In `templates/billing.html`, immediately before `<div class="tier-grid">`, insert:

```html
<div class="period-toggle" role="group" aria-label="Billing period" style="display:flex;justify-content:center;gap:.25rem;margin:0 auto 1.5rem;width:max-content;background:#f1f3f9;border-radius:999px;padding:.25rem;">
  <button type="button" class="period-opt is-active" data-period="monthly"
          style="border:0;background:transparent;border-radius:999px;padding:.4rem 1.1rem;font-weight:600;font-size:.9rem;cursor:pointer;color:#4a5568;">Monthly</button>
  <button type="button" class="period-opt" data-period="yearly"
          style="border:0;background:transparent;border-radius:999px;padding:.4rem 1.1rem;font-weight:600;font-size:.9rem;cursor:pointer;color:#4a5568;">
    Yearly <span class="hl hl-success" style="margin-left:.35rem;">2 months free</span>
  </button>
</div>
<style>
  .period-toggle .period-opt.is-active { background:#fff; color:#4f46e5; box-shadow:0 1px 3px rgba(0,0,0,.12); }
  .tier-card .price-yearly { display:none; }
  .billing-yearly .tier-card .price-monthly { display:none; }
  .billing-yearly .tier-card .price-yearly { display:block; }
</style>
```

- [ ] **Step 2: Render both prices in each tier card**

In `templates/billing.html`, replace the existing `<div class="tier-price">...</div>` block with:

```html
    <div class="tier-price">
      <span class="price-monthly">
        {% if t.monthly_price_cents %}
          ${{ '%g'|format(t.monthly_price_cents / 100) }}<small>/month</small>
        {% else %}
          <span class="text-muted small">price not set</span>
        {% endif %}
      </span>
      <span class="price-yearly">
        {% if t.yearly_configured %}
          ${{ '%g'|format(t.yearly_price_cents / 100) }}<small>/year</small>
          <div class="hl hl-success small">2 months free</div>
        {% else %}
          <span class="text-muted small">yearly not configured</span>
        {% endif %}
      </span>
    </div>
```

- [ ] **Step 3: Make the upgrade button period-aware**

In `templates/billing.html`, replace the upgrade `<button ...>` (the one with `data-upgrade="{{ t.key }}"`) with a version carrying yearly-config data attributes:

```html
    <button class="btn {% if t.key == 'silver' %}tj-grad-btn{% else %}btn-soft{% endif %} mt-auto"
            data-upgrade="{{ t.key }}"
            data-monthly-configured="{{ 1 if t.configured else 0 }}"
            data-yearly-configured="{{ 1 if t.yearly_configured else 0 }}"
            data-current="{{ 1 if t.current else 0 }}"
            {% if t.current or not t.configured or not stripe_configured %}disabled{% endif %}
            {% if not t.configured %}title="Set {{ t.price_id_env }} to enable"{% endif %}>
      {% if t.current %}Current plan
      {% elif not t.configured %}Not configured
      {% else %}Upgrade to {{ t.name }}{% endif %}
    </button>
```

- [ ] **Step 4: Wire the toggle JS and pass `period` to checkout**

In `templates/billing.html`, in the `{% block scripts %}` section, replace the existing `document.querySelectorAll('[data-upgrade]')...` block with:

```javascript
let billingPeriod = 'monthly';

function applyPeriodUI() {
  document.body.classList.toggle('billing-yearly', billingPeriod === 'yearly');
  document.querySelectorAll('.period-opt').forEach(b =>
    b.classList.toggle('is-active', b.dataset.period === billingPeriod));
  // Re-evaluate each upgrade button's enabled state for the active period.
  document.querySelectorAll('[data-upgrade]').forEach(btn => {
    if (btn.dataset.current === '1' || !{{ 'true' if stripe_configured else 'false' }}) {
      btn.disabled = true;
      return;
    }
    const ok = billingPeriod === 'yearly'
      ? btn.dataset.yearlyConfigured === '1'
      : btn.dataset.monthlyConfigured === '1';
    btn.disabled = !ok;
    btn.title = ok ? '' : (billingPeriod === 'yearly' ? 'Yearly not configured' : 'Monthly not configured');
  });
}

document.querySelectorAll('.period-opt').forEach(opt => {
  opt.addEventListener('click', () => { billingPeriod = opt.dataset.period; applyPeriodUI(); });
});
applyPeriodUI();

document.querySelectorAll('[data-upgrade]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const tier = btn.dataset.upgrade;
    const restore = TJ.btnLoading(btn, 'Redirecting…');
    const data = await TJ.api.post('/api/billing/checkout', { tier, period: billingPeriod },
      { progress: true, progressLabel: 'Creating checkout…' });
    restore();
    if (data && data.url) window.location.href = data.url;
  });
});
```

- [ ] **Step 5: Manual verification**

Run the app (`python -m uvicorn main:app --port 8002` or the `gootier-local` container) and open `http://localhost:8002/billing`. Verify:
- Toggle defaults to Monthly; cards show `/month` prices.
- Clicking Yearly switches all cards to `/year` prices (10× monthly) with a "2 months free" tag.
- A tier with no yearly Price ID configured shows "yearly not configured" and its upgrade button is disabled in Yearly mode but enabled in Monthly mode.
- Clicking an enabled upgrade button POSTs `period` (confirm via browser devtools Network tab: request body contains `"period":"monthly"` or `"yearly"` to match the toggle).
Expected: all of the above hold. Stop the server when done.

- [ ] **Step 6: Commit**

```bash
git add templates/billing.html
git commit -m "feat(billing): Monthly/Yearly toggle with dual pricing on /billing"
```

---

## Task 6: Full-flow sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the unit tests once more**

Run: `python -m pytest tests/ -v`
Expected: all tests in `tests/test_billing_period.py` PASS.

- [ ] **Step 2: Confirm the app boots**

Run: `python -c "from main import app; print('app ok')"`
Expected: prints `app ok` with no import/route errors.

- [ ] **Step 3: End-to-end confirmation (requires live Stripe test keys configured in /admin/env)**

With Stripe test mode keys set and a real yearly test Price created in Stripe and pasted into a tier on `/admin/plans`: on `/billing`, switch the toggle to Yearly, click Upgrade on that tier, and confirm Stripe Checkout opens showing the yearly price. (If Stripe test keys are not available in this environment, note that this step is deferred to a Stripe-test-mode environment.)
Expected: Stripe Checkout displays the yearly recurring price.

- [ ] **Step 4: Final commit (if any docs/notes updated)**

```bash
git status   # confirm clean working tree; nothing to commit if all prior tasks committed
```

---

## Notes for the implementer

- **Vendor-name rule:** No user-facing copy may say "Claude/Anthropic/Stripe model names." Stripe is a payment vendor and its name is fine in admin/config UI (Price ID labels) — that is infrastructure, not AI vendor copy. Do not introduce AI vendor names anywhere.
- **`get_env` is DB-first:** monthly legacy fallback reads `STRIPE_PRICE_*` via `get_env`, which checks the `EnvConfig` table then `os.environ`. The in-memory test DB has no `EnvConfig` rows, so the fallback resolves from `os.environ` (empty in tests) — that is expected and the yearly tests avoid that path entirely.
- **Migration safety:** `_safe_add_column` uses Postgres `ADD COLUMN IF NOT EXISTS` and is SQLite-safe; running `init_db()` against the existing `gootier.db` and against Railway Postgres is idempotent.
- **Deploy:** when deploying, `cd /Users/jaymevsmith/Documents/Claude/Projects/gootier-app/Gootier` first, then `railway up --ci` — cwd drift uploads the wrong project.
