# Gootier — Handoff Log

Chronological. Append a new `## <Topic> (YYYY-MM-DD)` section at the end; never
rewrite earlier sections — later entries supersede earlier ones by being later.

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
