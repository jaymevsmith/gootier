import os
import re
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from database import get_db
from models import RoleConfig, TierConfig, User

SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_ME_DEV_ONLY")
ALGORITHM = "HS256"
TOKEN_TTL_MINUTES = int(os.getenv("TOKEN_TTL_MINUTES", "60"))
COOKIE_NAME = "access_token"

# Refuse to boot in prod with the dev default — JWT signing requires a real secret.
if SECRET_KEY == "CHANGE_ME_DEV_ONLY" and os.getenv("ENV", "").lower() in {"prod", "production"}:
    raise RuntimeError(
        "SECRET_KEY is unset (still 'CHANGE_ME_DEV_ONLY') in a prod environment. "
        "Generate one with `python -c \"import secrets; print(secrets.token_hex(32))\"` "
        "and set it via env / Secrets Manager before boot."
    )
_BCRYPT_MAX = 72  # bcrypt input is hard-capped at 72 bytes

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8")[:_BCRYPT_MAX], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_BCRYPT_MAX], hashed.encode("utf-8"))
    except Exception:
        return False


def validate_username(username: str) -> Optional[str]:
    if not username or not _USERNAME_RE.match(username):
        return "Username must be 3-20 chars, letters/numbers/_/- only."
    return None


def validate_email(email: str) -> Optional[str]:
    if not email or not _EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def validate_password(password: str) -> Optional[str]:
    if not password or len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[a-z]", password):
        return "Password must contain a lowercase letter."
    if not re.search(r"[A-Z]", password):
        return "Password must contain an uppercase letter."
    if not re.search(r"[0-9]", password):
        return "Password must contain a number."
    return None


def create_access_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_user_id(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except (JWTError, ValueError):
        return None


def _load_permissions(db: Session, user: User) -> None:
    perms: dict = {}
    tier_cfg = db.query(TierConfig).filter(TierConfig.tier == user.tier).first()
    if tier_cfg:
        perms.update(tier_cfg.perms_dict())
    for role in (user.role or "").split(","):
        role = role.strip()
        if not role:
            continue
        role_cfg = db.query(RoleConfig).filter(RoleConfig.role == role).first()
        if role_cfg:
            perms.update(role_cfg.perms_dict())
    user.permissions = perms


def _get_user_from_token(db: Session, token: str) -> Optional[User]:
    user_id = _decode_user_id(token)
    if not user_id:
        return None
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if user:
        _load_permissions(db, user)
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    access_token: Optional[str] = Cookie(default=None),
) -> User:
    token = access_token
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = _get_user_from_token(db, token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    return user


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
    access_token: Optional[str] = Cookie(default=None),
) -> Optional[User]:
    if not access_token:
        return None
    return _get_user_from_token(db, access_token)


def require_perm(key: str):
    def _checker(user: User = Depends(get_current_user)) -> User:
        if not user.perm(key):
            raise HTTPException(status_code=403, detail=f"Requires permission: {key}")
        return user
    return _checker


def get_quota(db: Session, tier: str, key: str) -> int:
    cfg = db.query(TierConfig).filter(TierConfig.tier == tier).first()
    if not cfg:
        return 0
    return int(cfg.quotas_dict().get(key, 0))
