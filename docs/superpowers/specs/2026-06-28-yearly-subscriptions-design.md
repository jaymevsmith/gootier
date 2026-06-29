# Yearly subscriptions + billing-period toggle

**Date:** 2026-06-28
**Status:** Approved, ready for implementation plan

## Goal

Offer a **yearly** subscription option for every paid tier, priced at **10× the
monthly price** (i.e. two months free versus paying month-to-month). Add a
**Monthly / Yearly toggle** to the `/billing` page so users choose the period
before checkout. Admins manage the yearly Stripe Price ID per tier on
`/admin/plans`.

## Decisions (locked)

- **Yearly price provisioning:** Admin creates a real recurring yearly Price in
  Stripe (at 10× the monthly amount) and pastes its Price ID into a new
  `yearly_stripe_price_id` field on `/admin/plans`. Mirrors the existing monthly
  Price ID pattern. No legacy env fallback for yearly (the feature is new).
- **Yearly display price:** Computed as `monthly_price_cents × 10`. Not stored —
  always tracks the monthly price, nothing to keep in sync.
- **Default toggle state:** Monthly. Users opt into yearly.
- **Multiplier:** Fixed at 10× for all tiers (no per-tier override).

## Data model — `models.py` `TierConfig`

Add one column via the existing `_safe_add_column` migration path (Postgres
`ADD COLUMN IF NOT EXISTS`, SQLite-safe):

| Column                   | Type   | Notes                                  |
|--------------------------|--------|----------------------------------------|
| `yearly_stripe_price_id` | String | nullable; admin-entered Stripe Price ID |

The yearly display price is **not** a column — it is derived at render time as
`monthly_price_cents * 10`.

## Checkout — `routes/stripe_routes.py`

- `_stripe_price_id_for_tier(db, tier_key, period="monthly")` gains a `period`
  argument (`"monthly"` | `"yearly"`).
  - `period == "yearly"` → return `row.yearly_stripe_price_id` (or `None`); no
    env fallback.
  - `period == "monthly"` → unchanged (DB column then `STRIPE_PRICE_*` env).
- `/api/billing/checkout` reads `period` from the request body, defaults to
  `"monthly"`, validates it is one of the two allowed values, and resolves the
  price ID for `(tier, period)`. Returns 400 if the resolved price ID is missing.
  - `mode="subscription"` is unchanged.
  - `metadata` and `subscription_data["metadata"]` carry `period` alongside the
    existing `user_id` / `tier`.
- `_load_billing_tiers(db)` adds to each tier dict:
  - `yearly_price_id` — the configured yearly Price ID or `None`.
  - `yearly_configured` — `bool(yearly_price_id)`.
  - `yearly_price_cents` — computed `monthly_price_cents * 10` (0 if monthly
    price unset).
- `_price_id_to_tier_key(db)` includes each tier's `yearly_stripe_price_id` in
  the reverse-lookup map so the webhook resolves a yearly subscription to the
  correct tier. Tier mapping is period-agnostic: a yearly Gold subscription maps
  to Gold, exactly like the monthly Gold price does.

No changes needed to webhook handlers beyond the reverse-map: tier upgrade and
cancellation logic is the same regardless of billing period.

## UI — `templates/billing.html`

- Add a **Monthly / Yearly pill toggle** above the `.tier-grid`, defaulting to
  Monthly. Style it to match the design system (segmented pill, active segment
  uses the indigo/purple accent).
- Each tier card renders **both** prices in the DOM:
  - Monthly: `${{ monthly }}/month`
  - Yearly: `${{ yearly }}/year` plus a small "2 months free" tag.
  - JS shows the price matching the current toggle and hides the other; flips the
    suffix accordingly.
- The upgrade button passes the current toggle state as `period` to the checkout
  POST.
- Yearly-not-configured handling: when the toggle is on Yearly and a tier has no
  `yearly_price_id`, its upgrade button is disabled with a "Yearly not
  configured" hint — mirroring the existing monthly `not configured` treatment.
  Switching back to Monthly re-enables it (if monthly is configured).
- The toggle state lives in client JS only; no persistence needed.

## Admin — `routes/admin_routes.py` + `templates/admin_plans.html`

- Add a **"Yearly Stripe Price ID"** text input per tier, next to the existing
  monthly Price ID field.
- Persist it through the existing plans-save handler (same form round-trip that
  saves monthly Price ID, display name, etc.).
- Show the auto-computed 10× yearly price as read-only helper text beside the
  input (e.g. "Displays as $90.00/year (10× monthly)").

## Out of scope (YAGNI)

- Proration UI for switching period on an existing subscription — users do this
  through Stripe's customer portal via the existing "Manage subscription" button.
- In-app period switching for current subscribers.
- Per-tier custom yearly multipliers (fixed at 10×).
- Yearly env-var fallback (`STRIPE_PRICE_*_YEARLY`) — yearly is DB-only.

## Testing

- `_stripe_price_id_for_tier` returns the yearly ID for `period="yearly"` and the
  monthly ID/env for `period="monthly"`; returns `None` when the requested
  period is unconfigured.
- `/api/billing/checkout` rejects an invalid `period`, returns 400 for an
  unconfigured `(tier, period)`, and uses the correct price ID per period.
- `_price_id_to_tier_key` maps both monthly and yearly price IDs to the right
  tier.
- `_load_billing_tiers` computes `yearly_price_cents = monthly * 10` and sets
  `yearly_configured` correctly.
- Template: toggle defaults to Monthly; yearly button disabled when unconfigured.
