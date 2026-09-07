"""Runtime env-var overrides backed by the `env_configs` table.

Reads from DB first, falls back to `os.environ`. Lets admins set / rotate
API keys without redeploying. Mirrors the Trading Jay pattern from ECOSYSTEM.md.

Module imports must call `get_env("KEY")` instead of `os.getenv("KEY")` for
any value an admin might want to manage from `/admin/env`.

Pass `db=` whenever you already hold a request session. Without it `get_env()`
opens a second connection of its own, which is invisible to
`app.dependency_overrides[get_db]` in tests and is one more thing that can fail
mid-request. It stays optional because several callers legitimately have no
session to give — module-level config helpers, and `services/secrets.py`, whose
`_fernet()` runs inside a SQLAlchemy TypeDecorator during flush, where reusing
the flushing session would be re-entrant.

Caching is intentionally NOT done here — call sites read on demand. At our
current scale that's fine; if it ever isn't, wrap with a 30s TTL cache.
"""
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database import SessionLocal
from models import EnvConfig, User, log_action


def get_env(key: str, default: str = "", *, db: Optional[Session] = None) -> str:
    """Return DB-stored value if set & non-empty, otherwise `os.environ`, otherwise default.

    `db` is the caller's session, used as-is and left open — ownership stays
    with whoever passed it. Omit it and a short-lived session is opened and
    closed here, as before.

    Keyword-only so a session can never land in `default` by position.
    """
    if db is not None:
        return _lookup(db, key, default)

    own = SessionLocal()
    try:
        return _lookup(own, key, default)
    finally:
        own.close()


def _lookup(db: Session, key: str, default: str) -> str:
    # no_autoflush: reading config must never flush a caller's pending work as
    # a side effect. SessionLocal already sets autoflush=False, but a supplied
    # session is not guaranteed to be one of ours.
    with db.no_autoflush:
        cfg = db.query(EnvConfig).filter(EnvConfig.key == key).first()
    if cfg and cfg.value:
        return cfg.value
    return os.getenv(key, default)


def set_env(key: str, value: Optional[str], actor: User) -> dict:
    """Set or clear an env value from the admin UI. Refuses locked keys.

    Pass `value=None` or empty string to fall back to os.environ on next read.
    Returns a detached snapshot dict to avoid `DetachedInstanceError` after
    the session closes.
    """
    db = SessionLocal()
    try:
        cfg = db.query(EnvConfig).filter(EnvConfig.key == key).first()
        if not cfg:
            raise KeyError(f"Unknown env key: {key}")
        if cfg.is_locked:
            raise PermissionError(f"{key} is locked from admin editing.")

        new_val = (value or "").strip() or None
        cfg.value = new_val
        cfg.updated_at = datetime.utcnow()
        cfg.updated_by_id = actor.id
        cfg.updated_by_name = actor.username
        db.commit()
        db.refresh(cfg)

        snapshot = {
            "id": cfg.id,
            "key": cfg.key,
            "cleared": new_val is None,
            "updated_at": cfg.updated_at,
            "updated_by_name": cfg.updated_by_name,
        }

        # Audit log records the key, never the value.
        log_action(db, actor, "ENV_UPDATE", "EnvConfig", str(cfg.id),
                   detail=f"key={cfg.key} cleared={new_val is None}")
        return snapshot
    finally:
        db.close()


def list_for_admin(db) -> list:
    """List all env keys with secret values masked. For the admin UI."""
    rows = db.query(EnvConfig).order_by(
        EnvConfig.group_name.asc(), EnvConfig.key.asc()
    ).all()
    out = []
    for r in rows:
        has_value = bool(r.value or os.environ.get(r.key))
        source = "db" if r.value else ("env" if os.environ.get(r.key) else "unset")
        display = "" if r.is_secret else (r.value or "")
        out.append({
            "key": r.key,
            "group": r.group_name,
            "is_secret": r.is_secret,
            "is_locked": r.is_locked,
            "description": r.description,
            "has_value": has_value,
            "source": source,
            "display_value": display,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "updated_by_name": r.updated_by_name,
        })
    return out
