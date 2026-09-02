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
