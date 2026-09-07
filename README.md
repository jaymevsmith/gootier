# Gootier

Subscription SaaS for end-user marketers — connect your socials via OAuth, write or AI-generate campaigns, schedule once and let the scheduler do the rest.

## What it does

- **One-click social connections** — OAuth-based, no API-key copy/paste. Facebook Pages today; Instagram, X, LinkedIn, and TikTok on the roadmap.
- **AI campaign builder** — describe your marketing plan and cadence, and the Anthropic-backed generator drafts a ready-to-schedule mix of posts and email blasts.
- **Compose & schedule** — write posts or email blasts manually, fan them out across every connected channel, and let the background scheduler fire each one on its due time.
- **Tier-gated quotas** — Trial → Bronze → Silver → Gold, each with monthly post/blast/AI caps and per-blast recipient limits. Enforced at the API layer; usage shown on the dashboard.
- **Stripe subscriptions** — Checkout, customer portal, webhooks. Tier changes propagate from `customer.subscription.*` events into the user record.
- **Full admin panel** — users (roles, tiers, active/verified flags), env & keys (runtime config without redeploy), filterable activity log.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI 0.104+, Python 3.11 (prod), 3.14 (local dev) |
| Auth | JWT cookies signed with `python-jose`, passwords hashed with `bcrypt` directly |
| ORM | SQLAlchemy 2.x with inline idempotent `_upgrade_*` migrations |
| Database | SQLite for dev, Postgres for prod via `DATABASE_URL` |
| Templates | Jinja2 + Bootstrap 5 + Font Awesome 6.0.0 (pinned) |
| Frontend JS | Vanilla, no framework, driven by the `TJ.*` global namespace |
| Social APIs | `httpx` direct calls, no SDKs |
| AI | Anthropic SDK with prompt caching on the marketing-plan context |
| Billing | `stripe` SDK with hosted Checkout + Customer Portal |
| Scheduler | `asyncio.create_task` loop on a 60-second tick — no Celery, no APScheduler |
| Deploy target | Railway (Docker, via `railway.toml`), Railway Postgres, env in the Railway dashboard + `env_configs` overrides |

## Quick start

```bash
git clone https://github.com/jaymevsmith/gootier.git
cd gootier

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Generate a real signing key and paste it into SECRET_KEY:
python3 -c "import secrets; print(secrets.token_hex(32))"

# Bootstrap an admin user (creates the SQLite DB on first run):
python create_admin.py admin admin@example.local 'YourStrongPassword1!'

uvicorn main:app --reload --port 8002
```

Open `http://localhost:8002`, sign in as `admin`, and head to **Admin → Env & Keys** to paste your Anthropic, Stripe, Meta, and SMTP credentials.

## Configuration

Gootier reads configuration in this order: **`env_configs` DB rows → `.env` → defaults**. That means you can override any key at runtime from the admin UI without restarting — useful for rotating API keys or flipping a Stripe price ID without a deploy.

| Group | Keys |
|---|---|
| Core | `APP_URL`, `ALLOWED_ORIGINS`, `SECRET_KEY` (locked), `DATABASE_URL` (locked) |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `FROM_EMAIL` |
| AI | `ANTHROPIC_API_KEY` |
| Billing | `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_BRONZE/SILVER/GOLD` |
| Social | `META_APP_ID`, `META_APP_SECRET`, `META_OAUTH_REDIRECT` |

Locked keys (`SECRET_KEY`, `DATABASE_URL`) can only change via deploy — rotating the signing key invalidates every active session, and swapping the DB URL mid-flight would break SQLAlchemy's connection pool.

## Project layout

```
.
├── main.py                     # FastAPI app + lifespan (init_db, scheduler loop)
├── auth.py                     # bcrypt, JWT cookies, role/tier helpers
├── database.py                 # SQLAlchemy engine + SessionLocal
├── models.py                   # All ORM models + idempotent migrations + seeds
├── create_admin.py             # First-admin bootstrap CLI
├── routes/
│   ├── auth_routes.py          # /login, /signup, /forgot-password, /reset-password, /verify-email
│   ├── web_routes.py           # /, /dashboard, /connections, /compose, /ai-builder, /scheduled, /calendar, /profile
│   ├── api_routes.py           # /api/* JSON endpoints (posts, blasts, AI, profile)
│   ├── oauth_routes.py         # /oauth/facebook/{start,callback} with HMAC-signed state
│   ├── stripe_routes.py        # /billing, /api/billing/*, /webhooks/stripe
│   └── admin_routes.py         # /admin/users, /admin/env, /admin/logs
├── services/
│   ├── scheduler.py            # 60s asyncio loop publishing due posts + blasts
│   ├── social_publish.py       # Facebook Pages publish via Graph API
│   ├── email_utils.py          # SMTP send for verification, reset, blasts
│   ├── ai_generator.py         # Anthropic campaign generator with prompt caching
│   ├── quotas.py               # Tier quota enforcement (check_and_raise, usage_summary)
│   ├── env_config.py           # Runtime env overrides (DB > os.environ > default)
│   └── flash.py                # Server-set, JS-consumed toast on next page load
├── templates/                  # Jinja2 templates — base.html + auth_base.html + per-page
└── static/
    ├── css/design-system.css   # Light page + dark chrome, brand gradient tokens
    └── js/tj-notify.js         # TJ.* namespace: toasts, progress, confirm, api, flash
```

## Key concepts

### Multi-tenant ownership
Every marketing record (`social_connections`, `social_posts`, `email_blasts`) carries a `user_id` FK and every query filters by it. There is no cross-user data access path outside the admin panel.

### Tiers, roles, permissions
Roles (`admin`, `tech`, `strategist`, `marketing`, `client`) and tiers (`trial`, `bronze`, `silver`, `gold`) drive permission and quota matrices stored as JSON in `tier_configs` and `role_configs`. Staff bypass tier checks. Permissions are checked at **both** layers — the API guard and the template gate — never UI-only.

### TJ.* notification system
Every error, warning, success, and long-running task feedback flows through the `TJ.*` global namespace in `static/js/tj-notify.js`:

- `TJ.notify.{success,error,warning,info,alert}` — toasts with auto-dismiss + close button
- `TJ.progress.{start,update,done,fail}` — 3px top progress bar
- `TJ.confirm(msg, opts)` — promise-based modal, replaces native `confirm()`
- `TJ.api.{get,post,put,patch,del}` — fetch wrapper that handles 401 redirects, surfaces FastAPI `.detail`, and drives the progress bar
- `TJ.btnLoading(btn, label)` — disables a button and shows a spinner; returns a restore callback
- Server-side `services.flash.set_flash()` sets a `_tj_flash` cookie that the JS consumes on the next page load — for redirect-after-POST flows

Never use `alert()`, `confirm()`, or inline toast HTML. Validation uses `warning`; system failures use `error`.

### OAuth state hardening
`/oauth/facebook/start` HMAC-signs the state value as `<user_id>:<nonce>:<sig>` using `SECRET_KEY`. `/oauth/facebook/callback` requires the authenticated cookie session to match the state's `user_id`, blocking forged callbacks that would attach a Facebook Page to someone else's account.

### Background scheduler
`services/scheduler.py` runs as an `asyncio.create_task` in the FastAPI lifespan. Every 60 seconds it queries due `SocialPost` and `EmailBlast` rows, publishes them via `social_publish` / `email_utils`, stores per-platform JSON results, and updates each row's `status` to `published`, `partial`, `failed`, or `sent`.

## Admin panel

| Page | What it does |
|---|---|
| **/admin/users** | Filter by role/tier/status, click any row to edit roles, tier, subscribed-until, active, verified. Safety rails prevent admins from removing their own admin role or disabling themselves. |
| **/admin/env** | Runtime env-var editor. Changes take effect on the next request — no restart. Secret values masked; locked keys (`SECRET_KEY`, `DATABASE_URL`) reject edits. |
| **/admin/logs** | Filterable audit trail of every meaningful action (logins, signups, AI generations, admin updates, env changes, OAuth connects, password resets, etc). |

## Deployment

**Railway** — project `Gootier_App`, service `gootier`, environment `production`,
serving https://gootier.jhomeautomation.com.

### Deploying is merging

The service is git-linked to this repo, so **merging to `main` triggers the build
automatically**. There is no deploy command to run.

Do not reach for `railway up` instead. A checkout that is behind `main` will
build and ship happily, which means deploying by hand from a stale working copy
is a silent rollback. If a manual deploy is genuinely needed, run it from an
up-to-date worktree with `railway up --path-as-root .`.

Note that this cuts both ways: a docs-only merge rebuilds and redeploys too, so a
changed deployment ID does not by itself mean the code changed.

### How the container runs

`railway.toml` pins the build to the `Dockerfile` and sets `healthcheckPath =
"/health"` with a 300-second timeout. That window is deliberately generous —
`init_db()` runs `create_all`, the inline `_upgrade_*` migrations, the config and
tier seeds, and an at-rest token re-encrypt pass on *every* cold boot, and with
multiple workers serialized through a `pg_advisory_lock` the total is roughly 2×
the single-worker time.

The image's `CMD` is shell-form so `${PORT}` expands at container start:

```
gunicorn -k uvicorn.workers.UvicornWorker -w 2 -t 120 -b 0.0.0.0:${PORT:-8000} main:app
```

Railway injects `PORT`, so the listening port in production is **not** 8000 —
don't hardcode it anywhere. Don't add a `startCommand` to `railway.toml` either;
it would override the Dockerfile, and Railway's own `${PORT}` expansion there
isn't reliable.

### Configuration

`DATABASE_URL` comes from the Railway Postgres plugin. Everything else is a
Railway dashboard variable, overridable at runtime from `/admin/env` — see
[Configuration](#configuration) for the full precedence rules.

**One-time setup:**
1. Add the Railway Postgres plugin — it provisions `DATABASE_URL`
2. Set `SECRET_KEY` and `APP_URL` (and `ALLOWED_ORIGINS`) as Railway variables — both are locked against `/admin/env` edits
3. Set the SMTP, Anthropic, Stripe, Token Service and OAuth keys, either as Railway variables or, once the app is up, from `/admin/env`
4. Register the Stripe webhook endpoint at `{APP_URL}/webhooks/stripe`
5. Register your Meta OAuth redirect URI as `{APP_URL}/oauth/facebook/callback`
6. Bootstrap the first admin inside the running container:
   `railway ssh --service gootier -- python create_admin.py <username> <email> <password>`

### Verifying a deploy

A green health check proves the container starts, not that it runs the code you
merged. Probe for something that exists only in the new build — a signature, a
route, a string — from inside the running container with `railway ssh`. Recent
`HANDOFF.md` entries show the pattern and the output worth capturing.

## Known follow-ups

The audit pass left a few items deliberately deferred — pick when you need them:

- **CSRF tokens** on form-encoded auth endpoints (`samesite=lax` is the only mitigation today)
- **Encrypt OAuth tokens at rest** in `social_connections.access_token`
- **Standalone email-blast composer** (`/blasts`) — today blasts can only be created via the AI builder
- **Instagram + LinkedIn + X + TikTok** OAuth flows (same Meta app for IG, separate dev apps for the others)
- **Calendar timezone display** — currently all UTC; intuitive only if your users think in UTC
- **Touch drag-to-reschedule** — HTML5 drag-drop works on desktop only

---

Powered by [Jhome Automation](https://jhomeautomation.com).
