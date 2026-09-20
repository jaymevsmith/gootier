"""routes/internal_routes.py

POST /internal/handoff -- resolves a Backoffice customer into a Gootier
session, keyed by email (person-shaped, unlike Cloud Storage's domain-keyed
org resolution). See
docs/superpowers/specs/2026-09-01-gootier-connected-app-design.md in the
jhome-backoffice repo for the full requirement list this implements.
"""
import logging
import re
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import hash_password
from database import get_db
from models import HandoffToken, User, log_action
from routes.oauth_routes import _unique_username_from_email
from services.env_config import get_env
from services.handoff import generate_token, hash_token, default_expiry
from services import token_wallet

log = logging.getLogger("gootier.internal_handoff")

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class HandoffRequest(BaseModel):
    email: str
    name: str | None = None
    jhome_sub: str | None = None
    # The Backoffice's assertion that IT has proof of this address. Defaults
    # False so a caller that omits it fails closed -- refused rather than
    # silently vouching for an address nobody verified. Only gates binding an
    # EXISTING account by email (see the check in handoff()); creating a brand
    # new account is not a bind, so it stays reachable.
    email_verified: bool = False


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    expected = get_env("GOOTIER_INTERNAL_KEY", "")
    # Fail CLOSED on an unset key. Compare as bytes, not str:
    # secrets.compare_digest raises TypeError on non-ASCII str operands, and
    # Starlette decodes headers as latin-1, so any byte >= 0x80 reaches here.
    if not expected or not secrets.compare_digest(
        x_internal_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid internal key")


def _create_user(db: Session, email: str, jhome_sub: str | None, name: str | None) -> User:
    """Find-or-create with a bounded retry for the rare concurrent-username
    race: two handoffs for different brand-new emails that happen to derive
    the same base username, racing on the same candidate slot."""
    for attempt in range(3):
        username = _unique_username_from_email(db, email)
        user = User(
            username=username,
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            role="client",
            tier="trial",
            is_active=True,
            is_verified=True,
            nickname=name or username,
            jhome_sub=jhome_sub,
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
            log_action(db, user, "SIGNUP", "User", str(user.id),
                       detail="Backoffice handoff -- new account")
            return user
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise HTTPException(status_code=500, detail="could not allocate a username")
    raise HTTPException(status_code=500, detail="could not allocate a username")


def resolve_or_create_gootier_user(
    db: Session, *, jhome_sub: str | None, email: str, email_verified: bool,
    name: str | None = None,
) -> User:
    """Find-or-create a Gootier user by email, mirroring handoff()'s own
    refusal logic exactly -- every one of the 5 /internal/mcp/* routes calls
    this directly (not only ensure-account), so the system works correctly
    even if a customer's AI client never calls ensure-account first,
    matching MidCanvas's generate_image_for_mcp precedent.

    `email` must already be normalized (.strip().lower()) by the caller.
    """
    matches = db.query(User).filter(func.lower(User.email) == email).order_by(User.id).all()
    if len(matches) > 1:
        log.warning("mcp identity refused: %d case-variant accounts for email %s", len(matches), email)
        raise HTTPException(status_code=409, detail={"error": "ambiguous_identity"})
    user = matches[0] if matches else None

    if user is not None and not user.is_active:
        log.warning("mcp identity refused: user %s is deactivated", user.id)
        raise HTTPException(status_code=403, detail={"error": "account_inactive"})

    if user is not None and not email_verified:
        log.warning("mcp identity refused: caller did not assert email_verified for user %s", user.id)
        raise HTTPException(status_code=409, detail={"error": "unverified_caller_email"})

    if user is None and jhome_sub:
        existing_sub_holder = db.query(User).filter(User.jhome_sub == jhome_sub).first()
        if existing_sub_holder is not None:
            log.warning(
                "mcp identity refused: jhome_sub %s already belongs to a different user (%s), "
                "but the request's email does not match that user",
                jhome_sub, existing_sub_holder.id,
            )
            raise HTTPException(status_code=409, detail={"error": "linked_elsewhere"})

    if user is None:
        user = _create_user(db, email, jhome_sub, name)
    elif jhome_sub and not user.jhome_sub:
        # NOTE: deliberately not committed here. A caller-side refusal that
        # fires later in this function (e.g. the admin check below) must
        # leave zero DB side effects -- see
        # test_admin_jhome_sub_adoption_does_not_persist_on_refusal in
        # tests/test_internal_handoff.py. The caller (handoff() or an
        # /internal/mcp/* route) is responsible for committing once identity
        # resolution has fully succeeded.
        user.jhome_sub = jhome_sub
    elif jhome_sub and user.jhome_sub and user.jhome_sub != jhome_sub:
        log.warning(
            "mcp identity refused: user %s carried jhome_sub %s but it already has %s",
            user.id, jhome_sub, user.jhome_sub,
        )
        raise HTTPException(status_code=409, detail={"error": "linked_elsewhere"})

    if user.has_role("admin"):
        log.warning("mcp identity refused: user %s has platform admin access", user.id)
        raise HTTPException(status_code=403, detail={"error": "admin_account_not_supported"})

    if user.jhome_sub:
        try:
            token_wallet.link_wallet_to_customer(db, user)
        except Exception:  # noqa: BLE001 -- must never fail an identity resolution on this
            log.exception("could not link wallet for user %s", user.id)

    return user


@router.post("/internal/handoff", dependencies=[Depends(require_internal_key)])
def handoff(req: HandoffRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"

    app_url = get_env("APP_URL", "").rstrip("/")
    if not app_url:
        raise HTTPException(status_code=500, detail="APP_URL is not configured")

    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid email")

    user = resolve_or_create_gootier_user(
        db, jhome_sub=req.jhome_sub, email=email,
        email_verified=req.email_verified, name=req.name,
    )

    token = generate_token()
    db.add(HandoffToken(token_hash=hash_token(token), user_id=user.id,
                         expires_at=default_expiry()))
    db.commit()

    log.info("handoff minted token for user %s", user.id)
    log_action(db, user, "BACKOFFICE_HANDOFF", "User", str(user.id))

    return {"consume_url": f"{app_url}/sso/consume?token={token}"}
