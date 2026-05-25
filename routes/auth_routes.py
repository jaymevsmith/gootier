import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import (
    COOKIE_NAME, TOKEN_TTL_MINUTES, create_access_token, hash_password,
    validate_email, validate_password, validate_username, verify_password,
)
from database import get_db
from models import User, log_action
from services.email_utils import send_email_verification, send_password_reset
from services.env_config import get_env
from services.flash import set_flash

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
    return templates.TemplateResponse(request, "login.html", { "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
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
            { "error": "Invalid username or password."},
            status_code=401,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request,
            "login.html",
            { "error": "Account disabled."},
            status_code=403,
        )

    token = create_access_token(user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, samesite="lax", max_age=TOKEN_TTL_MINUTES * 60,
    )
    set_flash(response, "success", f"Welcome back, {user.nickname or user.username}!")
    log_action(db, user, "LOGIN", "User", str(user.id), detail="Login success")
    return response


@router.get("/signup")
async def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", { "error": None})


@router.post("/signup")
async def signup_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    for err in (validate_username(username), validate_email(email), validate_password(password)):
        if err:
            return templates.TemplateResponse(
                request, "signup.html", { "error": err}, status_code=400,
            )
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse(
            request,
            "signup.html",
            { "error": "Username already taken."},
            status_code=400,
        )
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            request,
            "signup.html",
            { "error": "Email already registered."},
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
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(db, user, "SIGNUP", "User", str(user.id))

    # Fire verification email (logs the link if SMTP isn't configured).
    trigger_verification_email(db, user, _app_url(request))

    token = create_access_token(user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, samesite="lax", max_age=TOKEN_TTL_MINUTES * 60,
    )
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
             "token": "", "done": False, "email": ""},
            status_code=400,
        )
    return templates.TemplateResponse(request, "verify_email.html",
                                       {"error": None, "token": token,
                                        "done": False, "email": user.email})


@router.post("/verify-email")
async def verify_email_submit(
    request: Request,
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _validate_verify_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request, "verify_email.html",
            {"error": "This verification link is invalid or has expired.",
             "token": "", "done": False, "email": ""},
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
        {"error": None, "token": "", "done": True, "email": user.email},
    )
    response.set_cookie(
        key=COOKIE_NAME, value=access, httponly=True, samesite="lax", max_age=TOKEN_TTL_MINUTES * 60,
    )
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
                                       {"error": None, "sent": False})


@router.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
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
                                       {"error": None, "sent": True})


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
             "token": "", "done": False},
            status_code=400,
        )
    return templates.TemplateResponse(request, "reset_password.html",
                                       {"error": None, "token": token, "done": False})


@router.post("/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _validate_reset_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"error": "This reset link is invalid or has expired.",
             "token": "", "done": False},
            status_code=400,
        )

    err = validate_password(password)
    if err:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"error": err, "token": token, "done": False},
            status_code=400,
        )

    user.hashed_password = hash_password(password)
    user.reset_token = None
    user.reset_token_expires_at = None
    db.commit()
    log_action(db, user, "PASSWORD_RESET", "User", str(user.id))

    return templates.TemplateResponse(request, "reset_password.html",
                                       {"error": None, "token": "", "done": True})
