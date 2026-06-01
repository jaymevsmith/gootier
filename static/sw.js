/**
 * Gootier service worker — phase 1 (caching + offline fallback).
 * Push handlers land in phase 2.
 *
 * Cache strategy:
 *   /static/*           → cache-first (static assets versioned via cache name)
 *   HTML navigation     → network-first, fall back to cached page or /offline
 *   Everything else     → network-only (passes through)
 *
 * Bump CACHE_VERSION on every deploy so old assets get purged.
 */

const CACHE_VERSION = "gootier-v1";
const STATIC_CACHE  = `${CACHE_VERSION}-static`;
const PAGES_CACHE   = `${CACHE_VERSION}-pages`;

// Files prefetched on install so the app shell loads instantly + offline works.
const PRECACHE_URLS = [
  "/offline",
  "/static/css/colors_and_type.css",
  "/static/css/design-system.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS).catch((err) => {
        // Individual fetches failing on install shouldn't abort the install —
        // partial cache is better than no SW at all.
        console.warn("[sw] precache partial:", err);
      }))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    // Purge old version caches.
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((n) => !n.startsWith(CACHE_VERSION))
          .map((n) => caches.delete(n))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // skip cross-origin

  // Static assets — cache-first, network fallback (good for offline + cheap repeats).
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) =>
        cached || fetch(req).then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(STATIC_CACHE).then((c) => c.put(req, clone));
          }
          return resp;
        }).catch(() => cached)
      )
    );
    return;
  }

  // HTML navigations — network-first so users always get fresh content when
  // online, fall back to cached page (then /offline) when network fails.
  const accepts = req.headers.get("accept") || "";
  if (req.mode === "navigate" || accepts.includes("text/html")) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(PAGES_CACHE).then((c) => c.put(req, clone));
        }
        return resp;
      }).catch(() =>
        caches.match(req).then((cached) => cached || caches.match("/offline"))
      )
    );
    return;
  }

  // Everything else (JSON APIs, etc.) — pass through, never cache.
});
