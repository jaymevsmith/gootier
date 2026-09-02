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
from sqlalchemy.orm import Session

from database import get_db
from models import HandoffToken, User, log_action
from services.env_config import get_env
from services.handoff import generate_token, hash_token, default_expiry

log = logging.getLogger("gootier.internal_handoff")

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class HandoffRequest(BaseModel):
    email: str
    name: str | None = None
    jhome_sub: str | None = None


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    expected = get_env("GOOTIER_INTERNAL_KEY", "")
    # Fail CLOSED on an unset key. Compare as bytes, not str:
    # secrets.compare_digest raises TypeError on non-ASCII str operands, and
    # Starlette decodes headers as latin-1, so any byte >= 0x80 reaches here.
    if not expected or not secrets.compare_digest(
        x_internal_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid internal key")


@router.post("/internal/handoff", dependencies=[Depends(require_internal_key)])
def handoff(req: HandoffRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"

    app_url = get_env("APP_URL", "").rstrip("/")
    if not app_url:
        raise HTTPException(status_code=500, detail="APP_URL is not configured")

    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid email")

    matches = db.query(User).filter(func.lower(User.email) == email).order_by(User.id).all()
    if len(matches) > 1:
        log.warning("handoff refused: %d case-variant accounts for email %s", len(matches), email)
        raise HTTPException(status_code=409, detail="ambiguous account")
    user = matches[0] if matches else None

    if user is None:
        # A later task replaces this with real new-user creation. None of
        # this task's tests exercise a brand-new email, so this 501 is
        # never hit yet -- it exists only so the endpoint has a defined,
        # non-crashing response for the branch until that task fills it in.
        raise HTTPException(status_code=501, detail="new-user creation not implemented yet")

    if user.has_role("admin"):
        log.warning("handoff refused: user %s has platform admin access", user.id)
        raise HTTPException(status_code=403, detail="handoff refused")

    token = generate_token()
    db.add(HandoffToken(token_hash=hash_token(token), user_id=user.id,
                         expires_at=default_expiry()))
    db.commit()

    log.info("handoff minted token for user %s", user.id)
    log_action(db, user, "BACKOFFICE_HANDOFF", "User", str(user.id))

    return {"consume_url": f"{app_url}/sso/consume?token={token}"}
