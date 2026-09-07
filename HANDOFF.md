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
  `scheduler_loop()`. **Fixed — see the entry below.**

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

## handoff_tokens reaper (2026-09-01)

**What changed:** `handoff_tokens` had no cleanup — every mint via
`POST /internal/handoff` left a row behind forever, so the table grew
monotonically (flagged as a deferred follow-up above). Added
`reap_expired_tokens(db)` in `services/handoff.py` and wired it into
`scheduler_loop()` (`services/scheduler.py`) via
`_cleanup_expired_handoff_tokens()`, running every 12h
(`HANDOFF_REAP_EVERY_TICKS`). Deletes rows where `expires_at` is more than a
day (`REAP_RETENTION`) in the past — covers both used and unused tokens,
since `expires_at` is set at mint time regardless of whether the token was
later redeemed.

Not a security fix — rows are hash-only and single-use — this is routine
housekeeping that was deliberately deferred out of the original SSO handoff
security review (branch `gootier-connected-app`, merged as PR #6) to keep
that PR scoped to the actual feature.

**State:** merged to `main` as [PR #8](https://github.com/jaymevsmith/gootier/pull/8),
commit `caad86f`, built off a fresh worktree at `.worktrees/handoff-token-reaper`
branched from `origin/main` — the primary checkout had unrelated uncommitted
work in progress (billing/template changes) and was 21 commits behind, so it
was left untouched.

**Tests:** `tests/test_handoff_reaper.py` (new, 3 cases: deletes past
retention, keeps within-retention/valid rows, deletes long-used rows too).
Full suite: `python3 -m pytest -q` → 96 passed, 2 pre-existing failures in
`tests/test_affiliates_integration.py` unrelated to this change (confirmed by
stashing this diff and re-running against unmodified `origin/main` — same 2
failures, likely a network-dependent affiliate-reporting call).

**Redeploy command:** none — no deploy config change, ships with the next
normal Railway deploy of the Gootier service once merged.


## Session cookie now marked Secure in production (2026-09-01)

**What changed:** picks up the "Deferred, flagged as follow-ups" item from
the section above — no cookie that mints a logged-in session (`/login`,
`/sso/consume`, signup auto-login, verify-email auto-login, Google OAuth
callback) set `secure=True` anywhere in the app, so the session JWT cookie
would be sent over plain HTTP in production. Added
`auth.set_session_cookie(response, token)` as the single call site for
minting it; it sets `secure=True` whenever `ENV` is `prod`/`production`
(same check `auth.py` already used at boot for the `SECRET_KEY` guard). All
five call sites in `routes/auth_routes.py` and `routes/oauth_routes.py` now
go through it instead of hand-rolling `response.set_cookie(...)`.

Also corrected `services/csrf.py`'s comment that claimed "samesite=lax +
secure-in-prod is the main mitigation" for the CSRF double-submit cookie —
that was never true; there is no secure-in-prod anywhere in the codebase
before this change, and the CSRF cookie *still* isn't marked secure after
it (out of scope, see below).

**State:** [PR #7](https://github.com/jaymevsmith/gootier/pull/7),
`fix-session-cookie-secure` → `main`, not yet merged. Branched off
`origin/main` at `f87530a` (which already includes PR #6, the connected-app
work above) in a dedicated worktree at
`.worktrees/fix-session-cookie-secure`, because the primary checkout had
unrelated uncommitted work (`routes/stripe_routes.py`,
`services/token_wallet.py`, several billing templates) that predates this
session and shouldn't be touched or bundled in.

**Redeploy:** merge PR #7, then the normal Railway deploy for this service
applies. No env var changes needed — this reads the existing `ENV` var,
which every Gootier deployment already sets.

**What's still open / found but deliberately not fixed here:**
- `services/csrf.py`'s CSRF double-submit cookie still doesn't set
  `secure=True` in prod. Only the session cookie was in scope for this fix
  (that's what the original review flagged); the CSRF cookie has the same
  class of gap and is a reasonable follow-up.
- The two pre-existing test failures in `tests/test_affiliates_integration.py`
  (`sqlite3.OperationalError: no such table: env_configs`) noted above are
  still present, confirmed unrelated to this change.
- No global `HTTPSRedirectMiddleware` / `TrustedHost` / proxy-header
  middleware exists in `main.py` — this fix only makes the cookie *itself*
  refuse to travel over HTTP once the browser is on HTTPS; it does not
  force HTTPS on the connection in the first place. Confirm the Railway
  edge/proxy terminates TLS and forwards `X-Forwarded-Proto` correctly
  before relying on `secure=True` alone, since a misconfigured proxy could
  make FastAPI think a request is HTTPS when it isn't.

**Trap found the hard way:** the primary checkout was dirty and on local
`main`, which was itself one commit *behind* `origin/main` (missing the
just-merged PR #6). Editing there would have both entangled unrelated
uncommitted work and started from a stale base. Always diff local `main`
against `origin/main` and check `git status` before assuming the primary
checkout is a safe place to branch from.

**Also:** this file previously had `Write` used on it without reading the
existing 218 lines first, which overwrote all of the above sections down
to just this one. Caught immediately (unexpectedly large deletion count in
the commit) and fixed with a follow-up commit restoring the original
content with this section appended — no force-push, no history rewritten.
Lesson: always `Read` this file before writing it, even when a `find` for
it in the wrong directory (the primary checkout, not the worktree the
branch was actually based on) suggests it doesn't exist yet.


## CSRF double-submit cookie also marked Secure in production (2026-09-02)

**What changed:** closed the follow-up flagged in the section above — the
CSRF double-submit cookie (`services/csrf.py::CSRFCookieMiddleware`) had
the exact same gap as the session cookie: no `secure=True` in prod, and its
own comment falsely claimed "secure-in-prod" was already the mitigation.
Added the same `os.getenv("ENV", "").lower() in {"prod", "production"}`
check used by `auth.set_session_cookie` directly to its `response.set_cookie`
call, and restored the comment to its original (now true) claim.

**Verified:** new `tests/test_csrf_cookie_secure.py`, RED before the change
(prod case asserted `Secure` present, failed against the real header) and
GREEN after; same pattern as `tests/test_session_cookie_secure.py`. Full
suite: 97 passed (up from 95), same 2 pre-existing unrelated failures.

**State:** same [PR #7](https://github.com/jaymevsmith/gootier/pull/7),
commit `98febc9`, still not merged. No remaining known cookie in this app
missing `secure=True` in prod.


## A Token Service outage 500s the pages that show a balance (2026-09-03)

**The report:** "when I select Gootier from the backoffice I get an error."

**What actually happened, in order:**

1. Backoffice `/services/gootier` called Gootier `POST /internal/handoff`. It
   **succeeded** — `handoff minted token for user 5` is in the logs twice at
   08:54:17 UTC. The handoff, `/sso/consume` and the session cookie were all
   fine; this was never an SSO bug.
2. `/sso/consume` redirected to `/dashboard`, which ran
   `credit_balance = balance_tokens(db, user) // 1000` (`routes/web_routes.py`).
3. That called the Jhome Token Service, which answered
   `GET /wallets/320/balance` → **404 with an empty body**. `JTSError` was
   unhandled on the route, so uvicorn returned a 500. The customer was signed in
   correctly and then shown an Internal Server Error.

**Root cause of the 404:** the Token Service itself was broken, not Gootier and
not the wallet. Its Railway deployment history shows a deploy at
`2026-09-03T08:46:22Z` and then one titled **"Restore token service; refund +
chargeback claw-back (merge 4a10e09)"** at `09:16:33Z`. The failures at 08:54:17
sit inside that window. Its current container's logs only begin at 09:16:59, so
the broken deployment's own logs are gone.

**How the empty body identified it.** No JTS route can produce a 404 with an
empty body — an unknown path returns `{"detail":"Not Found"}` (22 bytes), a
missing/foreign wallet returns `{"detail":"Wallet not found"}`, a bad key
returns 401. An empty-body 404 therefore came from in front of the app, which is
what pinned this on the deployment rather than on wallet 320 or on the API key.
Worth remembering: `POST /wallets` is a get-or-create that has no 404 branch at
all, so seeing it 404 was the tell.

**The outage is over and nothing is lost.** Verified from inside Gootier's own
container (`railway ssh --project c6ffd880-… --service gootier`, running a probe
through `services.env_config.get_env` so production's real config is used, and
printing only status codes):

```
GET /health              -> 200
GET /wallets/320/balance -> 200  {"wallet_id":320,"balance_tokens":250000,...}
```

**What this branch changes.** The outage is somebody else's; the 500 is ours.
Four page renders and one JSON endpoint called `balance_tokens()` with no
handling, so any Token Service blip took the page down:

| call site | page |
|---|---|
| `routes/web_routes.py` dashboard | `/dashboard` — where the Backoffice handoff lands |
| `routes/web_routes.py` studio_page | `/studio` |
| `routes/media_routes.py` assets_page | `/assets` |
| `routes/media_routes.py` media_catalog | `/api/media/catalog` — the generation modals open on this |
| `routes/stripe_routes.py` billing_page | `/billing` — **the page you go to when you run out** |

Added `services/token_wallet.balance_tokens_or_none()`, which returns `None`
when JTS cannot answer (both `JTSError` and raw transport failures — `httpx`
connect/read errors are not `JTSError` subclasses), and moved those five call
sites onto it. `None` renders as an em-dash, not `0`: an unreachable service
means the balance is *unknown*, and `0` reads as "you are out of tokens", which
would be a false statement that also aims the customer at the purchase page for
no reason.

**Deliberately NOT changed: `check_sufficient`.** It still raises. Failing open
on a label and failing open on an authorization are different decisions — a
balance we cannot read is not a balance we may authorize a charge against.
`tests/test_balance_render_degrades.py` pins that distinction with a test, so a
future "make it consistent" pass has to argue with it rather than quietly widen
the fail-open into the spend gate.

**Tests:** `tests/test_balance_render_degrades.py` (13 cases), RED first — the
four page cases failed with the literal production error,
`services.jts_client.JTSError: get_balance failed: 404`, before the fix. Full
suite: **113 passed, 2 failed**, the two failures being the same long-standing
`tests/test_affiliates_integration.py` `env_configs` gap; re-confirmed this
session by running that file in a throwaway worktree at unmodified
`origin/main`, which fails identically.

**Trap found the hard way (again):** the primary checkout at
`Projects/gootier-app/Gootier` was **31 commits behind `origin/main`** and dirty
with unrelated in-progress work (`display.py`, `static/js/token-guard.js`,
`static/css/auth-kit.css`, plus modified auth/billing templates — somebody's
compact-token-display and auth-kit work). Untouched. This branch was built in a
fresh worktree at `.worktrees/balance-degrade` off `origin/main`. That in-flight
work looks like it will also touch `templates/billing.html`'s balance line and
add a `tokens` Jinja filter, so expect a small conflict there and prefer the
filter version once it lands — this branch only stops the `None` from rendering
as the literal string "None", it does not add compact K/M formatting.

**Redeploy:** merge the PR; the normal Railway deploy for the `gootier` service
applies. No env var changes.

**Still open:**
- Nothing in this repo re-checks the Token Service's health, so a future JTS
  outage is still invisible here until a customer hits it. Gootier is not
  registered with the fleet monitor for that dependency.
- The Backoffice renders its own "could not sign you in" page only when Gootier
  refuses the *handoff*. A 500 from a Gootier page AFTER a successful handoff is
  invisible to the Backoffice, which by then has already 303'd the browser away.
  Nothing to fix here, but that is why this looked like a Backoffice problem.

**Merged and deployed (2026-09-03).** [PR #9](https://github.com/jaymevsmith/gootier/pull/9)
merged to `main` as `8b70366`; Railway's GitHub integration auto-triggered the
build 2 seconds later, so no `railway up` was run — worth noting, because the
primary checkout is 31 commits behind and deploying from it by hand would have
shipped a rollback. New deployment `1c358f16-13cc-45df-bcbc-be8710db3d61`
(previously `5ba6d779`), service Online.

Verified on the running container, not from the deploy's exit status:

```
RUNG3 balance_tokens_or_none present: True
RUNG3 routes.web_routes    uses_or_none=True old_unguarded_left=0
RUNG3 routes.stripe_routes uses_or_none=True old_unguarded_left=0
RUNG3 routes.media_routes  uses_or_none=True old_unguarded_left=0
RUNG5 degraded (JTS broken) -> None PASS
RUNG5 healthy (live JTS)  -> 250000 PASS
```

`old_unguarded_left=0` is the part that matters: it counts remaining bare
`balance_tokens(db, user)` calls in each deployed route module, so it fails loudly
if a call site was missed or an old image is still serving.

Then the reported click itself, end to end — a handoff token minted for the same
account and walked over the public URL exactly as the Backoffice does:

```
consume -> final url: /dashboard status: 200
dashboard status: 200   is 500 error page: False
  <div class="stat-value">250</div> <div class="stat-label">Tokens available</div>
billing status: 200
```

The original symptom is gone and the balance renders its real value.

**Redeploy command for this service, for next time:** none by hand — merge to
`main` and the GitHub integration builds it. If a manual deploy is ever needed,
run it from a worktree with `railway up --path-as-root .`, never from the primary
checkout while it is behind.


## Compact K/M token display (2026-09-03)

**What changed:** token counts now render `550` / `2K` / `750K` / `20M` instead
of a plain integer, per the house rule. Adds `display.py` at the repo root with
the canonical `format_tokens` (copied verbatim from the global `CLAUDE.md`, not
re-derived, so every app in the fleet rounds identically) and registers it as a
Jinja filter named `tokens`. The four balance surfaces now say
`{{ credit_balance|tokens }}` / `{{ token_balance_display|tokens }}`:
`dashboard.html`, `studio.html`, `assets.html`, `billing.html`.

The filter also absorbs the `None` case, so the em-dash handling added earlier
today collapses out of the templates — `None` is unknown, and unknown renders
`—`, never `0`.

**The trap, and it is specific to this repo: there is no shared Jinja
environment.** Six routers each construct their own
`Jinja2Templates(directory="templates")`, and each gets a private copy of the
filter dict. Registering `tokens` on one is invisible to the other five, and the
failure mode is a `TemplateAssertionError` at render time on whichever page was
not re-tested — a 500, not a fallback. So `display.install_filters()` wraps the
constructor at all five module-level sites, and `tests/test_display.py` walks
`routes/*.py` to assert none was missed. Removing the registration from
`media_routes.py` was confirmed to turn that test suite RED (6 failures) before
shipping, so the guard is real and not decorative.

The sixth instance is function-local, inside
`oauth_routes._google_auth_fail`, and renders `login.html`, which shows no token
counts. Left alone deliberately.

**What was deliberately NOT changed:**
- `/api/media/catalog`'s `balance` field stays an **integer**, not a formatted
  string. The rule applies to the number being *rendered*; `balance_display`
  stays an int on the wire so consumers keep working. Nothing in the app renders
  that field today — the generation modals fetch it but never display it.
- No JS `formatTokens` was added. The canonical JS implementation exists in the
  house rule, but Gootier has no client-rendered token count to apply it to, and
  shipping an unused copy invites it to drift from the Python one. Add it in the
  same change as the first JS surface that needs it.
- No `toLocaleString()` on a token count existed here to remove — the two hits
  in the codebase are datetime formatting in the admin pages.

**Tests:** `tests/test_display.py`, 40 cases — the house rule's table verbatim,
the threshold/rounding edges (`999_999 -> "1M"` rather than `"1000K"`,
`999_500 -> "999.5K"`, trailing `.0` dropped), the every-environment invariant,
and each of the four pages rendered through its own router's environment with
the number pulled out of its specific markup slot so a match elsewhere in the
document cannot fake a pass. Full suite: **153 passed, 2 failed**, the two being
the same long-standing `test_affiliates_integration.py` `env_configs` gap.

**Trap found the hard way (mine):** wrapping the constructor in
`install_filters(...)` broke the regex my own environment-scan test used to find
the routers, so that parametrised test silently iterated over an EMPTY list and
reported as a skip while appearing green. The only reason it was caught is a
deliberate `test_the_scan_actually_finds_the_environments` guard asserting the
scan finds >= 5. Any test that discovers its own parameters needs that guard, or
"passed" can mean "never ran".

**Heads-up for the primary checkout:** it has an UNTRACKED `display.py` with the
same `format_tokens`. `display.py` is now tracked on `main`, so a `git pull`
there will refuse with "untracked working tree file would be overwritten".
Delete the local copy and take the tracked one — it is the same function plus
`install_filters`. The in-flight `static/js/token-guard.js` is a different rule
(the 402 purchase-page redirect) and is untouched by this work.

**Redeploy:** none by hand — merge to `main` and the GitHub integration builds it.

## Fixed the 2 long-standing `test_affiliates_integration.py` failures (2026-09-02)

### Verdict up front

**Signup is not broken in production.** This was a test-isolation defect, not a
bug in the signup + affiliate-referral path. No production code was changed.

### What was actually wrong

The reported error (`sqlalche.me/e/20/e3q8`) is the generic SQLAlchemy
`OperationalError` doc link, not the detached-instance family it was assumed to
be. The real message is:

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: env_configs
[SQL: SELECT ... FROM env_configs WHERE env_configs."key" = ?]
[parameters: ('APP_URL', 1, 0)]
```

Chain, in `routes/auth_routes.py::signup_submit`:

- line 169 `trigger_verification_email(db, user, _app_url(request))`
- -> `_app_url()` (auth_routes.py:266) -> `get_env("APP_URL")`
- -> `services/env_config.py:24` opens **its own `database.SessionLocal()`**

`get_env()` does not take a session — it opens one bound to the module-level
engine built from `DATABASE_URL` (default `sqlite:///./gootier.db`). So
`app.dependency_overrides[get_db]` in the test fixture never reached it, and
those reads went to whatever database file happened to be in the working
directory. `_app_url()` is not wrapped in a try/except, so the error escaped and
the request 500'd.

The same call happens slightly earlier via `ensure_wallet` ->
`JTSClient.__init__` -> `get_env("TOKEN_SERVICE_URL")`, but that one is swallowed
by signup's `except Exception` (visible in the captured log as
`JTS ensure_wallet failed at signup: user=1`), which is why the traceback points
at `APP_URL`, not `TOKEN_SERVICE_URL`.

### Why production is fine

`main.py` lifespan calls `models.init_db()`, which runs
`Base.metadata.create_all()` and `_seed_env_configs()` before any request is
served. `env_configs` therefore always exists in a deployed app. The failure
requires an ambient database that is missing that table — i.e. a checkout with
no `gootier.db`.

### The trap (this is the part worth remembering)

**The suite's result depended on a gitignored file.** Copying the developer's
real `Gootier/gootier.db` into a fresh worktree made all 10 tests pass with no
code change; deleting it made them fail again. That is why they "fail on
`origin/main`" in a clean detached worktree but pass in the primary checkout —
the primary checkout has `gootier.db` (it does contain `env_configs`), a fresh
one does not, and SQLite silently creates an empty file on connect.

**Worse consequence of the same hole:** when that file *did* exist, `get_env()`
succeeded, so `ensure_wallet` built a real `JTSClient` — whose `TOKEN_SERVICE_URL`
default is the hardcoded **production** URL
(`services/jts_client.py:32`) — and the test suite made a live
`POST /wallets` to the production Jhome Token Service for a fake user. The
"passing" run took 9.99s versus 2.0s precisely because of that network round
trip. A test that only passes by calling production is worse than a failing one.
`TOKEN_SERVICE_URL` / `TOKEN_SERVICE_API_KEY` have no rows in the dev
`env_configs`, so it fell through to `os.getenv` and the production default with
an empty `X-API-Key`.

### What changed

Test infrastructure only — `tests/` only, no application code.

`tests/conftest.py`
- New `test_engine` fixture: one in-memory SQLite DB per test, `StaticPool` +
  `check_same_thread=False`, full schema created.
- New **autouse** `_isolate_session_local`: rebinds `database.SessionLocal` to
  `test_engine` for each test and restores afterwards. `SessionLocal` is a
  `sessionmaker` and `.configure(bind=...)` mutates it in place, so this reaches
  every module that did `from database import SessionLocal` at import time.
  Autouse on purpose — a test author can't be expected to know which transitive
  call opens its own session.
- New **autouse** `_no_real_network`: patches `httpx.HTTPTransport.handle_request`
  and `httpx.AsyncHTTPTransport.handle_async_request` to raise. `httpx.MockTransport`
  bypasses `HTTPTransport`, so `tests/test_jts_client.py` is unaffected.
- `db` now uses `test_engine`.

`tests/test_affiliates_integration.py`
- `client` fixture takes conftest's `test_engine` instead of building its own, so
  the request session and `get_env()`'s internal sessions share one database.
- The two failing tests now patch `services.token_wallet.ensure_wallet`, matching
  the other signup tests — without it they'd reach the live Token Service.
- Removed the now-unnecessary `patch.object(auth_routes, "get_env", ...)` from
  `test_signup_creates_jts_wallet` and
  `test_signup_succeeds_even_if_jts_ensure_wallet_raises`, and rewrote the
  docstrings that described this as "a pre-existing, unrelated issue ... out of
  scope for this task" — that description is now wrong.
- `test_signup_succeeds_when_ensure_wallet_local_commit_fails` **keeps** its
  `get_env` patch, deliberately: `StaticPool` means every session in a test shares
  one connection, and a real `get_env()` would open a second session on that
  connection while the request session is intentionally in pending-rollback
  state. That's a harness artifact (production gets its own connection), so it
  stays mocked out of the reproduction.

`tests/test_db_isolation.py` (new, 4 tests) — regression guards: `SessionLocal`
is bound to `:memory:`; `get_env()` reads the per-test DB; an empty DB reads
cleanly instead of raising `no such table`; real outbound HTTP raises.

### Verification (actually run, not assumed)

- Guards are non-vacuous: flipping both autouse fixtures to `autouse=False` fails
  all 4 isolation tests **and** reproduces the original 2 failures exactly.
- `PYTHONPATH=. pytest tests/ -q` -> **52 passed** (was 48 collected: 46 passed /
  2 failed; +4 new). Stable across 3 consecutive runs, ~2.4s.
- Passes **both** with and without a `gootier.db` in the working directory — the
  original contingency is gone.
- After a full run with the real dev DB present, `gootier.db` is byte-identical
  (sha256 compared). No stray `gootier.db` is created in a clean checkout.

Rerun with:

    PYTHONPATH=. pytest tests/ -q

`PYTHONPATH=.` is required — `tests/conftest.py` does `from database import Base`
and fails to import otherwise.

### Still open / not done here

- **`_app_url()` is the one unguarded DB read in signup's post-commit tail.** The
  user row is already committed at auth_routes.py:141 before line 169 runs, so a
  transient DB failure inside `get_env("APP_URL")` returns a 500 to a user whose
  account *was* created and who never got a session cookie. Not a live bug (the
  table always exists in prod), but it is the one place where a DB blip converts
  a successful signup into a visible error. Left alone deliberately — fixing it
  is a production-code change beyond this task. Flagged for a decision.
- **`get_env()`'s design is the underlying cause** and is unchanged: it opens its
  own session rather than accepting one. The conftest fixture contains the blast
  radius for tests; it does not remove the coupling. If `get_env` ever gains a
  `db` parameter, `_isolate_session_local` can shrink.
- **Whether the production Token Service actually created a wallet** for
  `external_user_id` "1" / "2" during earlier local test runs was not checked.
  The empty `X-API-Key` most likely got a 401, but if the JTS `/wallets` endpoint
  is unauthenticated there may be junk wallets to clean up. Worth a look in JTS.

## Hardened signup's post-commit tail against config-DB failures (2026-09-04)

Follow-up to the 2026-09-02 entry, which flagged `_app_url()`'s unguarded
`get_env()` as still open. This one **does** change production code.

### The gap

`signup_submit` commits the new User at auth_routes.py:141, then runs a tail of
best-effort work. Three of the four steps were wrapped (`ensure_wallet`,
`affiliates.report_signup`, `send_welcome_email`); the verification email was
not. Two unguarded config reads sat on that path, each opening its own
`database.SessionLocal()`:

- `_app_url(request)` -> `get_env("APP_URL")`
- `trigger_verification_email()` -> `send_email_verification()` ->
  `_smtp_config()` -> five more `get_env()` calls

A database blip in any of them raised straight out of the handler: a 500 for
someone whose account **had** been created and who never got a session cookie,
with nothing in the error to tell them they now have an account.

Fixing only `_app_url` would have left the identical hole one frame deeper in
`_smtp_config()`, so the fix covers the whole step.

### Changes — `routes/auth_routes.py`

- `_app_url()` catches a failed `get_env()` and degrades to the request's own
  scheme + host. That fallback already existed for an unset `APP_URL`, so this
  reuses a correct answer rather than inventing one. Also benefits
  `forgot_password_submit` (auth_routes.py:294) and both `api_routes.py` call
  sites (136, 151), which go through the same helper.
- `trigger_verification_email(...)` at the signup call site is now wrapped with
  `db.rollback()` + `logger.exception`, mirroring the `ensure_wallet` clause
  directly above it. The verify token is committed by
  `create_verification_token` *before* the SMTP reads happen, so it survives —
  the user just doesn't get the email, and can resend from their profile.
- `user_id = user.id` is captured right after `db.refresh(user)` and used for
  `create_access_token(user_id)`. Non-obvious but load-bearing: `db.rollback()`
  in a tail handler expires the instance, so every later `user.<attr>` read
  becomes a fresh SELECT. Issuing the session cookie — the one step that must
  not fail — no longer depends on the database still answering.

### Trap: the new wrapper nearly ate an existing regression test

`test_signup_succeeds_when_ensure_wallet_local_commit_fails` exists to prove the
`db.rollback()` in the **ensure_wallet** except clause is load-bearing; it
originally failed because the poisoned session blew up in
`trigger_verification_email`'s commit. Wrapping that call could have made the
test pass with the ensure_wallet rollback deleted — i.e. silently disarmed.

Checked explicitly by deleting that `db.rollback()` and re-running: the test
still fails, now on the `user.referral_code` read at auth_routes.py:172, which
sits between the two clauses and is still unguarded. `PendingRollbackError`
confirmed in the output. The guard is intact — but if that line is ever moved or
wrapped, re-verify this test the same way rather than trusting it.

### Tests

`tests/test_signup_resilience.py` (new, 5 tests): `_app_url` falls back when the
config read fails; `_app_url` still prefers a real configured value (control —
this one passed before the fix); signup returns 303 + session cookie with the
config DB unavailable; same when the verification step raises; and the User row
actually survives in the database, so the 303 isn't coming from a rolled-back
transaction.

`tests/conftest.py`: new `unavailable_config_db` fixture points
`database.SessionLocal` at a schema-less engine so every `get_env()` raises,
while the request's injected session keeps working off `test_engine`. That
asymmetry is the realistic shape of the failure (pool exhaustion / a blip
opening a new connection), and is the exact condition that used to 500 signup.
The `client` fixture moved here from `test_affiliates_integration.py` unchanged,
since two files now use it.

Verification: 4 of the 5 new tests fail against the pre-fix code for the right
reason (`no such table: env_configs`) and pass after. **57 passed**, stable over
3 consecutive runs, with and without a `gootier.db` present.

### Still open

- **`api_routes.py:137` has the same shape and was left alone.** The profile
  email-change handler calls `trigger_verification_email` right after its own
  `db.commit()`, unguarded, so a config-DB blip 500s a profile update that was
  already saved. `_app_url` is now safe there, but the SMTP reads inside
  `_smtp_config()` are not. Deliberately out of scope — different endpoint,
  separate call. `api_routes.py:151` (`resend_verification`) should **not** be
  wrapped: sending is the point of that request, so a failure belongs in the
  response.
- `get_env()` still opens its own session rather than accepting one. Both
  entries in this log work around that rather than fixing it.

## Same fix for the profile email-change endpoint (2026-09-05)

Closes the item the 2026-09-04 entry left open.

### The gap

`routes/api_routes.py::update_profile` has the same post-commit shape as signup:
it commits the new email address, then fires a verification email through the
same unguarded chain (`trigger_verification_email()` ->
`send_email_verification()` -> `_smtp_config()` -> five `get_env()` calls, each
opening its own `SessionLocal`). A database blip returned a 500 for a profile
update that had already been written — and `templates/profile.html` only
reloads on a successful response, so the UI never showed the new address either.

### Changes — `routes/api_routes.py`

- `trigger_verification_email(...)` wrapped with `db.rollback()` +
  `logger.exception`, mirroring `signup_submit`. The module had no logger; added
  `gootier.api`.
- `user_id` captured after `db.commit()` for the same reason as in
  `signup_submit` — the rollback expires `user`, so the log line must not need a
  fresh SELECT. Nothing after the wrapper touches `user`, so no other read had
  to move.
- The response now carries **`verification_email_sent`** when the email changed.
  Silently swallowing the failure would have been its own bug: the caller would
  sit waiting for mail that never went out. The key is **omitted** when the
  email did not change — an always-present `false` reads as a failure. This is
  additive; `templates/profile.html` ignores unknown keys.

### Deliberately not changed

- **`resend_verification` (api_routes.py:151+) is still unwrapped.** Sending is
  the entire point of that request, so a failure has to reach the caller rather
  than being absorbed into a cheerful `delivered: true`. Pinned by a test.
  Considered and rejected: returning `delivered: False` on a config-DB failure
  would fit the existing contract mechanically, but the UI renders that as
  "Email service is not configured — ask the admin to check the logs for your
  verification link", which is actively misleading during a blip because
  `create_verification_token` may not have produced a link to find.
- **`templates/profile.html` untouched.** It could warn on
  `verification_email_sent === false`, but it reloads the page 600ms after a
  successful save, which wipes any toast — and the reloaded page already shows
  the unverified banner with a working resend button. The recovery path exists;
  a toast that vanishes would be worse than nothing. Left as a UI decision.

### Trap: the first version of the resend test passed for the wrong reason

It asserted resend doesn't report `delivered: true` under a dead config DB — and
failed, showing `200 / delivered: true`. Not a code bug: the seeded fixture user
had `is_verified=True`, so the handler short-circuits at
`if user.is_verified` and returns `delivered: True` without attempting a send.
The test now marks the user unverified first. Worth remembering when writing
anything against that endpoint — the happy-path short-circuit looks exactly like
a successful send.

### Tests

`tests/test_profile_email_verification.py` (new, 5 tests) with its own
`profile_client` fixture. That fixture resolves `get_current_user` from the
**request's** session, the way the real dependency does — handing the handler a
User attached to a different session would make its `db.commit()` a no-op and
quietly invalidate every assertion in the file.

Coverage: the change is saved and returns 200 with the config DB down; the
response reports the mail as not sent; the happy path reports it as sent (so the
flag isn't hardcoded); a nickname-only change omits the key; and resend still
surfaces failure. 3 of the 5 fail against the pre-fix code.

**62 passed**, stable over 3 consecutive runs, with and without a `gootier.db`
present.

### Still open

- `get_env()` continues to open its own session instead of accepting one. All
  three entries in this log work around that rather than fixing it. If it ever
  takes a `db` parameter, the three `except Exception` wrappers added across
  these entries can be revisited.

## get_env() now accepts a caller's session (2026-09-05)

The root cause behind all three preceding entries, finally addressed rather than
worked around.

### The change — `services/env_config.py`

```python
def get_env(key: str, default: str = "", *, db: Optional[Session] = None) -> str:
```

- With `db`, the lookup uses that session and **leaves it open** — ownership
  stays with the caller. Without it, behaviour is exactly as before: open a
  short-lived `SessionLocal()`, close it.
- **Keyword-only.** `get_env("APP_URL", session)` would otherwise bind the
  session to `default` and silently return a `Session` object as a config value.
  A test pins this.
- The query runs under `db.no_autoflush`. `SessionLocal` sets `autoflush=False`,
  but a supplied session is not guaranteed to be one of ours, and a config read
  must never flush a caller's pending work as a side effect.

### Where it is threaded — and where it is not

Threaded through the paths that already hold a request session, which is every
path these HANDOFF entries touched:

| File | Change |
|------|--------|
| `routes/auth_routes.py` | `_app_url(request, db=None)`; signup and forgot-password pass `db`; `trigger_verification_email` forwards it to `send_email_verification` |
| `services/email_utils.py` | `_smtp_config(db=None)`, `send_email_verification(..., db=None)`, `send_password_reset(..., db=None)` |
| `routes/api_routes.py` | both `_app_url(request, db)` call sites |

That is 5 config reads per verification email that no longer open their own
connection, plus `APP_URL`.

**Left alone deliberately** — roughly 35 further call sites in `oauth_routes`,
`stripe_routes`, `web_routes`, `media_routes`, `services/media.py`,
`services/ai_generator.py`, `services/onboarding.py`, `services/jts_client.py`
and `services/secrets.py`. Most are module-level helpers (`def _meta_app_id():
return get_env("META_APP_ID", "")`) with no session in scope; threading one in
would mean changing their signatures and every caller for no gain.

One of them is not merely unnecessary but actively wrong to change:
**`services/secrets.py::_fernet()`** is reached from a SQLAlchemy
`TypeDecorator`'s `process_bind_param`, i.e. *during a flush*. Handing it the
flushing session would be re-entrant. This is the reason `db` is optional rather
than required, and it is recorded in the `env_config` module docstring so the
next person doesn't "finish the job" and break encryption.

`set_env()` still opens its own session. It is an admin-UI write path, not on
any request's critical path, and was out of scope here.

### Trap: this change silently disarmed three existing tests

`unavailable_config_db` broke `SessionLocal` only. That was a complete
simulation while every `get_env()` opened its own session — but the moment
`_app_url()` and `_smtp_config()` started taking `db=`, the request path stopped
touching `SessionLocal` at all, and the fixture stopped simulating anything for
those paths.

Caught by re-running the check from the 2026-09-04 entry: delete the handler
guards, confirm the tests fail. Three of them **passed** with their `try/except`
deleted:

- `test_signup_completes_when_the_config_database_is_unavailable`
- `test_signup_still_reports_the_account_as_created_in_the_database`
- `test_email_change_is_saved_when_the_config_database_is_unavailable`

The fixture now closes both routes: it drops `env_configs` from `test_engine`
(breaking the request session) **and** points `SessionLocal` at a schema-less
engine (breaking the no-argument form), restoring the table on teardown. All six
resilience tests were re-verified as non-vacuous afterwards — with every guard
deleted, 6 fail.

**Generalise this:** any change that alters *which session* a code path uses can
turn a resilience fixture into a no-op without failing anything. The suite goes
green and the coverage is gone. Re-run the delete-the-guard check after any such
change, not just after writing the test.

### Tests

`tests/test_env_config.py` (new, 7): reads through a supplied session; a
supplied session removes the dependency on `SessionLocal` (asserting the
no-argument form still raises, so the fixture is provably in force); the session
is left open and usable; default fallback; a cleared (`None`) stored value falls
through to the default; no flush of the caller's pending work (uses an
`autoflush=True` session, since `SessionLocal` would pass trivially); and `db`
cannot be passed positionally.

`tests/test_signup_resilience.py` gains `test_app_url_uses_the_session_it_is_handed`
— proving `_app_url` actually *forwards* the session rather than merely
accepting it, by showing the supplied-session call succeeds where the
no-argument call falls back.

`orphaned_session_local` (new conftest fixture) breaks `SessionLocal` only,
leaving `test_engine` healthy — the narrow counterpart to
`unavailable_config_db`. Both are needed: proving a supplied session helps
requires exactly one of the two to be broken.

Non-vacuity verified for the new guards too: removing `no_autoflush` and the
`*` fails exactly the two tests that cover them.

**70 passed**, stable over 3 consecutive runs, with and without a `gootier.db`
present.

### Still open

- `set_env()` and ~35 `get_env()` call sites still use their own session, by
  design (see above). `services/secrets.py::_fernet()` must stay that way.
- No caching. The module docstring's note still stands: config is read on demand,
  and a verification email is now 5 reads on one connection rather than 5
  connections. If that ever matters, a 30s TTL cache is the documented plan.

## Merged up to origin/main and opened PR #12 (2026-09-06)

Branch `claude/serene-bhaskara-94d80c` (the four entries above) integrated with
`origin/main` and pushed. **PR: https://github.com/jaymevsmith/gootier/pull/12**,
open against `main`, not yet merged.

`git merge origin/main` brought in 35 commits — the Backoffice SSO handoff,
`internal_routes.py`, `display.py`, `jhome_sub` / `customer_ref` wallet
grouping, `balance_tokens_or_none`, and the compact K/M token display. One
conflict, `HANDOFF.md` (add/add — both sides created the file independently).
Resolved by keeping origin/main's copy as the base and **appending** the four
new sections, per this file's own append-only convention. `STATUS.md` and
`routes/auth_routes.py` auto-merged; the `_app_url` / `db=` changes came through
intact (verified by grep, not assumed).

**177 passed** on the merged result, stable over 3 runs, with and without a
`gootier.db` present. All of `main`'s own tests pass under the new autouse
conftest fixtures (`_isolate_session_local`, `_no_real_network`) — worth noting,
since those apply suite-wide and were written before that upstream work existed.

### Traps for whoever picks this up

- **Local `main` is 35 commits behind `origin/main`** (it sits at `a5d33bf`,
  pre-PWA-merge). Merging anything into local `main` without fast-forwarding it
  first produces a divergent main missing the SSO handoff, the balance-outage
  fix and the compact token display. Always `git fetch` and compare against
  `origin/main`, not `main`.
- **The primary checkout has uncommitted work** — `routes/stripe_routes.py`,
  `services/token_wallet.py`, seven auth/billing templates, plus untracked
  `static/css/auth-kit.css`, `static/js/auth-kit.js`, `static/js/token-guard.js`
  and a `.worktrees/` directory. An in-flight "authkit" change, unrelated to
  this branch. `main` cannot fast-forward past it, because the upstream commits
  touch those same templates. Deal with that work before advancing `main`
  locally; nothing here touched it.
- The worktree at `.claude/worktrees/serene-bhaskara-94d80c` is deliberately
  **kept** — PR feedback needs somewhere to land.

### Still open

- PR #12 is unreviewed and unmerged. Nothing is deployed from this branch.
- The `set_env()` and `services/secrets.py` notes from the 2026-09-05 entry
  still stand.
