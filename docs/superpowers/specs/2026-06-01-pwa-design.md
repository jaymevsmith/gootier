# Gootier PWA — Design Spec

**Date:** 2026-06-01
**Status:** Approved, phase 1 in flight
**Author:** Brainstorm with Claude

## Goal

Ship Gootier as a Progressive Web App so users can install it to their home screen and receive push notifications, without building a second codebase, fighting App Store reviews, or paying Apple's 30% cut.

## Non-goals

- App Store / Play Store distribution (explicitly out of scope; revisit later via TWA wrapper for Android if Play Store becomes worth it)
- In-app purchases / IAP receipt validation (Stripe stays)
- A separate mobile codebase (React Native, Flutter, Capacitor, native Swift/Kotlin)
- OAuth deep-link handling (browser handles it)
- Bearer-token API auth (existing JWT cookies work for PWAs)

## Architecture

The current FastAPI + Jinja + SQLAlchemy stack stays exactly as-is. The PWA conversion adds:

- **Three static files** that turn the existing web app into something installable.
- **Three backend pieces** for push notification delivery via the W3C Push API + VAPID protocol.
- **Two UX patterns** for install + permission prompts.
- **Three native-feeling features** built with standard web APIs (no native code).

No screen rewrites. The existing Jinja templates already adapt to mobile breakpoints (mobile nav at 1024px per the cross-project rule); a targeted polish pass tightens what feels desktop-leaky.

## Components

### 1. Web App Manifest (`Gootier/static/manifest.webmanifest`)

JSON file declaring the install metadata:

```json
{
  "name": "Gootier",
  "short_name": "Gootier",
  "description": "AI-powered social scheduling and content creation",
  "start_url": "/dashboard",
  "display": "standalone",
  "background_color": "#0a0c14",
  "theme_color": "#667eea",
  "orientation": "portrait",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ],
  "share_target": {
    "action": "/m/share-target",
    "method": "POST",
    "enctype": "multipart/form-data",
    "params": {
      "title": "title",
      "text": "text",
      "url": "url",
      "files": [{ "name": "media", "accept": ["image/*", "video/*"] }]
    }
  }
}
```

`share_target` makes Gootier appear in the Android system share sheet. iOS has no PWA equivalent.

### 2. Service Worker (`Gootier/static/sw.js`)

Background script handling:

- **Push events** — `self.addEventListener('push', ...)` parses the JSON payload and calls `self.registration.showNotification(title, options)`.
- **Notification click** — `notificationclick` event opens the URL packed in the notification data.
- **Caching strategy** — "network-first, cache fallback" for HTML routes (so users always get fresh content when online, but the app shell loads instantly offline). "Cache-first" for `/static/*` (versioned via cache name bump on deploy).
- **Offline fallback** — if a navigation fails entirely, serve a cached `/offline` page.

Versioned cache name (`gootier-v1`, `gootier-v2`, ...) — bumped on deploy via a build-time string substitution so old caches get purged.

### 3. Base template additions (`Gootier/templates/base.html`)

Inside `<head>`:

```html
<link rel="manifest" href="/static/manifest.webmanifest">
<meta name="theme-color" content="#667eea">

<!-- iOS-specific PWA support -->
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Gootier">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
```

Inside the body's existing JS bundle, register the service worker:

```js
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(err =>
    console.warn('SW registration failed', err)
  );
}
```

### 4. VAPID push infrastructure (backend)

- **One-time VAPID keypair generation** — `pywebpush.generate_vapid_private_key()`. Public key embedded in templates; private key stored in env config as `VAPID_PRIVATE_KEY`.
- **`push_subscriptions` table** — columns: `id`, `user_id` (FK), `endpoint` (TEXT, unique), `p256dh` (TEXT), `auth` (TEXT), `created_at`, `last_used_at`, `user_agent` (TEXT, nullable). One user can have multiple subscriptions (phone + tablet + desktop).
- **`pywebpush>=1.14` in requirements.txt**.

### 5. Push API endpoints (`Gootier/routes/push_routes.py`)

- **`POST /api/push/subscribe`** — accepts `{endpoint, keys: {p256dh, auth}}` from the browser, upserts a `PushSubscription` keyed by `(user_id, endpoint)`.
- **`POST /api/push/unsubscribe`** — accepts `{endpoint}`, deletes the row.
- **`GET /api/push/vapid-public-key`** — returns the VAPID public key as JSON (so the browser can hand it to `pushManager.subscribe()`).

### 6. `send_push()` service (`Gootier/services/push.py`)

```python
def send_push(db, user, title, body, url=None, tag=None):
    """Fire a push to all of user's subscriptions.  Auto-prunes
    410-Gone subscriptions (browser revoked / uninstalled)."""
```

Wired into existing event paths:

- Scheduled `SocialPost.status` → `published` or `failed` (in `services/scheduler.py`)
- OAuth token expired (in `services/social_publish.py` token-refresh failure path)
- `MediaJob.status` → `done` for compose jobs (in `routes/media_routes._run_compose_job`)
- TikTok inbox item ready for user to publish (in `services/social_publish.py` TikTok path)

### 7. UX patterns

**Install prompt** (`Gootier/static/js/pwa-install.js`):
- Listen for `beforeinstallprompt` (Chrome/Edge/Samsung Internet). Stash the event, show our custom banner after the user has 3+ sessions tracked in `localStorage`.
- iOS Safari detection: show a different banner with the manual "Share → Add to Home Screen" instructions (with an illustrated mini-diagram).
- Dismiss persists in `localStorage` (`pwa_install_dismissed_at`); re-show after 30 days if still uninstalled.
- Hide entirely once `window.matchMedia('(display-mode: standalone)').matches` is true (already installed).

**Push permission prompt** — fired after the user does something that proves intent:
- Schedules their first social post → "Want a notification when this goes live?"
- Connects their first social account → "We'll let you know if the connection ever breaks."
- Never on first page load; never as a modal cold-open.

### 8. Native-feeling features (web APIs)

- **Camera/mic capture** — extend `/compose` with a "Record clip" button → `getUserMedia({video, audio})` → `MediaRecorder` → upload to existing `/api/media/assets` endpoint as if it were a file upload.
- **Web Share API (outbound)** — on the compose result modal and the studio output modal, add a "Share to other apps" button → `navigator.share({title, url, files})`. Works on iOS Safari + Android Chrome.
- **Share Target API (inbound, Android only)** — declared in the manifest. Backend adds `POST /m/share-target` route that accepts the shared title/text/url/files and pre-populates a new Compose draft.

## Data Flow

### Push notification flow

```
1. User opens Gootier → SW registers
2. After user schedules first post → JS calls /api/push/vapid-public-key
3. JS calls pushManager.subscribe(publicKey) → browser returns subscription
4. JS POSTs subscription to /api/push/subscribe → DB row created
5. Later: scheduler.publish() succeeds → calls send_push(user, "Posted!", "Your X post is live", "/dashboard")
6. pywebpush signs payload with VAPID private key, POSTs to subscription.endpoint
7. Browser's push service routes to the user's device → SW fires `push` event
8. SW calls showNotification() → user sees it
9. User taps notification → SW fires `notificationclick` → opens /dashboard
```

### Install flow (Android)

```
1. User visits in Chrome → SW registers, manifest parsed
2. After 3rd session → our custom install banner shows
3. User taps "Install" → we call beforeinstallprompt.prompt()
4. Chrome shows native install dialog → user accepts
5. Gootier icon added to home screen and app drawer
6. Subsequent launches open standalone window (no Chrome chrome)
```

### Install flow (iOS Safari)

```
1. User visits in Safari → SW registers, manifest parsed
2. After 3rd session → our iOS-specific banner shows
3. Banner shows illustrated "Tap Share → Add to Home Screen"
4. User does it manually → Gootier icon on home screen
5. Subsequent launches from icon open standalone (no Safari chrome)
6. Push only works if iOS 16.4+ AND PWA installed AND permission granted
```

## Error Handling

- **VAPID private key missing** — `send_push()` logs warning and no-ops; doesn't break the action that triggered it.
- **410 Gone from push endpoint** — `send_push()` deletes the subscription row (user uninstalled / revoked permission).
- **Service worker registration fails** — graceful degradation: site continues to work as a regular web app, no PWA install possible. Logged to console only.
- **Network offline during install** — manifest must be cached by SW or the install fails silently. Cached on first SW activation.
- **Push permission denied** — JS catches the rejected promise, sets `localStorage.push_denied = true`, never prompts again unless user explicitly enables in settings.

## Testing

- **Manual smoke tests** on real devices (iOS 16.4+ Safari, Android Chrome) — covered in phase 3.
- **Lighthouse PWA audit** — target 100/100 PWA score. Catches manifest issues, SW issues, HTTPS requirements, viewport meta, etc.
- **Push delivery test** — local script that registers a fake subscription, sends a test push, verifies receipt.
- **Cache invalidation** — bump cache version on deploy, manually verify old cache purged on second SW activation.

## Phased Delivery

### Phase 1 — Installable PWA exists *(this session)*
- Manifest, service worker (caching only, no push yet), `<head>` additions
- Generate 192/512/maskable icons from existing Gootier brand mark
- Install prompt banner with iOS + Chrome variants
- Lighthouse PWA score = 100

### Phase 2 — Push notifications *(separate session, ~3 days)*
- VAPID setup, `push_subscriptions` table, push routes
- `send_push()` service wired into 4 event triggers
- Permission prompt UX after first scheduled post / first connection
- Real-device delivery test

### Phase 3 — Native-feeling features *(separate session, ~3 days)*
- Camera/mic capture in /compose
- Web Share API on compose + studio result modals
- Share Target backend route + manifest entry
- Mobile-viewport audit pass (375×667 + 414×896)
- iOS PWA quirk fixes (safe-area-inset, viewport-fit=cover, etc.)

## Realistic Effort

| Phase | Effort | Outcome |
|---|---|---|
| 1 | 1 day | Installable PWA, no push |
| 2 | 3 days | Push notifications working end-to-end |
| 3 | 3 days | Camera, share, polish |
| **Total** | **~1.5 weeks solo** | **Production-quality PWA** |

## The honest catch

iOS PWAs are second-class citizens:
- Users must open in Safari (Chrome/Edge on iOS can't install PWAs)
- 5-step manual install flow (Share → Add to Home Screen)
- Push only on iOS 16.4+
- ~70% drop-off on first try

Onboarding copy must explicitly handhold iOS users through this. Detection JS shows different prompts based on browser + OS.

Android is unambiguously great — Chrome surfaces install itself, Share Target works, push is reliable.

## Future expansion (out of scope for this spec)

- Play Store distribution via TWA + Bubblewrap (if Android install rate justifies it)
- iOS App Store via Capacitor wrapper (if revenue justifies the Apple tax)
- Offline-first compose drafts that sync when network returns
- Native iOS Share Extension (requires Capacitor or above)
