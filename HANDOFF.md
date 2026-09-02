# Gootier — Handoff

## Backoffice connected-app SSO handoff — LIVE 2026-09-02

Gootier is now the Jhome Backoffice's 6th connected app: a signed-in Backoffice
customer clicks the "Gootier" tile and lands inside their Gootier account
already logged in, no second password. Built via subagent-driven-development
against a spec/plan living in the jhome-backoffice repo
(`docs/superpowers/specs/2026-09-01-gootier-connected-app-design.md`,
`docs/superpowers/plans/2026-09-01-gootier-connected-app.md`). Branch
`gootier-connected-app`, HEAD `1a5e210`, pushed — PR open against `main`, not
yet merged (deployed and verified live on the branch first, matching the
Cloud Storage precedent).

**What shipped, this repo's side:**
- `jhome_sub` column on `users` (nullable, unique, same convention as
  `google_sub`), `handoff_tokens` table (hash-only single-use tokens,
  2-minute TTL).
- `POST /internal/handoff` (`routes/internal_routes.py`) — resolves a
  customer by (case-insensitive) email, refuses admin-role and deactivated
  accounts, handles `jhome_sub` conflict/adoption for existing users,
  creates new users via the existing `oauth_routes._unique_username_from_email`
  helper (reused rather than reimplemented — see incident log below),
  links a Token Service wallet with `customer_ref=jhome_sub` (fail-open),
  mints the handoff token.
- `GET /sso/consume` (`routes/auth_routes.py`) — atomic single-use token
  burn (`UPDATE ... WHERE used_at IS NULL`, rowcount-checked), re-validates
  `is_active` AND admin-role at consume time (not just mint time, since
  state can change within the 2-minute TTL window), mints a real session.

**This went through unusually deep, adversarial review — real bugs caught
and fixed, worth remembering:**
1. **Privilege escalation** — the first draft had no admin-refusal check at
   all. Gootier's `/admin/env` can read/rotate every secret in the app,
   including `GOOTIER_INTERNAL_KEY` itself, so a handoff for an admin's
   email would have handed a holder of the shared Backoffice key full
   credential access. Fixed by mirroring RingBack's reference
   implementation, which explicitly refuses this case.
2. **Email-normalization mismatch** — the new endpoint's lookup normalized
   email (`.strip().lower()`), but Gootier's own signup stores email
   verbatim with no normalization at all. A mixed-case-stored user
   wouldn't be found, and would silently get a SECOND duplicate account
   once new-user creation shipped. Fixed with a case-insensitive query —
   which then needed its OWN fix (see #3).
3. **Crash on case-variant duplicates** — the case-insensitive fix above
   used `.one_or_none()`, which raises `MultipleResultsFound` (a 500) if
   two case-variant rows for one address already exist (confirmed
   reachable: signup allows it). Fixed by fetching all matches and
   explicitly refusing (409) on ambiguity instead of crashing.
4. **Username-generation bugs** — the first username-derivation helper had
   an off-by-one (the 20th collision candidate was computed but never
   actually tried, so 20 pre-existing colliding usernames produced a hard
   500) and generated usernames containing dots, which `auth.validate_username`
   itself rejects. Fixed by deleting the homegrown helper entirely and
   reusing `routes/oauth_routes.py`'s existing, better
   `_unique_username_from_email` (already used by Google sign-in) — DRY
   fix that closed both bugs at once.
5. **Unhandled account-migration race** — if a request's `jhome_sub` was
   already bound to a DIFFERENT existing user (the real-world case: a
   customer changed their email in Jhome Auth), the retry loop burned 3
   blind attempts and returned a misleading 500. Fixed with an explicit
   upfront check, returning a clean 409.
6. **Partial commit on refusal** — adopting a `jhome_sub` onto an existing
   user committed immediately, BEFORE the admin-refusal check ran later in
   the same function — so a refused admin handoff still permanently linked
   the `jhome_sub`. Fixed by removing the intermediate commit so it rides
   the function's single final commit instead (a refused request now has
   zero side effects, full stop).
7. **Consume-time admin re-check** — `/sso/consume` re-validated
   `is_active` (state can change in the 2-minute TTL window) but not the
   admin rule that `/internal/handoff` enforces at mint time. Fixed for
   consistency — a user promoted to admin mid-window can no longer get an
   admin session via a stale token.

**Deferred, flagged as follow-ups (spawn_task chips), not fixed here since
out of scope for this feature:**
- No session-minting cookie anywhere in this app sets `secure=True`
  (checked all five call sites) — pre-existing, app-wide gap, and
  `services/csrf.py` has a comment that falsely claims otherwise.
- `handoff_tokens` has no reaper — nothing deletes expired/used rows, so
  the table grows monotonically. Not a security issue (hash-only,
  single-use), just housekeeping that belongs in the existing
  `scheduler_loop()`.

**Operational, done live, not in code:** the Token Service's `App` row for
Gootier (id 6) had `shares_customer_balance=False` — flipped to `True`
directly on the live Token Service DB during deploy verification, since
wallet grouping needs it and it's a data flag, not a migration.

**Deployed and verified live 2026-09-02**, from a fresh GitHub clone (never
the primary checkout, which has unrelated concurrent work in progress —
`auth-kit.css`/`auth-kit.js`/`token-guard.js` uncommitted from another
session). Round-trip verified end-to-end against real production, DB-state
confirmed via `railway ssh`, not just HTTP status:
1. New Backoffice customer, no existing Gootier account → real user created
   (`tradingjay101@gmail.com`, id 5), correct defaults (role=client,
   tier=trial, is_active/is_verified=true, nickname from the handoff's
   `name` field), handoff token burned immediately.
2. Repeat handoff for the same email → reused the same account, confirmed
   no duplicate row.
3. Wallet grouping → confirmed via a fresh synthetic test handoff
   (`gootier-wallet-verify@example.com`) that a wallet created AFTER the
   `shares_customer_balance` flip correctly joins the customer's shared
   balance group. The real account's own wallet (created moments before
   the flip) needed a one-time manual re-link to the same customer group —
   done directly in the Token Service DB, since `ensure_wallet` never
   revisits an already-cached `jts_wallet_id`.

**Test data cleanup:** the synthetic verification user
(`gootier-wallet-verify@example.com`) could not be hard-deleted — Gootier's
`action_logs` table has a real FK constraint on `user_id` (not a loose
reference), so deleting it violated a foreign key. Neutralized instead
(deactivated, email scrubbed, `jhome_sub` cleared), matching the
insert-only-audit-trail pattern used elsewhere in the fleet (e.g.
stored-cloud's `audit_events`). The manually-minted Backoffice verification
session and a `ServiceVisit` row generated by the curl-based verification
(not real browser use) were both deleted cleanly by explicit id.

**Whole-branch review dispatched 2026-09-02** across both repos together,
checking payload-shape agreement between the two repos' handoff client/
server code, the `email_verified` field Backoffice's client sends that this
plan didn't originally anticipate, whether Gootier's `HandoffRequest` model
tolerates the `domains` field Backoffice sends to every connected app
unconditionally, durability of the manually-flipped `shares_customer_balance`
flag, and whether anything outside `/sso/consume` could mint a session. See
this file's next update for results once that review lands.
