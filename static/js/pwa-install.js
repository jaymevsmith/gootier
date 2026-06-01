/**
 * PWA install prompt UX.
 *
 * Two flows:
 *   - Chrome / Edge / Samsung Internet: capture `beforeinstallprompt`,
 *     hold it back, surface our custom banner instead, and call
 *     `prompt()` on tap.
 *   - iOS Safari: no programmatic install — show an illustrated
 *     "Tap Share → Add to Home Screen" hint instead.
 *
 * Banner shows after the user has visited 3+ sessions (proves intent).
 * Dismissal sticks for 30 days.  Hidden entirely once installed.
 */

(function () {
  // -------- Skip entirely if already running as a PWA. --------
  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;   // iOS Safari fallback
  if (isStandalone) return;

  // -------- Session-count gate. --------
  const sessionKey = "gootier_pwa_sessions";
  const dismissedKey = "gootier_pwa_dismissed_at";
  const MIN_SESSIONS = 3;
  const DISMISS_DAYS = 30;

  // Increment once per browser session (not once per page).
  if (!sessionStorage.getItem("gootier_session_counted")) {
    sessionStorage.setItem("gootier_session_counted", "1");
    const n = parseInt(localStorage.getItem(sessionKey) || "0", 10) + 1;
    localStorage.setItem(sessionKey, String(n));
  }
  const sessionCount = parseInt(localStorage.getItem(sessionKey) || "0", 10);

  const dismissedAt = parseInt(localStorage.getItem(dismissedKey) || "0", 10);
  const dismissedDays = dismissedAt
    ? (Date.now() - dismissedAt) / (1000 * 60 * 60 * 24)
    : Infinity;

  if (sessionCount < MIN_SESSIONS) return;
  if (dismissedDays < DISMISS_DAYS) return;

  // -------- Detect platform. --------
  const ua = navigator.userAgent;
  const isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
  const isSafariOnIOS = isIOS && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);

  // -------- Chrome-family path: stash `beforeinstallprompt`. --------
  let deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredPrompt = e;
    showBanner({ mode: "chrome" });
  });

  // iOS Safari has no install event — fire the iOS-specific banner directly.
  if (isSafariOnIOS) {
    // Wait until DOM is interactive so the banner can attach.
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => showBanner({ mode: "ios" }));
    } else {
      showBanner({ mode: "ios" });
    }
  }

  // -------- Banner UI. --------
  function showBanner({ mode }) {
    if (document.getElementById("pwa-install-banner")) return;  // already shown

    const banner = document.createElement("div");
    banner.id = "pwa-install-banner";
    banner.setAttribute("role", "region");
    banner.setAttribute("aria-label", "Install Gootier to home screen");
    banner.innerHTML = mode === "ios" ? iosBannerHTML() : chromeBannerHTML();

    document.body.appendChild(banner);

    banner.querySelector("[data-pwa-dismiss]").addEventListener("click", () => {
      localStorage.setItem(dismissedKey, String(Date.now()));
      banner.remove();
    });

    if (mode === "chrome") {
      banner.querySelector("[data-pwa-install]").addEventListener("click", async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        const choice = await deferredPrompt.userChoice;
        if (choice.outcome === "accepted") {
          banner.remove();
          // Don't even mark dismissed — they installed.
        } else {
          // User said no — treat as dismissed for 30 days.
          localStorage.setItem(dismissedKey, String(Date.now()));
          banner.remove();
        }
        deferredPrompt = null;
      });
    }
  }

  function chromeBannerHTML() {
    return `
      <div class="pwa-banner-inner">
        <img src="/static/icons/icon-192.png" alt="" class="pwa-banner-icon">
        <div class="pwa-banner-body">
          <div class="pwa-banner-title">Install Gootier</div>
          <div class="pwa-banner-sub">Add to your home screen for push notifications and faster access.</div>
        </div>
        <div class="pwa-banner-actions">
          <button type="button" class="btn btn-soft btn-sm" data-pwa-dismiss>Not now</button>
          <button type="button" class="btn tj-grad-btn btn-sm" data-pwa-install>
            <i class="fas fa-plus me-1"></i>Install
          </button>
        </div>
      </div>`;
  }

  function iosBannerHTML() {
    // iOS users have to do the install manually via the share sheet — show a
    // brief illustrated hint pointing at where the Share button sits in Safari.
    return `
      <div class="pwa-banner-inner">
        <img src="/static/icons/icon-192.png" alt="" class="pwa-banner-icon">
        <div class="pwa-banner-body">
          <div class="pwa-banner-title">Install Gootier</div>
          <div class="pwa-banner-sub">
            Tap <i class="fas fa-arrow-up-from-bracket"></i> Share, then
            <strong>Add to Home Screen</strong>.
          </div>
        </div>
        <div class="pwa-banner-actions">
          <button type="button" class="btn btn-soft btn-sm" data-pwa-dismiss>Got it</button>
        </div>
      </div>`;
  }

  // -------- Banner styles (inline so this works on every page). --------
  const styles = document.createElement("style");
  styles.textContent = `
    #pwa-install-banner {
      position: fixed;
      left: 12px; right: 12px;
      bottom: calc(env(safe-area-inset-bottom, 0) + 12px);
      z-index: 1080;
      background: #141720;
      border: 1px solid #1e2535;
      border-radius: 14px;
      box-shadow: 0 16px 40px rgba(0,0,0,0.5);
      color: #e8ebf0;
      animation: pwa-banner-in 220ms ease-out;
      max-width: 560px;
      margin: 0 auto;
    }
    @keyframes pwa-banner-in {
      from { transform: translateY(120%); opacity: 0; }
      to   { transform: translateY(0);    opacity: 1; }
    }
    .pwa-banner-inner {
      display: grid;
      grid-template-columns: 48px 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
    }
    .pwa-banner-icon { width: 48px; height: 48px; border-radius: 10px; }
    .pwa-banner-title { font-weight: 700; font-size: 0.95rem; line-height: 1.2; }
    .pwa-banner-sub   { font-size: 0.78rem; color: #a0aec0; margin-top: 2px; }
    .pwa-banner-actions { display: flex; gap: 6px; }
    @media (max-width: 480px) {
      .pwa-banner-inner {
        grid-template-columns: 40px 1fr;
        grid-template-areas:
          "icon  body"
          "acts  acts";
      }
      .pwa-banner-icon { width: 40px; height: 40px; grid-area: icon; }
      .pwa-banner-body { grid-area: body; }
      .pwa-banner-actions { grid-area: acts; justify-content: flex-end; margin-top: 4px; }
    }
  `;
  document.head.appendChild(styles);
})();
