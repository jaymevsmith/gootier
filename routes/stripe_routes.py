import logging
from datetime import datetime
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import TierConfig, User, log_action
from services.affiliates import affiliates
from services.env_config import get_env

logger = logging.getLogger("gootier.stripe")

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _stripe_secret() -> str: return get_env("STRIPE_SECRET_KEY", "")
def _stripe_webhook_secret() -> str: return get_env("STRIPE_WEBHOOK_SECRET", "")
def _app_url() -> str: return get_env("APP_URL", "http://localhost:8000").rstrip("/")


def _ensure_stripe_key() -> str:
    key = _stripe_secret()
    if not key:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    stripe.api_key = key
    return key

# Tier keys we render on the billing page (in this order).  "trial" is
# excluded because trial isn't a Stripe-purchasable plan — it's the default
# state for new accounts.
_BILLING_TIER_KEYS = ("bronze", "silver", "gold")


def _stripe_price_id_for_tier(db: Session, tier_key: str, period: str = "monthly") -> Optional[str]:
    """Look up a tier's Stripe Price ID for the given billing period.

    Monthly reads `tier_configs.stripe_price_id` first (admin-editable on
    /admin/plans), then falls back to the legacy `STRIPE_PRICE_BRONZE/SILVER/GOLD`
    env keys so older installs keep working.  Yearly reads
    `tier_configs.yearly_stripe_price_id` only — no env fallback, the feature is new.
    """
    row = db.query(TierConfig).filter(TierConfig.tier == tier_key).first()
    if period == "yearly":
        return (row.yearly_stripe_price_id or None) if row else None
    if row and row.stripe_price_id:
        return row.stripe_price_id
    legacy = get_env(f"STRIPE_PRICE_{tier_key.upper()}", "")
    return legacy or None


def _load_billing_tiers(db: Session) -> list[dict]:
    """All tier rows the billing page should render, in their configured sort
    order, normalized to the dict shape billing.html expects."""
    rows = (
        db.query(TierConfig)
        .filter(TierConfig.tier.in_(_BILLING_TIER_KEYS), TierConfig.is_active == True)  # noqa: E712
        .order_by(TierConfig.sort_order.asc())
        .all()
    )
    out = []
    for r in rows:
        legacy_env = f"STRIPE_PRICE_{r.tier.upper()}"
        price_id = r.stripe_price_id or get_env(legacy_env, "") or None
        monthly_cents = r.monthly_price_cents or 0
        out.append({
            "key": r.tier,
            "name": r.display_name or r.tier.title(),
            "blurb": r.blurb or "",
            "monthly_price_cents": monthly_cents,
            "yearly_price_cents": monthly_cents * 10,
            "features": r.features_list(),
            "price_id": price_id,
            "price_id_env": legacy_env,   # kept so the disabled-button tooltip can still hint
            "configured": bool(price_id),
            "yearly_price_id": r.yearly_stripe_price_id or None,
            "yearly_configured": bool(r.yearly_stripe_price_id),
        })
    return out


def _price_id_to_tier_key(db: Session) -> dict:
    """Reverse lookup for the Stripe webhook: price_id -> tier_key.

    Includes both the monthly and yearly price IDs so a yearly subscription
    resolves to the same tier as its monthly counterpart.
    """
    out: dict = {}
    for r in db.query(TierConfig).filter(TierConfig.tier.in_(_BILLING_TIER_KEYS)).all():
        monthly_pid = r.stripe_price_id or get_env(f"STRIPE_PRICE_{r.tier.upper()}", "")
        if monthly_pid:
            out[monthly_pid] = r.tier
        if r.yearly_stripe_price_id:
            out[r.yearly_stripe_price_id] = r.tier
    return out


# ------------------------------ Pages ------------------------------ #

@router.get("/billing")
async def billing_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tiers_view = _load_billing_tiers(db)
    for t in tiers_view:
        t["current"] = (user.tier == t["key"])
    from services.token_wallet import balance_tokens
    from services.env_config import get_env
    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "tiers": tiers_view,
        "stripe_configured": bool(_stripe_secret()),
        "has_subscription": bool(user.stripe_customer_id),
        "token_balance_display": balance_tokens(db, user) // 1000,
        "token_service_url": get_env("TOKEN_SERVICE_URL", "https://jhome-token-service-production.up.railway.app"),
        "token_service_app_slug": "gootier",
    })


# ------------------------------ Checkout ------------------------------ #

def _coupon_id_for_affiliate_code(code_info: dict) -> Optional[str]:
    """Get-or-create a Stripe Coupon in GOOTIER's own Stripe account matching
    the Jhome Affiliates hub's validate_code() response, and return its id.

    Coupon id is deterministic (derived from the code + terms) so repeat
    checkouts reuse the same Stripe Coupon object instead of creating a new
    one every time. Returns None if the code isn't valid or Stripe rejects it.
    """
    if not code_info.get("valid"):
        return None

    percent_off = code_info.get("percent_off")
    amount_off_cents = code_info.get("amount_off_cents")
    duration = code_info.get("duration") or "once"
    duration_months = code_info.get("duration_months")
    code = code_info.get("code") or ""

    if not percent_off and not amount_off_cents:
        logger.warning(
            "affiliates discount code %r validated but has neither percent_off "
            "nor amount_off_cents — skipping discount", code,
        )
        return None

    coupon_id = f"ja-{code}-{duration}-{percent_off or 0}-{amount_off_cents or 0}"
    coupon_id = coupon_id.lower().replace(" ", "_")[:64]

    try:
        return stripe.Coupon.retrieve(coupon_id).id
    except stripe.error.InvalidRequestError:
        pass  # doesn't exist yet — create it below

    params = {"id": coupon_id, "duration": duration}
    if duration == "repeating":
        params["duration_in_months"] = duration_months or 1
    if percent_off:
        params["percent_off"] = percent_off
    elif amount_off_cents:
        params["amount_off"] = amount_off_cents
        params["currency"] = "usd"

    try:
        coupon = stripe.Coupon.create(**params)
        return coupon.id
    except stripe.error.StripeError as e:
        logger.warning(
            "Stripe coupon create failed for affiliates discount code %r (coupon_id=%s): %s",
            code, coupon_id, e,
        )
        return None


@router.post("/api/billing/checkout")
async def create_checkout_session(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_stripe_key()
    body = await request.json()
    tier = body.get("tier")
    period = body.get("period", "monthly")
    discount_code = (body.get("discount_code") or "").strip()
    if period not in ("monthly", "yearly"):
        raise HTTPException(status_code=400, detail=f"Invalid billing period: {period!r}")
    price_id = _stripe_price_id_for_tier(db, tier, period) if tier in _BILLING_TIER_KEYS else None
    if not price_id:
        raise HTTPException(status_code=400, detail=f"Unknown or unconfigured {period} tier: {tier}")

    discounts = None
    if discount_code:
        code_info = affiliates.validate_code(discount_code)
        coupon_id = _coupon_id_for_affiliate_code(code_info)
        if coupon_id:
            discounts = [{"coupon": coupon_id}]

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer=user.stripe_customer_id or None,
            customer_email=None if user.stripe_customer_id else user.email,
            client_reference_id=str(user.id),
            metadata={"user_id": str(user.id), "tier": tier, "period": period},
            subscription_data={"metadata": {"user_id": str(user.id), "tier": tier, "period": period}},
            success_url=f"{_app_url()}/billing?upgraded=1",
            cancel_url=f"{_app_url()}/billing?cancelled=1",
            **({"discounts": discounts} if discounts else {}),
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if discounts:
        affiliates.report_redemption(discount_code)

    return {"url": session.url}


@router.post("/api/billing/tokens/checkout")
async def create_tokens_checkout(
    payload: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bundle_name = payload.get("bundle_name")
    if not bundle_name:
        raise HTTPException(status_code=400, detail="bundle_name is required")

    from services.token_wallet import ensure_wallet

    import httpx
    from services.env_config import get_env
    base_url = get_env("TOKEN_SERVICE_URL", "https://jhome-token-service-production.up.railway.app")
    try:
        wallet_id = ensure_wallet(db, user)
        resp = httpx.post(
            f"{base_url}/checkout",
            headers={"X-API-Key": get_env("TOKEN_SERVICE_API_KEY", "")},
            json={
                "wallet_id": wallet_id,
                "bundle_name": bundle_name,
                "success_url": f"{_app_url()}/billing?credits_added=1",
                "cancel_url": f"{_app_url()}/billing?credits_cancelled=1",
            },
            timeout=10,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "tokens checkout failed (JTS unreachable or ensure_wallet error): user=%s bundle_name=%s",
            user.id, bundle_name,
        )
        raise HTTPException(status_code=502, detail="Could not start checkout")

    if resp.status_code != 200:
        logger.error(
            "tokens checkout non-200 from JTS: user=%s bundle_name=%s status=%s body=%s",
            user.id, bundle_name, resp.status_code, resp.text[:500],
        )
        raise HTTPException(status_code=502, detail="Could not start checkout")
    return {"checkout_url": resp.json()["checkout_url"]}


@router.post("/api/billing/portal")
async def create_portal_session(user: User = Depends(get_current_user)):
    _ensure_stripe_key()
    if not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No subscription on file")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{_app_url()}/billing",
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"url": portal.url}


# ------------------------------ Webhook ------------------------------ #

@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    webhook_secret = _stripe_webhook_secret()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Stripe webhook not configured")
    _ensure_stripe_key()  # webhook handlers also call stripe.Subscription.retrieve
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook: {e}")

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        # Token-pack purchases now go through JTS's own checkout + webhook
        # entirely out of band — this handler only needs to cover Gootier's
        # own tier-subscription checkout (mode=subscription).
        #
        # No compatibility shim was added for the old local-ledger topup
        # Checkout flow (_handle_topup_completed, removed alongside the route
        # that created those sessions) even though a customer with an old
        # topup Checkout URL still open at deploy time could complete payment
        # after deploy and land here with no matching handler (this handler
        # silently no-ops for that session type, returning 200 OK per Stripe's
        # webhook contract, without granting anything). This is intentionally
        # not handled: Gootier is pre-launch with no real paying customers or
        # live topup sessions (see
        # docs/superpowers/specs/2026-07-17-jhome-token-service-migration-design.md),
        # so there is no possible in-flight old-style Checkout session for
        # this gap to affect. Revisit if this ever changes.
        _handle_checkout_completed(db, obj)
    elif etype == "customer.subscription.updated":
        _handle_subscription_updated(db, obj)
    elif etype == "customer.subscription.deleted":
        _handle_subscription_cancelled(db, obj)
    elif etype == "charge.refunded":
        _handle_charge_refunded(db, obj)

    return JSONResponse({"received": True})


def _resolve_user(db: Session, customer_id: str = "", user_id_str: str = "") -> Optional[User]:
    if user_id_str and user_id_str.isdigit():
        u = db.query(User).filter(User.id == int(user_id_str)).first()
        if u:
            return u
    if customer_id:
        return db.query(User).filter(User.stripe_customer_id == customer_id).first()
    return None


def _handle_checkout_completed(db: Session, session_obj: dict) -> None:
    metadata = session_obj.get("metadata") or {}
    user = _resolve_user(
        db,
        customer_id=session_obj.get("customer", "") or "",
        user_id_str=metadata.get("user_id", "") or session_obj.get("client_reference_id", "") or "",
    )
    if not user:
        return

    tier = metadata.get("tier")
    sub_id = session_obj.get("subscription")
    customer_id = session_obj.get("customer")

    if customer_id:
        user.stripe_customer_id = customer_id
    if tier and tier in _BILLING_TIER_KEYS:
        user.tier = tier
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            user.subscribed_until = datetime.utcfromtimestamp(sub["current_period_end"])
        except stripe.error.StripeError:
            pass
    db.commit()
    log_action(db, user, "SUBSCRIBE", "User", str(user.id),
               detail=f"Tier upgraded to {user.tier} via Stripe checkout")

    amount_total = session_obj.get("amount_total")
    # Report under the invoice id (in_...) when present — charge.refunded
    # reports charge["invoice"], and the hub matches invoice_id exactly, so
    # this is what lets a refunded first payment reverse the commission.
    payment_ref = session_obj.get("invoice") or session_obj.get("id") or ""
    if amount_total is not None:
        affiliates.report_payment(user.id, amount_total, payment_ref or f"sub-{user.id}")


def _handle_subscription_updated(db: Session, sub_obj: dict) -> None:
    user = _resolve_user(db, customer_id=sub_obj.get("customer", "") or "")
    if not user:
        return
    items = (sub_obj.get("items") or {}).get("data") or []
    if items:
        price_id = (items[0].get("price") or {}).get("id")
        mapped = _price_id_to_tier_key(db).get(price_id)
        if mapped:
            user.tier = mapped
    period_end = sub_obj.get("current_period_end")
    if period_end:
        user.subscribed_until = datetime.utcfromtimestamp(period_end)
    db.commit()
    log_action(db, user, "UPDATE", "User", str(user.id), detail="Subscription updated")


def _handle_subscription_cancelled(db: Session, sub_obj: dict) -> None:
    user = _resolve_user(db, customer_id=sub_obj.get("customer", "") or "")
    if not user:
        return
    user.tier = "trial"
    user.subscribed_until = None
    db.commit()
    log_action(db, user, "UPDATE", "User", str(user.id), detail="Subscription cancelled — reverted to trial")


def _handle_charge_refunded(db: Session, charge_obj: dict) -> None:
    """Report the refund to Jhome Affiliates.

    NOTE: Gootier had no pre-existing refund-handling business logic (no
    tier/credit reversal on refund) before this integration — this handler
    only adds affiliate-hub reporting and intentionally does not invent new
    tier-downgrade or credit-clawback behavior Gootier didn't already have.
    """
    invoice_id = charge_obj.get("invoice") or charge_obj.get("id") or ""
    if invoice_id:
        affiliates.report_refund(invoice_id)
