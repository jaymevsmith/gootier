"""Unit tests for the Stripe webhook price->tier reverse lookup used to keep
User.tier in sync for any legacy/pre-existing Stripe subscriptions.

Period-aware checkout helpers (_stripe_price_id_for_tier, _load_billing_tiers)
were removed along with the old Bronze/Silver/Gold subscription checkout —
Gootier bills via the Jhome Token Service wallet now (see templates/billing.html).
"""
from models import TierConfig
from routes import stripe_routes as sr


def _make_tier(db, tier, monthly_cents, monthly_pid=None, yearly_pid=None):
    row = TierConfig(
        tier=tier,
        monthly_price_cents=monthly_cents,
        stripe_price_id=monthly_pid,
        yearly_stripe_price_id=yearly_pid,
        is_active=True,
        sort_order=0,
    )
    db.add(row)
    db.commit()
    return row


def test_price_id_to_tier_key_includes_yearly(db):
    _make_tier(db, "gold", 4900, monthly_pid="price_m_gold", yearly_pid="price_y_gold")
    rev = sr._price_id_to_tier_key(db)
    assert rev["price_m_gold"] == "gold"
    assert rev["price_y_gold"] == "gold"
