from datetime import datetime
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User, log_action
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

TIERS = [
    {"key": "bronze", "name": "Bronze", "blurb": "For solo brands getting started.",
     "features": ["3 social accounts", "60 posts/month", "4 email blasts/month",
                  "30 AI generations/month", "Email support"],
     "price_id_env": "STRIPE_PRICE_BRONZE"},
    {"key": "silver", "name": "Silver", "blurb": "For growing teams shipping weekly.",
     "features": ["6 social accounts", "200 posts/month", "12 email blasts/month",
                  "100 AI generations/month", "Priority support"],
     "price_id_env": "STRIPE_PRICE_SILVER"},
    {"key": "gold", "name": "Gold", "blurb": "For agencies running many brands.",
     "features": ["20 social accounts", "1000 posts/month", "50 email blasts/month",
                  "1000 AI generations/month", "Dedicated support"],
     "price_id_env": "STRIPE_PRICE_GOLD"},
]


def _price_to_tier_map() -> dict:
    return {get_env(t["price_id_env"], ""): t["key"] for t in TIERS if get_env(t["price_id_env"])}


def _tier_to_price(tier: str) -> Optional[str]:
    for t in TIERS:
        if t["key"] == tier:
            return get_env(t["price_id_env"]) or None
    return None


# ------------------------------ Pages ------------------------------ #

@router.get("/billing")
async def billing_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    tiers_view = []
    for t in TIERS:
        tiers_view.append({
            **t,
            "configured": bool(get_env(t["price_id_env"])),
            "current": user.tier == t["key"],
        })
    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "tiers": tiers_view,
        "stripe_configured": bool(_stripe_secret()),
        "has_subscription": bool(user.stripe_customer_id),
    })


# ------------------------------ Checkout ------------------------------ #

@router.post("/api/billing/checkout")
async def create_checkout_session(
    request: Request,
    user: User = Depends(get_current_user),
):
    _ensure_stripe_key()
    body = await request.json()
    tier = body.get("tier")
    price_id = _tier_to_price(tier)
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
            success_url=f"{APP_URL}/billing?upgraded=1",
            cancel_url=f"{APP_URL}/billing?cancelled=1",
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
        _handle_checkout_completed(db, obj)
    elif etype == "customer.subscription.updated":
        _handle_subscription_updated(db, obj)
    elif etype == "customer.subscription.deleted":
        _handle_subscription_cancelled(db, obj)

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
    if tier and tier in {t["key"] for t in TIERS}:
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
        mapped = _price_to_tier_map().get(price_id)
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
