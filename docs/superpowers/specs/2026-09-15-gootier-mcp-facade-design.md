# Gootier MCP Façade — Design Spec

**Status:** Approved by user 2026-09-15, pending written-spec review.

## Goal

Give Gootier the same MCP-connected-app treatment already built and deployed for
MidCanvas: a customer can connect their own AI assistant to their Gootier
account and have it draft an AI campaign, schedule a social post, schedule an
email blast, or list which social channels are connected — the same three
actions/read a human already does through Gootier's own web UI, now callable
by an external MCP client (Claude Desktop, or any other MCP-speaking AI).

This is the second slice in a stated sequence: MidCanvas façade (done, live,
proven end to end) → **Gootier (this spec)** → Jhome Hosting (later).

## Scope

**In scope**, confirmed with the user during brainstorming:
- Draft an AI campaign (preview only — nothing persisted)
- Schedule a single social post
- Schedule a single email blast
- List the customer's connected social channels (a necessary read: nothing
  lets a caller schedule a post without knowing which `connection_ids` exist)

**Explicitly out of scope for this slice:**
- Gootier's combined "schedule a whole campaign, optionally with generated
  media" endpoint (`/api/ai/schedule`) — the user did not select media
  generation as an MCP capability. An AI client that wants to turn a drafted
  campaign into real scheduled items loops over `gootier_schedule_post` /
  `gootier_schedule_email_blast` per item instead.
- Image/video/music generation jobs (fal.ai pipeline) — not selected.
- Token balance reporting — not selected for this slice (scheduling and
  drafting, per the billing analysis below, cost zero Jhome tokens; there is
  nothing balance-relevant to report yet).
- Anything that mutates social connections themselves (connect/disconnect a
  channel) — OAuth flows aren't callable server-to-server; out of scope by
  construction, not a deliberate cut.

## Architecture

Two repos change, mirroring the MidCanvas/Jhome-MCP-Server split exactly:

1. **Gootier** (this repo) gains 5 new internal routes under `/internal/mcp/*`,
   guarded by a new secret, `GOOTIER_MCP_INTERNAL_KEY` — deliberately separate
   from the existing `GOOTIER_INTERNAL_KEY` that Backoffice's `/internal/handoff`
   already uses (different caller, different blast radius if one leaks; same
   reasoning MidCanvas's `MCP_INTERNAL_KEY` used).
2. **A new standalone repo, `Gootier-MCP-Server`** (to be created, `~/Documents/Claude/Projects/Gootier-MCP-Server`)
   — FastAPI + SQLModel + Postgres (matching Jhome-MCP-Server's stack, NOT
   Gootier's own SQLAlchemy-core stack — Gootier's models are not shared
   directly). Owns its own `Customer`/`ConnectionKey` tables, its own
   `/connect` web UI for a customer to mint a connection key, and the actual
   MCP tool registrations an AI client calls.

Each connected app gets its own dedicated MCP server (confirmed with the user)
rather than growing one shared server's tool surface across unrelated
products — keeps each product's auth, billing, and deploy blast radius
independent.

## Gootier-side: 5 new internal routes

### Shared identity helper

`resolve_or_create_gootier_user(db: Session, *, jhome_sub: str | None, email: str, email_verified: bool) -> User`,
extracted from `handoff()`'s existing inline logic in `routes/internal_routes.py`
(find-or-create by email, jhome_sub binding/conflict handling, admin/inactive
refusal — the exact logic already adversarially reviewed for `/internal/handoff`).
`handoff()` is refactored to call this helper — a **behavior-preserving
refactor**, verified by the existing handoff test suite passing unchanged.

Every one of the 5 new routes calls this SAME helper independently (not only
`ensure-account`) — mirroring MidCanvas's `generate_image_for_mcp`, so the
system works correctly even if a customer's AI client never explicitly calls
`ensure-account` first.

**Precondition, inherited from the extracted logic:** `email` must already be
normalized (`.strip().lower()`) before calling the helper — each new route
does this itself, matching `handoff()`'s own existing normalization.

### Convention for all 5 routes

- **Method:** POST, always — including the "list connections" read. Identity
  (`jhome_sub`, `email`, `email_verified`) travels in the JSON body, never as
  a query parameter, keeping identity out of URLs and request logs.
- **Auth:** `require_mcp_internal_key()` (new function, same fail-closed shape
  as the existing `require_internal_key()` — `secrets.compare_digest`,
  byte-encoded to handle non-ASCII header bytes safely — but checking the new,
  separate `GOOTIER_MCP_INTERNAL_KEY` setting).
- **Success response:** plain dict, FastAPI-serialized directly.
- **Refusal response:** structured `{"error": "<short_code>", "message": "<human string, where available>"}`
  — matching `/internal/handoff`'s own existing refusal convention (`{"error": "unverified_caller_email"}`
  etc.), NOT the plain-string-`detail` shape `quotas.py`/`token_wallet.py`
  currently use for their user-facing HTTP errors.
- **Error normalization:** each route wraps its calls into `services/quotas.py`
  (`check_and_raise`, `check_per_call`) and any `require_perm`-style permission
  check, catching the existing `HTTPException(403/402, detail=<string>)` and
  re-raising as `HTTPException(same_status, detail={"error": <specific_code>, "message": <original string>})`.
  The original human-readable string is preserved as `message` so the eventual
  customer-facing copy can still reflect Gootier's real limits without the MCP
  tool layer string-matching UI prose.
- `response.headers["Cache-Control"] = "no-store"`, matching `/internal/handoff`'s
  own convention for any internal route.
- Router style: no prefix, full paths spelled out in each route decorator —
  matching `routes/internal_routes.py`'s existing convention (`router = APIRouter()`,
  not `APIRouter(prefix=...)`).

### `POST /internal/mcp/ensure-account`

**Request:** `{jhome_sub, email, email_verified}`
**Response:** `{"user_id": int, "tier": str}`

Resolves/creates the user via the shared helper, returns its id and tier.
**Deliberately does NOT link the Jhome Token Service wallet** — unlike
MidCanvas's `ensure-account`, which needed wallet-grouping because the SAME
slice's `generate/image` bills immediately. None of this slice's 4 tools
bill anything (confirmed in each route's own section below), so wiring wallet
linking in now would be speculative scope with nothing in this plan to
exercise it. Add it in whichever future slice first needs balance-awareness,
not here.

### `POST /internal/mcp/social-connections`

**Request:** `{jhome_sub, email, email_verified}`
**Response:** `{"connections": [{"id": int, "platform": str, "display_name": str}, ...]}`

Resolves identity, queries the user's active `SocialConnection` rows (the
same query `/api/social/posts` already uses to validate `connection_ids`
ownership), returns the minimal shape an AI client needs to pick a target
for `schedule-post`.

### `POST /internal/mcp/schedule-post`

**Request:** `{jhome_sub, email, email_verified, content, connection_ids: [int], image_url?, video_url?, link_url?, scheduled_at?}`
**Response:** `{"id": int, "status": str}` (status: `pending`/`published`/`partial`/`failed`, matching `/api/social/posts`'s own existing shape)

Resolves identity, validates `connection_ids` belong to and are active for
this user (reusing the existing validation from `routes/api_routes.py:237-243`,
or extracting it into a shared helper if that proves cleaner during
implementation), quota-gates via `check_and_raise(db, user, "posts_per_month")`
with normalized-error wrapping, creates the `SocialPost` row, publishes
immediately via the existing `publish_to_connections(...)` if `scheduled_at`
is unset — otherwise leaves it `pending` for Gootier's own existing scheduler
loop to pick up. **Zero Jhome tokens billed** (confirmed: `/api/social/posts`
never calls the token wallet).

### `POST /internal/mcp/schedule-email-blast`

**Request:** `{jhome_sub, email, email_verified, subject, body_html, recipients: [str], scheduled_at?}`
**Response:** `{"id": int, "status": str}` (status: `sent`/`partial`/`failed`/`pending`)

Resolves identity, checks the `marketing.email_blast` permission (structured
`403 {"error": "plan_upgrade_required", "message": "..."}` if the caller is
on trial — trial's tier permission for this is `False`), quota-gates via
`check_and_raise(db, user, "blasts_per_month")` AND `check_per_call(db, user, "blast_recipients", len(recipients))`,
creates the `EmailBlast` row, sends immediately via the existing send path if
unscheduled. **Zero Jhome tokens billed** (SMTP-based, confirmed no wallet call).

**Known real-world latency, planned for up front (see Client Timeout below):**
Gootier's own handler sends synchronously, in-request, to every recipient —
up to 25,000 for a gold-tier blast. This is a genuinely slow path, not a
hypothetical one.

### `POST /internal/mcp/draft-campaign`

**Request:** `{jhome_sub, email, email_verified, plan: str, schedule: str, count: int, channels: [str]}`
**Response:** pass-through of the existing `generate_campaign()` JSON shape — `{"items": [{"kind", "content", "subject", "link_url", "image_prompt", "video_prompt", "suggested_asset_kind", "scheduled_at"}, ...]}`

Resolves identity, quota-gates via `check_and_raise(db, user, "ai_generations_per_month")`,
calls the existing `services/ai_generator.generate_campaign(...)` (Anthropic-backed,
`claude-sonnet-4-5`). **Preview only — nothing is persisted or scheduled.**
Matches `/api/ai/generate`'s existing behavior exactly; this route is a thin
identity/auth wrapper around it, not a reimplementation.

**Confirmed, worth restating explicitly since it's counterintuitive:** this
route does NOT bill Jhome tokens, even though it calls Anthropic — Gootier's
own `/api/ai/generate` doesn't either (an apparent inconsistency in Gootier's
own established convention relative to its media-generation billing, which
IS token-billed; not this façade's problem to fix, just accurately modeled
here rather than assumed).

## Gootier-MCP-Server: the new repo

### Data model

`Customer` (email, `jhome_sub` unique, `token_wallet_id`, timestamps) and
`ConnectionKey` (hashed key, `customer_id`, `display_prefix`, `revoked_at`) —
copied structurally from Jhome-MCP-Server's own `app/models.py`, since this
is a separate service with its own database, not a shared one.

### `GootierClient`

Mirrors `MidCanvasClient` exactly: a `Protocol` (`ensure_account`,
`list_social_connections`, `schedule_post`, `schedule_email_blast`,
`draft_campaign`), `HTTPGootierClient` (real, httpx-based), `FakeGootierClient`
(deterministic, discriminating — different prompts/content must produce
different fake ids, matching the lesson already paid for twice this session),
`get_gootier_client()` (provider-name-gated: `GOOTIER_PROVIDER=http` + both
key and URL set selects real, anything else the fake).

**Fail-closed**, matching `MidCanvasClient`'s own reasoning: a failed
post-schedule or blast-send must surface as an error the tool layer can act
on, never silently succeed. Raises `GootierError(status_code, detail)` on any
non-2xx AND on any connection-level failure (wrapping `httpx.HTTPError`/
`httpx.InvalidURL`/`ValueError`, exactly like `MidCanvasClient`'s own
connection-failure fix) — never a raw httpx exception.

**Client timeout, set generously from the start:** the MidCanvas walk found,
the hard way, that a client timeout shorter than the real underlying
operation's worst case turns a successful, already-committed action into a
lost response. Gootier's own email-blast send is synchronous and can run
against thousands of recipients; its AI campaign draft calls Anthropic.
`HTTPGootierClient`'s timeout starts at a generous value (300s, matching the
pattern MidCanvas settled on after the incident) rather than a naive default
discovered as a bug during the eventual live walk.

`GootierError` gets a `detail_dict()` helper identical to `MidCanvasError`'s
own, for the same reason: `detail` may be a parsed structured-error dict or
raw text, and callers should not have to hand-roll an `isinstance` check.

### The 4 MCP tools

`app/mcp_app/tools_gootier.py`, mirroring `tools_media.py`'s shape exactly —
an inner `_*` function that does the work and raises `GootierError`, a thin
wrapper that catches it and renders a customer-facing sentence:

- `gootier_draft_campaign(topic, schedule, count, channels)` → renders the
  draft items as readable text (not raw JSON) for the AI client to present
  and let the customer react to.
- `gootier_list_social_connections()` → renders the connected channels
  (id + platform + display name) so the AI/customer can pick targets.
- `gootier_schedule_post(content, connection_ids, image_url?, video_url?, link_url?, scheduled_at?)` →
  renders a confirmation sentence with status.
- `gootier_schedule_email_blast(subject, body_html, recipients, scheduled_at?)` →
  renders a confirmation sentence with status.

**Error rendering, one sentence per structured code** (not one generic
catch-all): `posts_quota_exceeded`, `blasts_quota_exceeded`,
`recipient_cap_exceeded`, `plan_upgrade_required`, `ai_generations_quota_exceeded`
each get their own specific, actionable sentence built from the code (falling
back to Gootier's own `message` string where a specific rendering hasn't been
written yet, never a raw stack trace or internal error, matching
`tools_media.py`'s "never relay MidCanvas's own error text" principle applied
to Gootier's own internal detail instead).

### Tool registration

`app/mcp_app/server.py`'s `register_tools(server)` gains the 4 `@server.tool()`
functions, each resolving `current_customer(session)` and delegating to
`tools_gootier`, matching `jhome_generate_image`'s existing registration
pattern in Jhome-MCP-Server exactly (same `open_session()`/`current_customer()`
helpers, same `str = ""` → `None` conversion convention for optional string
params at the tool-signature boundary).

## Testing & rollout

Same discipline as the MidCanvas slice throughout: TDD per task (failing test
first, verify red, implement, verify green), spec-compliance review AND
code-quality review per commit (each independently re-running tests, never
trusting a self-report), mutation-proofs on every guard (each quota-refusal
code path, connection-key auth, the identity-resolution conflict/refusal
paths), a whole-branch review on each repo before proposing deployment, and
an explicit stop for the user's go-ahead before any push or deploy — both
repos have real, or soon-to-be-real, customers.

All Gootier-side work happens in a fresh worktree off `origin/main`
(`.worktrees/gootier-mcp`, branch `feat/gootier-mcp-facade`) — the primary
checkout is dirty with unrelated in-progress work and 49 commits behind, per
this repo's own repeatedly-documented lesson never to branch or deploy from it.

An end-to-end real customer walk (mirroring MidCanvas's Task 11) closes the
slice: draft a real campaign, schedule a real post and confirm it actually
publishes to a real connected channel, schedule a real email blast, list
connections and confirm the IDs actually match what was used, and exhaust a
quota deliberately (on a throwaway trial-tier test account) to confirm the
refusal renders the specific, correct sentence — not just that the happy
path works.
