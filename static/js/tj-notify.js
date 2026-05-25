/* Gootier — TJ.* global notification namespace.
   Mirrors the Trading Jay tj-notify.js contract:
     - TJ.notify.{success,error,warning,info,alert}
     - TJ.progress.{start,update,done,fail}
     - TJ.btnLoading(btn, label)
     - TJ.api.{get,post,put,patch,del}
     - TJ.confirm(msg, opts)          — promise-based modal, replaces native confirm()
     - TJ.flash(kind, msg)            — fire a toast that survives a page redirect
     - TJ.task(label)                 — multi-step progress wrapper

   Rules:
     - Never use alert() — use TJ.notify.error / warning
     - Never use confirm() — use await TJ.confirm()
     - Validation uses TJ.notify.warning(); system failures use error
     - Long ops (>1s) should pass { progress: true } to TJ.api.*
     - The api wrapper returns null on any failure — guard: if (!data) return;
*/
(function () {
  const TJ = window.TJ = window.TJ || {};

  // ---------- Toasts ----------
  function buildToast(msg, kind, opts) {
    const c = document.getElementById('tj-toast-container');
    if (!c) return null;
    opts = opts || {};
    const div = document.createElement('div');
    div.className = `tj-toast tj-toast-${kind}`;

    const close = document.createElement('button');
    close.className = 'tj-toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.innerHTML = '<i class="fas fa-xmark"></i>';
    close.addEventListener('click', () => dismiss(div));
    div.appendChild(close);

    if (opts.title) {
      const t = document.createElement('div');
      t.className = 'tj-toast-title';
      t.textContent = opts.title;
      div.appendChild(t);
    }
    const body = document.createElement('div');
    body.className = 'tj-toast-body';
    body.textContent = msg;
    div.appendChild(body);

    c.appendChild(div);
    const dur = opts.duration !== undefined ? opts.duration : 3500;
    if (dur > 0) setTimeout(() => dismiss(div), dur);
    return div;
  }
  function dismiss(div) {
    if (!div) return;
    div.style.opacity = '0';
    div.style.transform = 'translateY(8px)';
    setTimeout(() => div.remove(), 200);
  }

  TJ.notify = {
    success: (msg, opts) => buildToast(msg, 'success', opts),
    error:   (msg, opts) => buildToast(msg, 'error',   opts),
    warning: (msg, opts) => buildToast(msg, 'warning', Object.assign({ duration: 6000 }, opts || {})),
    info:    (msg, opts) => buildToast(msg, 'info',    opts),
    alert(target, msg, kind) {
      const el = typeof target === 'string' ? document.getElementById(target) : target;
      if (!el) return;
      if (!msg) {
        el.classList.add('d-none');
        el.textContent = '';
        return;
      }
      el.classList.remove('d-none');
      el.className = `alert alert-${kind || 'warning'}`;
      el.textContent = msg;
    },
    dismiss,
  };

  // ---------- Progress bar ----------
  const bar = () => document.getElementById('tj-progress-bar');
  TJ.progress = {
    start(label) {
      const b = bar(); if (!b) return;
      b.classList.remove('tj-success', 'tj-failure');
      b.classList.add('tj-active');
      b.style.width = '15%';
      setTimeout(() => { b.style.width = '80%'; }, 50);
      if (label) TJ.notify.info(label, { duration: 1500 });
    },
    update(pct, label) {
      const b = bar(); if (!b) return;
      b.style.width = `${Math.min(100, Math.max(0, pct))}%`;
      if (label) TJ.notify.info(label, { duration: 1500 });
    },
    done(msg) {
      const b = bar(); if (!b) return;
      b.classList.add('tj-success');
      b.style.width = '100%';
      if (msg) TJ.notify.success(msg);
      setTimeout(() => { b.classList.remove('tj-active'); b.style.width = '0'; }, 400);
    },
    fail(msg) {
      const b = bar(); if (!b) return;
      b.classList.add('tj-failure');
      b.style.width = '100%';
      if (msg) TJ.notify.error(msg);
      setTimeout(() => { b.classList.remove('tj-active'); b.style.width = '0'; }, 600);
    },
  };

  // ---------- Button loading ----------
  TJ.btnLoading = function (btn, label) {
    if (!btn) return () => {};
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-2"></span>${label || 'Working…'}`;
    return () => { btn.disabled = false; btn.innerHTML = original; };
  };

  // ---------- API wrapper ----------
  async function call(method, url, body, opts) {
    opts = opts || {};
    if (opts.progress) TJ.progress.start(opts.progressLabel);
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'same-origin',
      });
    } catch (e) {
      if (opts.progress) TJ.progress.fail();
      TJ.notify.error('Network error — check your connection');
      return null;
    }
    if (res.status === 401) {
      window.location.href = '/login';
      return null;
    }
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const detail = (data && data.detail) || `Request failed (${res.status})`;
      // 403 quota messages — show as warning so they read clearer
      const kind = res.status === 403 ? 'warning' : 'error';
      if (opts.progress) TJ.progress.fail();
      TJ.notify[kind](detail);
      return null;
    }
    if (opts.progress) TJ.progress.done(opts.successMsg || null);
    else if (opts.successMsg) TJ.notify.success(opts.successMsg);
    return data;
  }

  TJ.api = {
    get:   (url, opts)        => call('GET',    url, null, opts),
    post:  (url, body, opts)  => call('POST',   url, body, opts),
    put:   (url, body, opts)  => call('PUT',    url, body, opts),
    patch: (url, body, opts)  => call('PATCH',  url, body, opts),
    del:   (url, opts)        => call('DELETE', url, null, opts),
  };

  // ---------- Promise-based confirm modal ----------
  function ensureConfirmModal() {
    let el = document.getElementById('tj-confirm-modal');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'tj-confirm-modal';
    el.className = 'modal fade';
    el.tabIndex = -1;
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML = `
      <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title" id="tj-confirm-title">Are you sure?</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body" id="tj-confirm-body"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-soft" id="tj-confirm-cancel">Cancel</button>
            <button type="button" class="btn" id="tj-confirm-ok">Confirm</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(el);
    return el;
  }

  TJ.confirm = function (message, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const el = ensureConfirmModal();
      el.querySelector('#tj-confirm-title').textContent = opts.title || 'Are you sure?';
      el.querySelector('#tj-confirm-body').textContent = message;
      const okBtn = el.querySelector('#tj-confirm-ok');
      const cancelBtn = el.querySelector('#tj-confirm-cancel');
      okBtn.textContent = opts.okLabel || 'Confirm';
      cancelBtn.textContent = opts.cancelLabel || 'Cancel';
      okBtn.className = `btn ${opts.danger ? 'btn-danger' : 'tj-grad-btn'}`;

      const modal = bootstrap.Modal.getOrCreateInstance(el);
      let settled = false;
      const finish = (val) => {
        if (settled) return;
        settled = true;
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        el.removeEventListener('hidden.bs.modal', onHidden);
        modal.hide();
        resolve(val);
      };
      const onOk = () => finish(true);
      const onCancel = () => finish(false);
      const onHidden = () => finish(false);

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      el.addEventListener('hidden.bs.modal', onHidden);
      modal.show();
    });
  };

  // ---------- Multi-step task wrapper ----------
  TJ.task = function (label) {
    TJ.progress.start(label);
    return {
      step(msg, pct) {
        if (typeof pct === 'number') TJ.progress.update(pct, msg);
        else if (msg) TJ.notify.info(msg, { duration: 1500 });
      },
      done(msg) { TJ.progress.done(msg); },
      fail(msg) { TJ.progress.fail(msg); },
    };
  };

  // ---------- Server-flash bridge ----------
  // Server sets a `_tj_flash` cookie (JSON: {kind, msg}). Client reads it on
  // page load, fires the toast, then clears the cookie. Survives PRG redirects.
  function readCookie(name) {
    const m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }
  function clearCookie(name) {
    document.cookie = `${name}=; Path=/; Max-Age=0; SameSite=Lax`;
  }
  TJ.flash = function (kind, msg) {
    // Client-side helper: same shape, fires immediately.
    (TJ.notify[kind] || TJ.notify.info)(msg);
  };

  document.addEventListener('DOMContentLoaded', () => {
    const raw = readCookie('_tj_flash');
    if (!raw) return;
    clearCookie('_tj_flash');
    try {
      const data = JSON.parse(raw);
      if (data && data.msg) (TJ.notify[data.kind] || TJ.notify.info)(data.msg);
    } catch (_) {}
  });
})();
