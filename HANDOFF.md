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


## Whole-branch review results and fixes — 2026-09-02

The dispatched review (previous section) found **two severe seam bugs**,
invisible to every per-task review that preceded it because each sits at
the boundary between a touched line and untouched context around it —
exactly what a whole-branch pass exists to catch.

**1. Wallet grouping was broken for the feature's actual common case.**
`services/token_wallet.py::ensure_wallet` has a pre-existing cache
short-circuit (`if user.jts_wallet_id is not None: return ...`). Every
Gootier user who already had an account — normal signup already creates a
wallet — hit this short-circuit and NEVER reached the Token Service call
that applies `customer_ref` grouping. Only a user who was brand-new at the
exact moment of the handoff (no wallet yet) got grouped correctly, by
accident of ordering. The manually-flipped `shares_customer_balance` flag
(previous section) accomplished nothing for the target population — real
existing Gootier customers connecting their account via Jhome would never
have joined the shared balance. Fixed with a new
`link_wallet_to_customer(db, user)` that always calls the Token Service
(idempotent get-or-create on that end), used by the handoff route in place
of `ensure_wallet`. Verified with a genuine control: the reviewer
temporarily restored the old short-circuited path and confirmed zero Token
Service calls reproduced the exact bug, then confirmed the fix produces
exactly one call with the right `customer_ref`.

**2. Every refusal rendered as a generic "temporarily unavailable" 502,
and suspended accounts triggered a false credential-misconfiguration
alert.** The Backoffice's shared handoff client expects `detail` to be a
dict carrying a machine-readable `"error"` code (matching Jhome Auth's
convention); Gootier's five refusals all used plain strings, so none of
them ever matched the Backoffice's `_REFUSAL_COPY` lookup — every one fell
through to the generic outage page regardless of the real, permanent, and
often actionable reason. Separately, the deactivated-user refusal used
HTTP 401 — the exact status the Backoffice reserves for "the shared
internal key is wrong" — so every suspended Gootier customer who clicked
the tile fired an ERROR-level "handoff misconfigured" alert with no real
misconfiguration behind it. Fixed: real error codes copied from Jhome
Auth's reference implementation (`account_inactive`, `linked_elsewhere`,
`unverified_caller_email`, `ambiguous_identity` — all four are genuine
existing fleet codes, not invented) plus one Gootier-specific code
(`admin_account_not_supported`, no other connected app has needed to
refuse on admin role yet — noted in a comment that the Backoffice's
`_REFUSAL_COPY` has no specific entry for it yet, so it renders the
generic-but-still-terminal fallback page rather than a 502 until that copy
is added on the Backoffice side). Deactivated-user status changed 401 →
403, matching Jhome Auth's own convention for `account_inactive`.

**3. `email_verified` was silently ignored.** The Backoffice sends this
field as an explicit assertion "safe to bind an existing account by email
address"; Gootier's `HandoffRequest` didn't even declare it, so it was
Pydantic-dropped, and Gootier would adopt `jhome_sub` onto an existing
account matched by email with no check that the Backoffice actually
vouched for that email. Currently latent (Jhome Auth's own `/authorize`
already blocks unverified sessions from reaching a connected app), but the
fail-closed default the Backoffice's field exists to provide was defeated
on Gootier's end. Fixed: the field is now declared, and an EXISTING-user
match with `email_verified=False` is refused (409
`unverified_caller_email`, matching Jhome Auth's own code for this exact
condition) before any binding happens. New-user creation is deliberately
NOT gated by this — Jhome Auth's own gate already covers that path.

**4.** Added a regression test posting the real 5-field Backoffice payload
shape (`email, name, jhome_sub, domains, email_verified`) end-to-end,
pinning that the unused `domains` field stays silently dropped (Pydantic's
default `extra='ignore'`) rather than crashing — confirmed there's no
strict-mode config anywhere in this model that a future edit might
accidentally introduce.

**Also confirmed clean by the same review** (no fix needed): payload field
names agree exactly between the two repos; nothing outside `/sso/consume`
can mint a session; `GOOTIER_INTERNAL_KEY` is spelled identically in both
repos' source.

Fix commit `6acad43` (rebased onto the docs commit above after a local
worktree/remote divergence during the fix dispatch — caught before it
became a real problem, nothing was force-pushed or lost). Re-reviewed
(spec + code quality) after the fix: both passed, spec review included an
independent empirical proof that the wallet-grouping bug was genuinely
real and is genuinely fixed, not just claimed. 93 tests passing (from 84),
same 2 pre-existing unrelated failures.

**Still open, correctly out of scope for this fix:** the Backoffice's own
`_REFUSAL_COPY` has no entry for `admin_account_not_supported` yet (falls
through to a generic-but-terminal fallback page, not a 502 — acceptable,
but a Backoffice-side follow-up to add proper copy). The real account
created during Task 14's deploy verification (`tradingjay101@gmail.com`)
had its wallet manually re-linked to the correct customer group at deploy
time, BEFORE this fix existed — that manual correction remains correct and
doesn't need redoing, since `link_wallet_to_customer` would now produce
the identical result on its own for any future handoff.
