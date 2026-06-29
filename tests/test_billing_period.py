"""Unit tests for period-aware billing helpers in routes/stripe_routes.py."""
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


def test_price_id_monthly_returns_monthly(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid="price_y_bronze")
    assert sr._stripe_price_id_for_tier(db, "bronze", "monthly") == "price_m_bronze"


def test_price_id_yearly_returns_yearly(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid="price_y_bronze")
    assert sr._stripe_price_id_for_tier(db, "bronze", "yearly") == "price_y_bronze"


def test_price_id_yearly_unconfigured_returns_none(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid=None)
    assert sr._stripe_price_id_for_tier(db, "bronze", "yearly") is None


def test_load_billing_tiers_computes_yearly(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid="price_y_bronze")
    tiers = sr._load_billing_tiers(db)
    assert len(tiers) == 1
    t = tiers[0]
    assert t["yearly_price_cents"] == 9000          # 900 * 10
    assert t["yearly_price_id"] == "price_y_bronze"
    assert t["yearly_configured"] is True


def test_load_billing_tiers_yearly_unconfigured(db):
    _make_tier(db, "bronze", 900, monthly_pid="price_m_bronze", yearly_pid=None)
    t = sr._load_billing_tiers(db)[0]
    assert t["yearly_price_cents"] == 9000
    assert t["yearly_configured"] is False


def test_price_id_to_tier_key_includes_yearly(db):
    _make_tier(db, "gold", 4900, monthly_pid="price_m_gold", yearly_pid="price_y_gold")
    rev = sr._price_id_to_tier_key(db)
    assert rev["price_m_gold"] == "gold"
    assert rev["price_y_gold"] == "gold"
