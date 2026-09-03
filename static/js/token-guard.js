// House rule: a task costing more tokens than the user holds sends them to this
// app's own token purchase page, instead of failing with a bare error.
//
// Wraps window.fetch once, so every existing call site is covered without
// editing them. Two guards, both from real incidents:
//   1. Only a definite, server-confirmed shortfall redirects. The server alone
//      decides (an unknown balance must never redirect); this file only obeys.
//   2. The balance endpoint and the purchase page itself are exempt. Without
//      that, a 402 from the token badge's own poll bounces to billing, which
//      reloads, which polls again -- an infinite reload loop that locks the
//      user out of the app entirely.
(function () {
  if (window.__tokenGuardInstalled) return;
  window.__tokenGuardInstalled = true;

  var EXEMPT = /\/(api\/)?(billing\/tokens|tokens\/balance|tokens)(\/|\?|$)/;
  var origFetch = window.fetch.bind(window);

  function samePath(url) {
    try { return new URL(url, location.origin).pathname === location.pathname; }
    catch (e) { return false; }
  }

  function goBuy(message, url) {
    var el = document.createElement("div");
    el.setAttribute("role", "status");
    el.textContent = message + " Taking you to the tokens page.";
    el.style.cssText =
      "position:fixed;left:50%;top:18px;transform:translateX(-50%);z-index:99999;" +
      "max-width:min(92vw,540px);padding:12px 18px;border-radius:12px;" +
      "background:#141720;color:#f2f4f8;border:1px solid #2a3040;text-align:center;" +
      "font:600 16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;" +
      "box-shadow:0 8px 28px rgba(0,0,0,.35)";
    (document.body || document.documentElement).appendChild(el);
    setTimeout(function () { location.href = url; }, 1400);
  }

  window.fetch = function (input, init) {
    var reqUrl = typeof input === "string" ? input : (input && input.url) || "";
    return origFetch(input, init).then(function (resp) {
      if (resp.status !== 402 || EXEMPT.test(reqUrl)) return resp;
      return resp.clone().json().then(function (body) {
        var d = (body && body.detail) || body || {};
        if (d.error !== "insufficient_tokens" || !d.purchase_url) return resp;
        if (samePath(d.purchase_url)) return resp;
        var sep = d.purchase_url.indexOf("?") < 0 ? "?" : "&";
        var back = encodeURIComponent(location.pathname + location.search);
        goBuy(d.message || "You do not have enough tokens for that.",
              d.purchase_url + sep + "return_to=" + back);
        return new Promise(function () {});   // navigation underway; never resolves
      }).catch(function () { return resp; });
    });
  };
})();
