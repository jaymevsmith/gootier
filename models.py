import json
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, inspect, text,
)
from sqlalchemy.orm import relationship

from database import Base, engine


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="client", nullable=False)
    tier = Column(String, default="trial", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    trial_started_at = Column(DateTime, default=datetime.utcnow)
    subscribed_until = Column(DateTime, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    nickname = Column(String, nullable=True)
    reset_token = Column(String, nullable=True, index=True)
    reset_token_expires_at = Column(DateTime, nullable=True)
    verify_token = Column(String, nullable=True, index=True)
    verify_token_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    permissions: dict = {}

    def has_role(self, role: str) -> bool:
        return role in (self.role or "").split(",")

    def is_staff(self) -> bool:
        return any(self.has_role(r) for r in ("admin", "tech", "strategist"))

    def is_marketing(self) -> bool:
        return self.has_role("marketing")

    @property
    def tier_level(self) -> int:
        return {"trial": 0, "bronze": 1, "silver": 2, "gold": 3}.get(self.tier, 0)

    def perm(self, key: str) -> bool:
        if self.is_staff():
            return True
        return bool(self.permissions.get(key, False))


class TierConfig(Base):
    __tablename__ = "tier_configs"

    id = Column(Integer, primary_key=True)
    tier = Column(String, unique=True, nullable=False)
    perms_json = Column(Text, default="{}", nullable=False)
    quotas_json = Column(Text, default="{}", nullable=False)

    def perms_dict(self) -> dict:
        try:
            return json.loads(self.perms_json or "{}")
        except json.JSONDecodeError:
            return {}

    def quotas_dict(self) -> dict:
        try:
            return json.loads(self.quotas_json or "{}")
        except json.JSONDecodeError:
            return {}


class RoleConfig(Base):
    __tablename__ = "role_configs"

    id = Column(Integer, primary_key=True)
    role = Column(String, unique=True, nullable=False)
    perms_json = Column(Text, default="{}", nullable=False)

    def perms_dict(self) -> dict:
        try:
            return json.loads(self.perms_json or "{}")
        except json.JSONDecodeError:
            return {}


class SocialConnection(Base):
    __tablename__ = "social_connections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    platform = Column(String, nullable=False)
    account_name = Column(String, nullable=False)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_secret = Column(String, nullable=True)
    page_id = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", backref="social_connections")


class SocialPost(Base):
    __tablename__ = "social_posts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    image_url = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    link_url = Column(String, nullable=True)
    image_job_id = Column(Integer, ForeignKey("media_jobs.id"), nullable=True)
    video_job_id = Column(Integer, ForeignKey("media_jobs.id"), nullable=True)
    connection_ids = Column(String, nullable=False)  # comma-separated
    scheduled_at = Column(DateTime, nullable=True)
    status = Column(String, default="pending", nullable=False)
    publish_results = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True)
    ai_generated = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", backref="social_posts")


class EmailBlast(Base):
    __tablename__ = "email_blasts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    subject = Column(String, nullable=False)
    body_html = Column(Text, nullable=False)
    recipient_list = Column(Text, nullable=False)  # newline-separated emails
    recipient_count = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    scheduled_at = Column(DateTime, nullable=True)
    status = Column(String, default="pending", nullable=False)
    ai_generated = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", backref="email_blasts")


class CampaignContext(Base):
    __tablename__ = "campaign_contexts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    plan_text = Column(Text, nullable=False)
    schedule_text = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", backref="campaign_contexts")


class ActionLog(Base):
    __tablename__ = "action_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    user_name = Column(String, nullable=True)
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MediaAsset(Base):
    """User-owned reference images (mascot / person / product) reused across generations."""
    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    kind = Column(String, default="other", nullable=False)  # mascot | person | product | other
    file_url = Column(String, nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    mime_type = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", backref="media_assets")


class MediaJob(Base):
    """Async media generation job tracked from fal.ai webhook callbacks."""
    __tablename__ = "media_jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # image | video
    provider = Column(String, default="fal", nullable=False)
    model_key = Column(String, nullable=False)        # short key into MEDIA_MODEL_CATALOG
    model_endpoint = Column(String, nullable=False)   # fal endpoint id, e.g. fal-ai/kling-video/...
    prompt = Column(Text, nullable=False)
    ref_asset_ids = Column(String, nullable=True)     # comma-separated MediaAsset ids
    aspect_ratio = Column(String, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    status = Column(String, default="queued", nullable=False)  # queued|running|done|failed|cancelled
    fal_request_id = Column(String, nullable=True, index=True)
    result_url = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    cost_credits = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", backref="media_jobs")


class CreditLedger(Base):
    """Append-only ledger of credit grants and spends. Balance = SUM(delta)."""
    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    delta = Column(Integer, nullable=False)  # positive=grant, negative=spend
    reason = Column(String, nullable=False)  # monthly_grant_<tier>, topup_pack_<n>, image_gen, video_gen, refund, admin_adjust
    media_job_id = Column(Integer, ForeignKey("media_jobs.id"), nullable=True)
    stripe_session_id = Column(String, nullable=True, index=True)
    granted_for_month = Column(String, nullable=True, index=True)  # YYYY-MM for monthly_grant rows
    detail = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EnvConfig(Base):
    """Runtime env-var overrides editable from the admin panel.

    Values stored here supersede `os.environ` for known keys; the helper in
    services/env_config.py falls back to the OS env when a row is empty.
    """
    __tablename__ = "env_configs"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False, nullable=False)
    is_locked = Column(Boolean, default=False, nullable=False)
    group_name = Column(String, default="other", nullable=False)
    description = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_name = Column(String, nullable=True)


# --------------------------------------------------------------------------- #
# Idempotent migrations + seeds (run on every startup)
# --------------------------------------------------------------------------- #

_DEFAULT_TIER_PERMS = {
    "trial": {
        "marketing.view": True,
        "marketing.social_connect": True,
        "marketing.social_post": True,
        "marketing.email_blast": False,
        "marketing.ai_generate": True,
    },
    "bronze": {
        "marketing.view": True,
        "marketing.social_connect": True,
        "marketing.social_post": True,
        "marketing.email_blast": True,
        "marketing.ai_generate": True,
    },
    "silver": {
        "marketing.view": True,
        "marketing.social_connect": True,
        "marketing.social_post": True,
        "marketing.email_blast": True,
        "marketing.ai_generate": True,
    },
    "gold": {
        "marketing.view": True,
        "marketing.social_connect": True,
        "marketing.social_post": True,
        "marketing.email_blast": True,
        "marketing.ai_generate": True,
    },
}

_DEFAULT_TIER_QUOTAS = {
    "trial":  {"social_connections": 1, "posts_per_month": 10,  "blasts_per_month": 0,  "blast_recipients": 0,    "ai_generations_per_month": 5},
    "bronze": {"social_connections": 3, "posts_per_month": 60,  "blasts_per_month": 4,  "blast_recipients": 500,  "ai_generations_per_month": 30},
    "silver": {"social_connections": 6, "posts_per_month": 200, "blasts_per_month": 12, "blast_recipients": 2500, "ai_generations_per_month": 100},
    "gold":   {"social_connections": 20, "posts_per_month": 1000, "blasts_per_month": 50, "blast_recipients": 25000, "ai_generations_per_month": 1000},
}

_FORCE_OVERWRITE_KEYS = set()


# (key, group, is_secret, is_locked, description)
KNOWN_ENV_KEYS = [
    ("APP_URL",                "core",    False, False, "Public base URL for OAuth callbacks and email links. No trailing slash."),
    ("ALLOWED_ORIGINS",        "core",    False, False, "Comma-separated CORS origins."),
    ("SECRET_KEY",             "core",    True,  True,  "JWT signing secret. Locked — rotate via deploy only (changing invalidates all sessions)."),
    ("DATABASE_URL",           "core",    True,  True,  "SQLAlchemy connection string. Locked — changing live would break the app."),

    ("SMTP_HOST",              "email",   False, False, "SMTP server hostname (e.g. smtp.sendgrid.net)."),
    ("SMTP_PORT",              "email",   False, False, "SMTP port (587 for STARTTLS, 465 for SSL)."),
    ("SMTP_USERNAME",          "email",   False, False, "SMTP login username."),
    ("SMTP_PASSWORD",          "email",   True,  False, "SMTP login password."),
    ("FROM_EMAIL",             "email",   False, False, "Sender address used for verification, reset, and blast emails."),

    ("ANTHROPIC_API_KEY",      "ai",      True,  False, "Anthropic API key powering the AI campaign builder."),

    ("STRIPE_SECRET_KEY",      "billing", True,  False, "Stripe API secret key (server-side)."),
    ("STRIPE_PUBLISHABLE_KEY", "billing", False, False, "Stripe publishable key (safe for client-side)."),
    ("STRIPE_WEBHOOK_SECRET",  "billing", True,  False, "Stripe webhook signing secret for /webhooks/stripe."),
    ("STRIPE_PRICE_BRONZE",    "billing", False, False, "Stripe Price ID for the Bronze tier."),
    ("STRIPE_PRICE_SILVER",    "billing", False, False, "Stripe Price ID for the Silver tier."),
    ("STRIPE_PRICE_GOLD",      "billing", False, False, "Stripe Price ID for the Gold tier."),

    ("META_APP_ID",            "social",  False, False, "Facebook/Meta developer app ID."),
    ("META_APP_SECRET",        "social",  True,  False, "Facebook/Meta developer app secret."),
    ("META_OAUTH_REDIRECT",    "social",  False, False, "OAuth redirect URI registered with Meta. Must match exactly."),

    ("FAL_API_KEY",            "ai",      True,  False, "fal.ai API key powering image + video generation."),
]


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _upgrade_users(conn):
    if not _column_exists(conn, "users", "stripe_customer_id"):
        conn.execute(text("ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR"))
    if not _column_exists(conn, "users", "subscribed_until"):
        conn.execute(text("ALTER TABLE users ADD COLUMN subscribed_until DATETIME"))
    if not _column_exists(conn, "users", "reset_token"):
        conn.execute(text("ALTER TABLE users ADD COLUMN reset_token VARCHAR"))
    if not _column_exists(conn, "users", "reset_token_expires_at"):
        conn.execute(text("ALTER TABLE users ADD COLUMN reset_token_expires_at DATETIME"))
    if not _column_exists(conn, "users", "verify_token"):
        conn.execute(text("ALTER TABLE users ADD COLUMN verify_token VARCHAR"))
    if not _column_exists(conn, "users", "verify_token_expires_at"):
        conn.execute(text("ALTER TABLE users ADD COLUMN verify_token_expires_at DATETIME"))


def _upgrade_social_connections(conn):
    if not _column_exists(conn, "social_connections", "refresh_token"):
        conn.execute(text("ALTER TABLE social_connections ADD COLUMN refresh_token TEXT"))


def _upgrade_media(conn):
    """Future-proof column adds on media tables. The CREATE happens via
    Base.metadata.create_all; this is for additive schema changes."""
    if not _column_exists(conn, "social_posts", "video_url"):
        conn.execute(text("ALTER TABLE social_posts ADD COLUMN video_url VARCHAR"))
    if not _column_exists(conn, "social_posts", "image_job_id"):
        conn.execute(text("ALTER TABLE social_posts ADD COLUMN image_job_id INTEGER"))
    if not _column_exists(conn, "social_posts", "video_job_id"):
        conn.execute(text("ALTER TABLE social_posts ADD COLUMN video_job_id INTEGER"))


def _seed_tier_configs(db) -> None:
    for tier, perms in _DEFAULT_TIER_PERMS.items():
        existing = db.query(TierConfig).filter(TierConfig.tier == tier).first()
        quotas = _DEFAULT_TIER_QUOTAS[tier]
        if existing is None:
            db.add(TierConfig(
                tier=tier,
                perms_json=json.dumps(perms),
                quotas_json=json.dumps(quotas),
            ))
        else:
            current = existing.perms_dict()
            for key in _FORCE_OVERWRITE_KEYS:
                if key in perms:
                    current[key] = perms[key]
            for key, value in perms.items():
                current.setdefault(key, value)
            existing.perms_json = json.dumps(current)
            if not existing.quotas_json or existing.quotas_json == "{}":
                existing.quotas_json = json.dumps(quotas)
    db.commit()


def _seed_env_configs(db) -> None:
    """Ensure a row exists for every KNOWN_ENV_KEYS entry. Doesn't overwrite
    user-edited values. Pulls initial value from os.environ on first seed."""
    import os as _os
    for key, group, is_secret, is_locked, desc in KNOWN_ENV_KEYS:
        existing = db.query(EnvConfig).filter(EnvConfig.key == key).first()
        if existing:
            # Refresh metadata (description / group / locked / secret) in case
            # we change them in code — but never overwrite the value.
            existing.group_name = group
            existing.is_secret = is_secret
            existing.is_locked = is_locked
            existing.description = desc
            continue
        db.add(EnvConfig(
            key=key,
            value=_os.getenv(key, "") or None,
            is_secret=is_secret,
            is_locked=is_locked,
            group_name=group,
            description=desc,
        ))
    db.commit()


def _seed_role_configs(db) -> None:
    defaults = {
        "admin":      {"marketing.view": True, "marketing.email_blast": True, "marketing.social_post": True, "marketing.ai_generate": True, "admin.view": True},
        "marketing":  {"marketing.view": True, "marketing.email_blast": True, "marketing.social_post": True, "marketing.ai_generate": True},
        "tech":       {"admin.view": True},
        "strategist": {},
        "client":     {},
    }
    for role, perms in defaults.items():
        existing = db.query(RoleConfig).filter(RoleConfig.role == role).first()
        if existing is None:
            db.add(RoleConfig(role=role, perms_json=json.dumps(perms)))
        else:
            current = existing.perms_dict()
            for k, v in perms.items():
                current.setdefault(k, v)
            existing.perms_json = json.dumps(current)
    db.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _upgrade_users(conn)
        _upgrade_social_connections(conn)
        _upgrade_media(conn)
    from database import SessionLocal
    db = SessionLocal()
    try:
        _seed_tier_configs(db)
        _seed_role_configs(db)
        _seed_env_configs(db)
    finally:
        db.close()


def log_action(db, user: Optional[User], action: str, entity_type: str = None,
               entity_id: str = None, detail: str = None) -> None:
    db.add(ActionLog(
        user_id=user.id if user else None,
        user_name=user.username if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
    ))
    db.commit()
