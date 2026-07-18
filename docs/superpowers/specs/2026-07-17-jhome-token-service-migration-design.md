# Gootier → Jhome Token Service migration

Date: 2026-07-17

## Summary

Replace Gootier's homegrown AI/media credit ledger (`services/credits.py`) with the shared Jhome Token Service (JTS), following the same integration pattern already used by Lumify, AI Website Builder, Studio-wan, and Site Agent. Stripe subscription tiers (Trial/Bronze/Silver/Gold) and their post/blast/connection quota gating (`services/quotas.py`) are unrelated to this migration and stay as they are.

Gootier is pre-launch with no real paying customers or live credit balances, so this is a clean cutover — no balance-migration step is needed.

## Non-goals

- No change to tier subscriptions, Stripe subscription checkout/portal, or `services/quotas.py` count-based quotas (`posts_per_month`, `blasts_per_month`, `ai_generations_per_month`, `social_connections`, `blast_recipients`).
- No metering added to the Anthropic campaign text-draft call (`services/ai_generator.py`) — it stays governed only by the existing `ai_generations_per_month` quota, same as today. (Confirmed explicitly — this was the one open question during design.)
- No JTS core feature work. JTS's existing app-facing surface (wallet get-or-create, balance, debit with `request_id` idempotency, catalog/widget/checkout) is sufficient as-is.

## What moves where

| Concern | Today | After |
|---|---|---|
| Tier subscriptions, quotas | Gootier's own Stripe + `quotas.py` | Unchanged |
| AI/media credit balance | Local `CreditLedger` (SUM of ledger deltas) | JTS wallet, one per user, keyed by `str(user.id)` (same convention as Site Agent) |
| Monthly bonus credits (`_ensure_monthly_grant`, `TierConfig.monthly_credit_grant`) | Auto-inserted lazily per calendar month | **Removed entirely.** Tiers no longer bundle AI credits — they gate feature quotas only. |
| Buying more credits | Gootier's own `TopupPackConfig` picker + `/api/billing/credits/checkout` Stripe flow + `_handle_topup_completed` webhook | Removed. Replaced by JTS's `/widget.js` (or `/embed/pricing`) embedded on Gootier's `/billing` page — JTS's own bundles, Stripe checkout, and webhook credit the wallet directly. |
| New user's starter credits | N/A (tier grant covered this) | JTS one-time `trial_tokens` grant at wallet creation. Proposed: 2,000,000 (matches Site Agent/Studio-wan registration) — adjustable in JTS admin at registration time. |
| Spend accounting | Flat "credits" (5–400) per `MEDIA_MODEL_CATALOG` entry, spent **before** submitting to fal, refunded via `credits_grant` if fal call fails | JTS `POST /wallets/{id}/debit` with **real per-model USD cost**, called **after** the fal job completes successfully (see "Debit timing" below) |

## Rate registration

New JTS `Rate` rows, one per Gootier model currently in `MEDIA_MODEL_CATALOG` / `TTS_MODEL_CATALOG`, priced at the **real underlying cost** (fal's actual per-call or per-compute-second cost, not Gootier's marked-up retail credit price). This matches how every other JTS-integrated app was set up — margin lives entirely in JTS bundle pricing (bundle USD price vs. tokens granted), not in the per-model rate.

- Image models: `fal-nano-banana-2`, `fal-nano-banana-pro`, `fal-flux-pro-ultra` (kind=unit, per image)
- Video models: `fal-kling-2.1-master` and other video catalog entries (kind=unit, per video, or per-second if fal bills that way for a given model)
- TTS/narration models used by `services/video_composer.py`
- Music generation: reuse `fal-cassetteai-music` (already registered for Studio-wan) if Gootier uses the same underlying fal model; otherwise register a Gootier-specific rate.

Exact fal endpoint → real-cost mapping to be pulled from fal's pricing docs during implementation (mirrors how Studio-wan's `fal-cassetteai-music` rate was derived from fal's actual per-compute-second cost).

## Purchase flow

Gootier's `/billing` page keeps its tier subscription card exactly as today (Stripe subscription checkout/portal, unchanged). The credits section is replaced:

- Remove: pack picker, `/api/billing/credits/checkout`, `_handle_topup_completed`, `TopupPackConfig` usage in this flow.
- Add: JTS `/widget.js` embedded inline (per-app accent, `postMessage` buy flow via `window.onJhomeTokenBuy`), or a link to `/embed/pricing`, per the standard JTS integration pattern documented in JTS's `INTEGRATION.md`.
- Balance display: always render JTS's `_display` field (never the raw token count), per JTS's display standard.

## Debit timing (behavior change)

JTS's app-facing API is debit-only — there is no refund/credit-back endpoint for apps (only Stripe-webhook credits and admin manual credit). Gootier's current code spends *before* calling fal and refunds on failure, which doesn't map cleanly onto that.

New flow: keep creating the `MediaJob` row as `queued` and doing a **soft pre-flight check** (`GET /balance` — enough to cover the job's real cost) purely for UX, so users aren't allowed to queue jobs they obviously can't afford. Then submit to fal. On the job's webhook-reported **success**, call `POST /debit` with the real cost and a `request_id` derived from the job (e.g. `gootier-media-{job.id}`) for idempotency against webhook redelivery. On failure, **no debit happens** — nothing to refund, since nothing was ever charged.

This removes `credits_grant`-as-refund entirely and is consistent with the "debit real usage after the call resolves" pattern JTS was designed around.

Known tradeoff (accepted, same as other JTS apps): the pre-flight check is not an atomic reservation, so a burst of concurrent job submissions from the same user could transiently queue more than their balance covers before the first debit lands. Not addressed here; matches existing practice elsewhere in the JTS ecosystem.

## Data model changes

- `models.py`: `CreditLedger`, `TierConfig.monthly_credit_grant`, `TopupPackConfig` become unused by the new flow. Decide at implementation time whether to drop the tables/columns outright (pre-launch, no data to preserve) or leave them and just stop writing to them — dropping is cleaner given there's no live data.
- `services/credits.py`: replaced by a new thin wrapper (e.g. `services/token_wallet.py`) around JTS's HTTP API (get-or-create wallet, get balance, debit), mirroring the client pattern used in Lumify/Site Agent's integrations.
- New env vars: `GOOTIER_TOKEN_SERVICE_API_KEY` (registered in the central credentials file), `JHOME_TOKEN_SERVICE_URL`.

## Testing

- Unit tests for the new `token_wallet` wrapper (mock JTS HTTP calls): wallet get-or-create, balance fetch, debit success, debit 402 insufficient-balance surfaces the same user-facing "top up" error Gootier shows today.
- Integration test for the media-job success/failure paths: success → exactly one debit call with the right `request_id`; failure → zero debit calls.
- Remove/replace existing `credits.py` tests that no longer apply; confirm `quotas.py` tests are unaffected (they don't touch credits).
- Manual verification against the real JTS Railway deployment for wallet creation + a real debit, same as Studio-wan/Site Agent's live-verification step.

## App registration (JTS side)

Register Gootier as a new JTS app: slug `gootier`, accent color `#5b6ee1` (Gootier's own primary indigo, per `static/css/colors_and_type.css` — distinct from existing apps' accents: Lumify `#e91e63`, Studio-wan `#f0b454`, Site Agent `#f0a132`), `trial_tokens` 2,000,000. Add the new Rate rows described above. Log the issued API key into the central credentials file as `GOOTIER_TOKEN_SERVICE_API_KEY`.
