---
phase: live
milestone: "Backoffice SSO handoff LIVE; balance surfaces hardened against a Token Service outage and switched to compact K/M display (PRs #9, #11 merged + deployed 2026-09-03)"
progress: 98
tags: [fastapi, sqlalchemy, stripe, fal-ai, anthropic]
connects_to:
  - to: Jhome-Token-Service
    via: "Token wallet/debit API; wallets now grouped into the shared Jhome balance via customer_ref when jhome_sub is set"
  - to: jhome-backoffice
    via: "SSO handoff (POST /internal/handoff + GET /sso/consume), person-shaped (resolved by email, not domain)"
---

Social-media scheduling/publishing SaaS (FB/IG/LinkedIn/YouTube/TikTok). Jhome Token Service migration (design + plan in `docs/superpowers/specs/` and `docs/superpowers/plans/`) fully implemented via subagent-driven-development: all 5 job-type billing paths (image/video/compose/music/AI-plan) migrated to debit-after-success against JTS, wallet creation at signup, billing/dashboard/studio/assets/ai-builder UI updated to show real JTS balances, old local credit-ledger system removed. App registered in JTS as `gootier` (id 6, accent `#5b6ee1`, 2M token trial), 9 fal.ai rates + reused `anthropic-sonnet-5` registered. 177 passing (the 2 long-standing `test_affiliates_integration.py` failures were fixed 2026-09-02 — a test-isolation gap, not a signup bug — followed by three rounds hardening the config-DB read path; see HANDOFF.md). Live-verified end-to-end against the real production JTS deployment 2026-07-18: signup → wallet created (2,000 token trial), real image generation → correct debit (2,000 → 1,940), a forced fal-auth failure → no charge, and a real Stripe checkout session created via the token-purchase proxy (not completed, to avoid spending real money). Remaining: land the config-DB hardening branch (`claude/serene-bhaskara-94d80c`, merged up to date with origin/main, 177 passing) on main and deploy.
