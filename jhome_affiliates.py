"""Jhome Affiliates integration kit — drop this single file into any product.

Usage:
    from jhome_affiliates import AffiliatesClient
    affiliates = AffiliatesClient()   # reads env: JHOME_AFFILIATES_URL,
                                      # JHOME_AFFILIATES_PRODUCT, JHOME_AFFILIATES_KEY

    affiliates.report_signup(user.id, ref_code=user.referral_code)
    affiliates.report_payment(user.id, invoice.amount_paid, invoice.id)
    affiliates.report_refund(invoice.id)
    info = affiliates.validate_code("JAYS10")   # {"valid": bool, "percent_off": ...}

Every call is safe: unconfigured or unreachable hub -> no-op / {"valid": False}.
Never raises. Never blocks checkout.

Fork safety: the HTTP client is created lazily on first use (not in __init__),
which keeps it safe under gunicorn's pre-fork worker model — but only if you
don't call any AffiliatesClient method before your app server forks workers
(e.g. in a pre-fork startup hook). Doing so would share one client/socket
across all forked workers.
"""
import hashlib
import hmac
import json
import logging
import os
import urllib.parse

import httpx

log = logging.getLogger("jhome_affiliates")

_TIMEOUT = 5.0
_RETRIES = 2  # httpx transport-level connect retries


class AffiliatesClient:
    def __init__(self, hub_url: str = None, product_slug: str = None, api_key: str = None):
        self.hub_url = (hub_url if hub_url is not None
                        else os.getenv("JHOME_AFFILIATES_URL", "")).rstrip("/")
        self.product_slug = (product_slug if product_slug is not None
                             else os.getenv("JHOME_AFFILIATES_PRODUCT", ""))
        self.api_key = (api_key if api_key is not None
                        else os.getenv("JHOME_AFFILIATES_KEY", ""))
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.hub_url and self.product_slug and self.api_key)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.hub_url, timeout=_TIMEOUT,
                transport=httpx.HTTPTransport(retries=_RETRIES))
        return self._client

    def _post_event(self, payload: dict) -> bool:
        if not self.enabled:
            return False
        body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(self.api_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
        try:
            r = self._http().post("/api/v1/events", content=body, headers={
                "Content-Type": "application/json",
                "X-JA-Product": self.product_slug,
                "X-JA-Signature": sig,
            })
            return r.status_code == 200
        except Exception as exc:  # noqa: BLE001 — the hub must never break the product
            log.warning("jhome_affiliates event failed: %s", exc)
            return False

    def report_signup(self, external_user_id, ref_code=None, discount_code=None) -> bool:
        payload = {"type": "signup", "external_user_id": str(external_user_id)}
        if ref_code:
            payload["ref_code"] = str(ref_code)
        if discount_code:
            payload["discount_code"] = str(discount_code)
        return self._post_event(payload)

    def report_payment(self, external_user_id, gross_cents: int, invoice_id: str) -> bool:
        return self._post_event({
            "type": "payment", "external_user_id": str(external_user_id),
            "gross_cents": int(gross_cents), "invoice_id": str(invoice_id),
        })

    def report_refund(self, invoice_id: str) -> bool:
        return self._post_event({"type": "refund", "invoice_id": str(invoice_id)})

    def report_redemption(self, code: str) -> bool:
        return self._post_event({"type": "redemption", "code": str(code)})

    def validate_code(self, code: str) -> dict:
        """Fail-closed: anything but a clean 200 -> {"valid": False}."""
        if not self.enabled or not code:
            return {"valid": False}
        try:
            r = self._http().get(f"/api/v1/codes/{urllib.parse.quote(code, safe='')}",
                                 params={"product": self.product_slug})
            if r.status_code == 200:
                return r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("jhome_affiliates code check failed: %s", exc)
        return {"valid": False}
