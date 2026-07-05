import json
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, inspect, text,
)
from sqlalchemy.orm import relationship

from database import Base, engine
# NOTE: services.secrets is imported lazily inside _encrypt_existing_tokens to
# avoid a circular import (services.secrets reads from env_config which reads
# from models).


def _EncryptedText():
    """Lazy proxy — at class-definition time we can't import services.secrets
    (circular). At the moment the SocialConnection columns are *bound* by
    SQLAlchemy, we need the actual TypeDecorator. Solution: a function we call
    once at module-load time AFTER everything is wired up. We call it here at
    import end after models classes are declared."""
    from services.secrets import EncryptedString
    return EncryptedString()


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
    calendar_token = Column(String, nullable=True, index=True)
    # Stable Google account ID for users who signed in via "Continue with Google".
    # Populated on first Google-auth callback; used to re-link the account if the
    # user later changes the email on their Google profile.
    google_sub = Column(String, nullable=True, index=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Jhome Affiliates referral code captured from ?ref= at signup (nullable —
    # most users won't have one). See routes/auth_routes.py signup_submit.
    referral_code = Column(String, nullable=True)

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

    # Billing-page display fields (Admin → Plans).  Adding columns here
    # instead of a new table keeps tier metadata in one row per tier and
    # lets the existing permissions/quotas editor + the new plans editor
    # share a join-free read path.
    display_name = Column(String, nullable=True)            # "Bronze"
    blurb = Column(String, nullable=True)                   # "For solo brands getting started."
    monthly_price_cents = Column(Integer, nullable=True)    # 900 for $9.00 (display only — Stripe is source of truth)
    stripe_price_id = Column(String, nullable=True)         # "price_..."
    yearly_stripe_price_id = Column(String, nullable=True)  # "price_..." (yearly interval; display = monthly_price_cents * 10)
    features_json = Column(Text, default="[]", nullable=False)  # JSON array of bullet strings
    monthly_credit_grant = Column(Integer, default=0, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

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

    def features_list(self) -> list:
        try:
            data = json.loads(self.features_json or "[]")
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


class TopupPackConfig(Base):
    """One row per credit pack offered on the billing page.

    Prices are denormalized cents (USD) — they get passed straight into
    Stripe's inline `price_data` so there's no Stripe Price object to
    keep in sync.  Admin edits go live the next time /billing is loaded;
    no app restart needed.
    """
    __tablename__ = "topup_pack_configs"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)   # "pack_500"
    label = Column(String, nullable=False)              # "Starter — 500 credits"
    credits = Column(Integer, nullable=False)           # 500
    price_cents = Column(Integer, nullable=False)       # 500 → $5.00
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)


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
    # Encrypted at rest. EncryptedString transparently encrypts on write and
    # decrypts on read; legacy plaintext rows keep working until rewritten.
    access_token = Column("access_token", _EncryptedText(), nullable=False)
    refresh_token = Column("refresh_token", _EncryptedText(), nullable=True)
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
    analytics_json = Column(Text, nullable=True)  # latest per-connection metrics snapshot
    analytics_fetched_at = Column(DateTime, nullable=True)
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
    webhook_token = Column(String, nullable=True, index=True)
    compose_meta_json = Column(Text, nullable=True)  # JSON for video-compose jobs
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
    ("TOKEN_ENCRYPTION_KEY",   "core",    True,  True,  "Fernet key for at-rest encryption of OAuth tokens. Locked — rotating without re-encrypting existing rows will lock you out of every social connection. Defaults to a value derived from SECRET_KEY when unset."),

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

    ("META_APP_ID",            "social",  False, False, "Facebook/Meta developer app ID (used for Facebook + Instagram OAuth)."),
    ("META_APP_SECRET",        "social",  True,  False, "Facebook/Meta developer app secret."),
    ("META_OAUTH_REDIRECT",    "social",  False, False, "Facebook OAuth redirect URI registered with Meta. Must match exactly."),

    ("X_CLIENT_ID",            "social",  False, False, "X (Twitter) OAuth 2.0 client ID. Requires the paid Basic tier or higher for write access."),
    ("X_CLIENT_SECRET",        "social",  True,  False, "X (Twitter) OAuth 2.0 client secret."),
    ("X_OAUTH_REDIRECT",       "social",  False, False, "X OAuth redirect URI, e.g. https://yourhost/oauth/twitter/callback"),

    ("LINKEDIN_CLIENT_ID",     "social",  False, False, "LinkedIn OAuth 2.0 client ID."),
    ("LINKEDIN_CLIENT_SECRET", "social",  True,  False, "LinkedIn OAuth 2.0 client secret."),
    ("LINKEDIN_OAUTH_REDIRECT","social",  False, False, "LinkedIn OAuth redirect URI, e.g. https://yourhost/oauth/linkedin/callback"),

    ("TIKTOK_CLIENT_KEY",      "social",  False, False, "TikTok developer client key."),
    ("TIKTOK_CLIENT_SECRET",   "social",  True,  False, "TikTok developer client secret."),
    ("TIKTOK_OAUTH_REDIRECT",  "social",  False, False, "TikTok OAuth redirect URI, e.g. https://yourhost/oauth/tiktok/callback"),

    ("YOUTUBE_CLIENT_ID",      "social",  False, False, "Google OAuth 2.0 client ID for YouTube uploads. From Google Cloud Console → Credentials."),
    ("YOUTUBE_CLIENT_SECRET",  "social",  True,  False, "Google OAuth 2.0 client secret."),
    ("YOUTUBE_OAUTH_REDIRECT", "social",  False, False, "YouTube OAuth redirect URI, e.g. https://yourhost/oauth/youtube/callback"),

    ("GOOGLE_AUTH_CLIENT_ID",     "auth",   False, False, "Google OAuth client ID for Sign in with Google (account auth, NOT YouTube uploads). If blank, falls back to YOUTUBE_CLIENT_ID. From Google Cloud Console → Credentials."),
    ("GOOGLE_AUTH_CLIENT_SECRET", "auth",   True,  False, "Google OAuth client secret for Sign in with Google. Falls back to YOUTUBE_CLIENT_SECRET if blank."),
    ("GOOGLE_AUTH_REDIRECT",      "auth",   False, False, "Sign in with Google redirect URI, e.g. https://yourhost/oauth/google/callback. Must be registered in Google Cloud Console alongside the YouTube redirect."),

    ("FAL_API_KEY",            "ai",      True,  False, "fal.ai API key powering image + video generation."),
]


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _safe_add_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Race-safe `ALTER TABLE table ADD COLUMN column ddl_type`.

    Postgres 9.6+ supports `ADD COLUMN IF NOT EXISTS` — use it directly.
    SQLite (which doesn't) is single-process so the existence check is safe.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"))
    else:
        if not _column_exists(conn, table, column):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _upgrade_users(conn):
    _safe_add_column(conn, "users", "stripe_customer_id",        "VARCHAR")
    _safe_add_column(conn, "users", "subscribed_until",          "TIMESTAMP")
    _safe_add_column(conn, "users", "reset_token",               "VARCHAR")
    _safe_add_column(conn, "users", "reset_token_expires_at",    "TIMESTAMP")
    _safe_add_column(conn, "users", "verify_token",              "VARCHAR")
    _safe_add_column(conn, "users", "verify_token_expires_at",   "TIMESTAMP")
    _safe_add_column(conn, "users", "calendar_token",            "VARCHAR")
    _safe_add_column(conn, "users", "google_sub",                "VARCHAR")
    _safe_add_column(conn, "users", "referral_code",             "VARCHAR")


def _upgrade_social_connections(conn):
    _safe_add_column(conn, "social_connections", "refresh_token", "TEXT")


def _upgrade_tier_configs(conn):
    """Add display fields to tier_configs without dropping perms/quotas data."""
    _safe_add_column(conn, "tier_configs", "display_name",         "VARCHAR")
    _safe_add_column(conn, "tier_configs", "blurb",                "VARCHAR")
    _safe_add_column(conn, "tier_configs", "monthly_price_cents",  "INTEGER")
    _safe_add_column(conn, "tier_configs", "stripe_price_id",      "VARCHAR")
    _safe_add_column(conn, "tier_configs", "yearly_stripe_price_id", "VARCHAR")
    _safe_add_column(conn, "tier_configs", "features_json",        "TEXT")
    _safe_add_column(conn, "tier_configs", "monthly_credit_grant", "INTEGER")
    _safe_add_column(conn, "tier_configs", "sort_order",           "INTEGER")
    _safe_add_column(conn, "tier_configs", "is_active",            "BOOLEAN")


def _upgrade_media(conn):
    _safe_add_column(conn, "social_posts", "video_url",            "VARCHAR")
    _safe_add_column(conn, "social_posts", "image_job_id",         "INTEGER")
    _safe_add_column(conn, "social_posts", "video_job_id",         "INTEGER")
    _safe_add_column(conn, "media_jobs",   "webhook_token",        "VARCHAR")
    _safe_add_column(conn, "media_jobs",   "compose_meta_json",    "TEXT")
    _safe_add_column(conn, "social_posts", "analytics_json",       "TEXT")
    _safe_add_column(conn, "social_posts", "analytics_fetched_at", "TIMESTAMP")


_DEFAULT_TIER_DISPLAY = {
    # tier_key: (display_name, blurb, monthly_price_cents, monthly_credit_grant, sort_order, features)
    "trial":  ("Trial", "Free 14-day trial.", 0, 100, 0,
               ["1 social account", "10 posts", "1 email blast", "5 AI generations", "Community support"]),
    "bronze": ("Bronze", "For solo brands getting started.", 900, 500, 10,
               ["3 social accounts", "60 posts/month", "4 email blasts/month",
                "30 AI generations/month", "Email support"]),
    "silver": ("Silver", "For growing teams shipping weekly.", 2900, 2000, 20,
               ["6 social accounts", "200 posts/month", "12 email blasts/month",
                "100 AI generations/month", "Priority support"]),
    "gold":   ("Gold", "For agencies running many brands.", 9900, 50000, 30,
               ["20 social accounts", "1000 posts/month", "50 email blasts/month",
                "1000 AI generations/month", "Dedicated support"]),
}


def _seed_tier_configs(db) -> None:
    """Idempotent seed for the four built-in tiers.  Populates permissions,
    quotas, and the billing-page display fields without clobbering admin edits.

    Reseed rules:
      - perms / quotas: merge into existing row, force-overwriting only the
        keys in _FORCE_OVERWRITE_KEYS (so admins can tweak everything else)
      - display fields: filled in *only* when blank, so once an admin edits
        the display copy on /admin/plans we never overwrite their changes
    """
    from sqlalchemy.exc import IntegrityError
    for tier, perms in _DEFAULT_TIER_PERMS.items():
        quotas = _DEFAULT_TIER_QUOTAS[tier]
        display = _DEFAULT_TIER_DISPLAY.get(tier, (tier.title(), "", 0, 0, 0, []))
        d_name, d_blurb, d_price, d_grant, d_sort, d_features = display
        try:
            existing = db.query(TierConfig).filter(TierConfig.tier == tier).first()
            if existing is None:
                db.add(TierConfig(
                    tier=tier,
                    perms_json=json.dumps(perms),
                    quotas_json=json.dumps(quotas),
                    display_name=d_name,
                    blurb=d_blurb,
                    monthly_price_cents=d_price,
                    monthly_credit_grant=d_grant,
                    features_json=json.dumps(d_features),
                    sort_order=d_sort,
                    is_active=True,
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
                # Only fill display fields when blank so admin edits stick.
                if not existing.display_name:
                    existing.display_name = d_name
                if not existing.blurb:
                    existing.blurb = d_blurb
                if existing.monthly_price_cents is None:
                    existing.monthly_price_cents = d_price
                if not existing.monthly_credit_grant:
                    existing.monthly_credit_grant = d_grant
                if not existing.features_json or existing.features_json == "[]":
                    existing.features_json = json.dumps(d_features)
                if not existing.sort_order:
                    existing.sort_order = d_sort
                if existing.is_active is None:
                    existing.is_active = True
            db.commit()
        except IntegrityError:
            db.rollback()
            continue


_DEFAULT_TOPUP_PACKS = [
    # (key, label, credits, price_cents, sort_order)
    ("pack_500",   "Starter — 500 credits",     500,   500,  10),
    ("pack_2000",  "Standard — 2,000 credits",  2000,  1500, 20),
    ("pack_10000", "Pro — 10,000 credits",      10000, 6000, 30),
]


def _seed_topup_packs(db) -> None:
    """Insert the default credit packs on first boot.  Never overwrites
    existing rows so admins can rename/reprice/disable packs from
    /admin/plans without their edits getting reverted on the next deploy."""
    from sqlalchemy.exc import IntegrityError
    for key, label, credits, price_cents, sort_order in _DEFAULT_TOPUP_PACKS:
        try:
            if db.query(TopupPackConfig).filter(TopupPackConfig.key == key).first():
                continue
            db.add(TopupPackConfig(
                key=key, label=label, credits=credits,
                price_cents=price_cents, sort_order=sort_order, is_active=True,
            ))
            db.commit()
        except IntegrityError:
            db.rollback()
            continue


def _seed_env_configs(db) -> None:
    """Ensure a row exists for every KNOWN_ENV_KEYS entry. Doesn't overwrite
    user-edited values. Pulls initial value from os.environ on first seed.

    Multi-worker safe: each row is committed in its own transaction, and a
    UniqueViolation from a racing worker is caught + rolled back rather than
    nuking the whole startup (which is what brought down the previous deploy
    when two gunicorn workers both tried to seed fresh env keys at once).
    """
    import os as _os
    from sqlalchemy.exc import IntegrityError

    for key, group, is_secret, is_locked, desc in KNOWN_ENV_KEYS:
        try:
            existing = db.query(EnvConfig).filter(EnvConfig.key == key).first()
            if existing:
                # Refresh metadata (description / group / locked / secret) in
                # case we change them in code — but never overwrite the value.
                existing.group_name = group
                existing.is_secret = is_secret
                existing.is_locked = is_locked
                existing.description = desc
            else:
                db.add(EnvConfig(
                    key=key,
                    value=_os.getenv(key, "") or None,
                    is_secret=is_secret,
                    is_locked=is_locked,
                    group_name=group,
                    description=desc,
                ))
            db.commit()
        except IntegrityError:
            # A sibling worker won the race for this key. Rollback our pending
            # insert and move on — the row exists, that's what we wanted.
            db.rollback()
            continue
        except Exception:
            db.rollback()
            raise


def _seed_role_configs(db) -> None:
    from sqlalchemy.exc import IntegrityError
    defaults = {
        "admin":      {"marketing.view": True, "marketing.email_blast": True, "marketing.social_post": True, "marketing.ai_generate": True, "admin.view": True},
        "marketing":  {"marketing.view": True, "marketing.email_blast": True, "marketing.social_post": True, "marketing.ai_generate": True},
        "tech":       {"admin.view": True},
        "strategist": {},
        "client":     {},
    }
    for role, perms in defaults.items():
        try:
            existing = db.query(RoleConfig).filter(RoleConfig.role == role).first()
            if existing is None:
                db.add(RoleConfig(role=role, perms_json=json.dumps(perms)))
            else:
                current = existing.perms_dict()
                for k, v in perms.items():
                    current.setdefault(k, v)
                existing.perms_json = json.dumps(current)
            db.commit()
        except IntegrityError:
            db.rollback()
            continue


def init_db() -> None:
    """Race-safe migrations + seeds.

    The advisory-lock approach I tried earlier hung Railway healthchecks for
    reasons I couldn't pin down without dashboard access, so we go simpler:
    every individual step is idempotent on its own.

      * `Base.metadata.create_all` is already idempotent (CREATE TABLE IF NOT
        EXISTS under the hood).
      * `_safe_add_column` uses Postgres's native `ADD COLUMN IF NOT EXISTS`,
        which is safe to run concurrently.
      * Each seed wraps its per-row insert in try/except IntegrityError so a
        worker that loses a unique-key race just rolls back its insert and
        continues — the row exists, which is the post-condition we wanted.
    """
    print("[init_db] starting", flush=True)
    Base.metadata.create_all(bind=engine)
    print("[init_db] create_all done", flush=True)
    with engine.begin() as conn:
        _upgrade_users(conn)
        _upgrade_social_connections(conn)
        _upgrade_media(conn)
        _upgrade_tier_configs(conn)
    print("[init_db] migrations done", flush=True)
    from database import SessionLocal
    db = SessionLocal()
    try:
        _seed_tier_configs(db)
        _seed_role_configs(db)
        _seed_env_configs(db)
        _seed_topup_packs(db)
        _encrypt_existing_tokens(db)
    finally:
        db.close()
    print("[init_db] complete", flush=True)


def _encrypt_existing_tokens(db) -> None:
    """One-shot: rewrite any plaintext access_token / refresh_token rows so they
    pick up the encrypted prefix. Safe to run repeatedly — encrypt() is idempotent."""
    try:
        from services.secrets import PREFIX
    except Exception:
        return  # cryptography not available — skip
    rows = db.query(SocialConnection).all()
    touched = 0
    for r in rows:
        # The TypeDecorator decrypts on read, so r.access_token is plaintext
        # regardless of what's stored. Setting it back through process_bind_param
        # will encrypt + persist.
        if r.access_token:
            r.access_token = r.access_token  # noqa — triggers TypeDecorator on commit
            touched += 1
        if r.refresh_token:
            r.refresh_token = r.refresh_token  # noqa — same
    if touched:
        db.commit()


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
