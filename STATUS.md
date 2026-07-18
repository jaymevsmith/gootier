---
phase: testing
milestone: "Jhome Token Service migration implemented on branch worktree-gootier-jts-migration, pending merge + live verification"
progress: 85
tags: [fastapi, sqlalchemy, stripe, fal-ai, anthropic]
connects_to:
  - to: Jhome-Token-Service
    via: "Token wallet/debit API"
---

Social-media scheduling/publishing SaaS (FB/IG/LinkedIn/YouTube/TikTok). Jhome Token Service migration (design + plan in `docs/superpowers/specs/` and `docs/superpowers/plans/`) fully implemented via subagent-driven-development: all 5 job-type billing paths (image/video/compose/music/AI-plan) migrated to debit-after-success against JTS, wallet creation at signup, billing/dashboard/studio/assets/ai-builder UI updated to show real JTS balances, old local credit-ledger system removed. App registered in JTS as `gootier` (id 6, accent `#5b6ee1`, 2M token trial), 9 fal.ai rates + reused `anthropic-sonnet-5` registered. 50/50 tests passing. Remaining: merge to main, then live end-to-end verification against the real JTS deployment (real signup → wallet, real debit, real purchase flow).
