import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import (
    COOKIE_NAME, create_access_token, hash_password, set_session_cookie,
    validate_email, validate_password, validate_username, verify_password,
)
from database import get_db
from models import HandoffToken, User, log_action
from services.affiliates import affiliates
from services.csrf import get_or_create_token, verify_csrf
from services.email_utils import send_email_verification, send_password_reset
from services.env_config import get_env
from services.flash import set_flash
from services.handoff import hash_token

logger = logging.getLogger("gootier.auth")

router = APIRouter()
templates = Jinja2Templates(directory="templates")
RESET_TOKEN_TTL_MINUTES = 60
VERIFY_TOKEN_TTL_HOURS = 24


def create_verification_token(db: Session, user: User) -> str:
    """Generate and persist a fresh verification token. Returns the token."""
    token = secrets.token_urlsafe(32)
    user.verify_token = token
    user.verify_token_expires_at = datetime.utcnow() + timedelta(hours=VERIFY_TOKEN_TTL_HOURS)
    db.commit()
    return token


def trigger_verification_email(db: Session, user: User, base_url: str) -> bool:
    """Create token, build link, send. Returns True only if SMTP actually
    delivered. Returns False if the link was logged for dev (no SMTP) or if
    delivery failed — callers should surface that distinction to users."""
    token = create_verification_token(db, user)
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    return send_email_verification(user.email, link)


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html",
                                       {"error": None, "csrf_token": get_or_create_token(request)})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    user = (
        db.query(User)
        .filter((User.username == username) | (User.email == username))
        .first()
    )
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request,
            "login.html",
            { "error": "Invalid username or password.", "csrf_token": get_or_create_token(request)},
            status_code=401,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request,
            "login.html",
            { "error": "Account disabled.", "csrf_token": get_or_create_token(request)},
            status_code=403,
        )

    token = create_access_token(user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(response, token)
    set_flash(response, "success", f"Welcome back, {user.nickname or user.username}!")
    log_action(db, user, "LOGIN", "User", str(user.id), detail="Login success")
    return response


# Deliberately a bare GET, unlike /verify-email's CSRF-protected-POST
# interstitial: this URL is a server-side redirect target from the
# Backoffice, never an emailed link a scanner/prefetcher could hit, so
# that pattern's justification doesn't apply here.
@router.get("/sso/consume")
def sso_consume(token: str = "", db: Session = Depends(get_db)):
    if not token:
        return RedirectResponse(url="/login?error=sso", status_code=303)

    h = hash_token(token)
    row = db.query(HandoffToken).filter(HandoffToken.token_hash == h).one_or_none()
    now = datetime.utcnow()
    if row is None or row.used_at is not None or row.expires_at <= now:
        return RedirectResponse(url="/login?error=sso", status_code=303)

    user = db.query(User).filter(User.id == row.user_id).one_or_none()
    if user is None or not user.is_active or user.has_role("admin"):
        return RedirectResponse(url="/login?error=sso", status_code=303)

    # Burn BEFORE issuing the cookie, and make the burn itself the
    # concurrency guard: two racing consumes of one token cannot both
    # succeed.
    burned = db.query(HandoffToken).filter(
        HandoffToken.id == row.id, HandoffToken.used_at.is_(None),
    ).update({"used_at": now})
    db.commit()
    if burned != 1:
        return RedirectResponse(url="/login?error=sso", status_code=303)

    session_token = create_access_token(user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    set_session_cookie(response, session_token)
    set_flash(response, "success", f"Welcome, {user.nickname or user.username}!")
    log_action(db, user, "BACKOFFICE_HANDOFF_CONSUME", "User", str(user.id))
    return response


@router.get("/signup")
async def signup_page(request: Request, ref: str = ""):
    return templates.TemplateResponse(request, "signup.html",
                                       {"error": None, "csrf_token": get_or_create_token(request),
                                        "ref": ref})


@router.post("/signup")
async def signup_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    ref: str = Form(default=""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    for err in (validate_username(username), validate_email(email), validate_password(password)):
        if err:
            return templates.TemplateResponse(
                request, "signup.html",
                { "error": err, "csrf_token": get_or_create_token(request), "ref": ref}, status_code=400,
            )
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse(
            request,
            "signup.html",
            { "error": "Username already taken.", "csrf_token": get_or_create_token(request), "ref": ref},
            status_code=400,
        )
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            request,
            "signup.html",
            { "error": "Email already registered.", "csrf_token": get_or_create_token(request), "ref": ref},
            status_code=400,
        )

    user = User(
        username=username,
        email=email,
        hashed_password=hash_password(password),
        role="client",
        tier="trial",
        is_active=True,
        is_verified=False,
        referral_code=ref.strip() or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(db, user, "SIGNUP", "User", str(user.id))

    # Eagerly create the JTS wallet (and grant the trial balance) at signup
    # rather than lazily on first AI use. Deliberately wrapped: unlike
    # debit_after_success, ensure_wallet() has no built-in try/except of its
    # own (by design — check_sufficient/balance_tokens need it to be able to
    # raise so *they* can decide what to do). Called here as a best-effort
    # step after the account is already committed, so a JTS outage at signup
    # time (network blip, JTS deployment down, etc.) can't crash the signup
    # request or block account creation — a wallet-less user just gets
    # ensure_wallet'd lazily on their first check_sufficient/balance_tokens
    # call instead.
    try:
        from services.token_wallet import ensure_wallet
        ensure_wallet(db, user)
    except Exception:
        db.rollback()
        logger.exception("JTS ensure_wallet failed at signup: user=%s", user.id)

    if user.referral_code:
        try:
            affiliates.report_signup(user.id, ref_code=user.referral_code)
        except Exception as e:
            logger.warning("affiliates report_signup failed for user %s: %s", user.id, e)

    # Fire verification email + welcome email (logs the link if SMTP isn't configured).
    trigger_verification_email(db, user, _app_url(request))
    try:
        from services.onboarding import send_welcome_email
        send_welcome_email(user)
    except Exception:
        pass  # welcome email is best-effort — never block signup on it

    token = create_access_token(user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(response, token)
    set_flash(response, "success", "Welcome to Gootier! Check your email to verify your address.")
    return response


@router.get("/logout")
async def logout(response: Response):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# --------------------------------------------------------------------------- #
# Email verification (scanner-proof two-step pattern)
# --------------------------------------------------------------------------- #

def _validate_verify_token(db: Session, token: str):
    if not token:
        return None
    user = db.query(User).filter(User.verify_token == token).first()
    if not user or not user.verify_token_expires_at:
        return None
    if user.verify_token_expires_at < datetime.utcnow():
        return None
    return user


@router.get("/verify-email")
async def verify_email_page(request: Request, token: str = "",
                              db: Session = Depends(get_db)):
    user = _validate_verify_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request, "verify_email.html",
            {"error": "This verification link is invalid or has expired.",
             "token": "", "done": False, "email": "",
             "csrf_token": get_or_create_token(request)},
            status_code=400,
        )
    return templates.TemplateResponse(request, "verify_email.html",
                                       {"error": None, "token": token,
                                        "done": False, "email": user.email,
                                        "csrf_token": get_or_create_token(request)})


@router.post("/verify-email")
async def verify_email_submit(
    request: Request,
    token: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    user = _validate_verify_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request, "verify_email.html",
            {"error": "This verification link is invalid or has expired.",
             "token": "", "done": False, "email": "",
             "csrf_token": get_or_create_token(request)},
            status_code=400,
        )

    user.is_verified = True
    user.verify_token = None
    user.verify_token_expires_at = None
    db.commit()
    log_action(db, user, "EMAIL_VERIFIED", "User", str(user.id))

    # Auto-login after successful verification.
    access = create_access_token(user.id)
    response = templates.TemplateResponse(
        request, "verify_email.html",
        {"error": None, "token": "", "done": True, "email": user.email,
         "csrf_token": get_or_create_token(request)},
    )
    set_session_cookie(response, access)
    return response


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #

def _app_url(request: Request) -> str:
    env_url = get_env("APP_URL", "").rstrip("/")
    if env_url:
        return env_url
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


@router.get("/forgot-password")
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html",
                                       {"error": None, "sent": False,
                                        "csrf_token": get_or_create_token(request)})


@router.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    # Always render the same "if an account exists…" message — don't leak who's registered.
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user and user.is_active:
        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
        db.commit()

        link = f"{_app_url(request)}/reset-password?token={token}"
        send_password_reset(user.email, link)
        log_action(db, user, "PASSWORD_RESET_REQUEST", "User", str(user.id))

    return templates.TemplateResponse(request, "forgot_password.html",
                                       {"error": None, "sent": True,
                                        "csrf_token": get_or_create_token(request)})


def _validate_reset_token(db: Session, token: str):
    if not token:
        return None
    user = db.query(User).filter(User.reset_token == token).first()
    if not user or not user.reset_token_expires_at:
        return None
    if user.reset_token_expires_at < datetime.utcnow():
        return None
    return user


@router.get("/reset-password")
async def reset_password_page(request: Request, token: str = "",
                               db: Session = Depends(get_db)):
    user = _validate_reset_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"error": "This reset link is invalid or has expired.",
             "token": "", "done": False, "csrf_token": get_or_create_token(request)},
            status_code=400,
        )
    return templates.TemplateResponse(request, "reset_password.html",
                                       {"error": None, "token": token, "done": False,
                                        "csrf_token": get_or_create_token(request)})


@router.post("/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    user = _validate_reset_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"error": "This reset link is invalid or has expired.",
             "token": "", "done": False, "csrf_token": get_or_create_token(request)},
            status_code=400,
        )

    err = validate_password(password)
    if err:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"error": err, "token": token, "done": False,
             "csrf_token": get_or_create_token(request)},
            status_code=400,
        )

    user.hashed_password = hash_password(password)
    user.reset_token = None
    user.reset_token_expires_at = None
    db.commit()
    log_action(db, user, "PASSWORD_RESET", "User", str(user.id))

    return templates.TemplateResponse(request, "reset_password.html",
                                       {"error": None, "token": "", "done": True,
                                        "csrf_token": get_or_create_token(request)})
