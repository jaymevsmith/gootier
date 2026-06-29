from datetime import datetime
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import CreditLedger, TierConfig, User, log_action
from services.credits import (
    balance as credits_balance, get_topup_pack, grant as credits_grant,
    list_topup_packs, recent_history as credits_history,
)
from services.env_config import get_env

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
    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "tiers": tiers_view,
        "stripe_configured": bool(_stripe_secret()),
        "has_subscription": bool(user.stripe_customer_id),
        "credit_balance": credits_balance(db, user),
        "credit_history": credits_history(db, user, limit=20),
        "topup_packs": list_topup_packs(db),
    })


# ------------------------------ Checkout ------------------------------ #

@router.post("/api/billing/checkout")
async def create_checkout_session(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_stripe_key()
    body = await request.json()
    tier = body.get("tier")
    price_id = _stripe_price_id_for_tier(db, tier) if tier in _BILLING_TIER_KEYS else None
    if not price_id:
        raise HTTPException(status_code=400, detail=f"Unknown or unconfigured tier: {tier}")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer=user.stripe_customer_id or None,
            customer_email=None if user.stripe_customer_id else user.email,
            client_reference_id=str(user.id),
            metadata={"user_id": str(user.id), "tier": tier},
            subscription_data={"metadata": {"user_id": str(user.id), "tier": tier}},
            success_url=f"{_app_url()}/billing?upgraded=1",
            cancel_url=f"{_app_url()}/billing?cancelled=1",
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"url": session.url}


@router.post("/api/billing/credits/checkout")
async def create_credits_checkout(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One-off Stripe Checkout for a credit pack (mode='payment', no recurring)."""
    _ensure_stripe_key()
    body = await request.json()
    pack_key = body.get("pack_key")
    pack = get_topup_pack(db, pack_key)
    if not pack:
        raise HTTPException(status_code=400, detail=f"Unknown credit pack: {pack_key!r}")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["price_cents"],
                    "product_data": {
                        "name": pack["label"],
                        "description": f"{pack['credits']} Gootier credits — never expires.",
                    },
                },
                "quantity": 1,
            }],
            customer=user.stripe_customer_id or None,
            customer_email=None if user.stripe_customer_id else user.email,
            client_reference_id=str(user.id),
            metadata={
                "user_id": str(user.id),
                "kind": "credit_pack",
                "pack_key": pack["key"],
                "credits": str(pack["credits"]),
            },
            payment_intent_data={
                "metadata": {
                    "user_id": str(user.id),
                    "kind": "credit_pack",
                    "pack_key": pack["key"],
                    "credits": str(pack["credits"]),
                },
            },
            success_url=f"{_app_url()}/billing?credits_added=1",
            cancel_url=f"{_app_url()}/billing?credits_cancelled=1",
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"url": session.url}


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
        # Branch on mode — credit packs use mode=payment, plans use mode=subscription.
        mode = obj.get("mode")
        metadata = obj.get("metadata") or {}
        if mode == "payment" or metadata.get("kind") == "credit_pack":
            _handle_topup_completed(db, obj)
        else:
            _handle_checkout_completed(db, obj)
    elif etype == "customer.subscription.updated":
        _handle_subscription_updated(db, obj)
    elif etype == "customer.subscription.deleted":
        _handle_subscription_cancelled(db, obj)

    return JSONResponse({"received": True})


def _handle_topup_completed(db: Session, session_obj: dict) -> None:
    metadata = session_obj.get("metadata") or {}
    user = _resolve_user(
        db,
        customer_id=session_obj.get("customer", "") or "",
        user_id_str=metadata.get("user_id", "") or session_obj.get("client_reference_id", "") or "",
    )
    if not user:
        return

    credits_str = (metadata.get("credits") or "").strip()
    if not credits_str.isdigit():
        return
    credits = int(credits_str)
    if credits <= 0:
        return

    session_id = session_obj.get("id") or ""
    if session_id:
        already = db.query(CreditLedger).filter(
            CreditLedger.stripe_session_id == session_id,
        ).first()
        if already:
            return  # idempotency — webhook re-delivery

    if session_obj.get("customer") and not user.stripe_customer_id:
        user.stripe_customer_id = session_obj["customer"]

    pack_key = metadata.get("pack_key", "topup")
    credits_grant(
        db, user, credits,
        reason=f"topup_{pack_key}",
        stripe_session_id=session_id,
        detail=f"Top-up via Stripe Checkout: {credits} credits ({pack_key})",
    )
    log_action(db, user, "TOPUP", "User", str(user.id),
               detail=f"+{credits} credits via {pack_key}")


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
