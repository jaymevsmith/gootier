import logging
import smtplib
from email.message import EmailMessage
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from services.env_config import get_env

logger = logging.getLogger("gootier.email")


def _smtp_config(db: Optional[Session] = None) -> dict:
    """Five config reads. Passing the caller's session keeps them on the
    connection the request already holds instead of opening five more."""
    return {
        "host": get_env("SMTP_HOST", "", db=db),
        "port": int(get_env("SMTP_PORT", "587", db=db) or "587"),
        "username": get_env("SMTP_USERNAME", "", db=db),
        "password": get_env("SMTP_PASSWORD", "", db=db),
        "from_email": get_env("FROM_EMAIL", "no-reply@gootier.local", db=db),
    }


def send_email_verification(to_email: str, verify_link: str,
                            db: Optional[Session] = None) -> bool:
    cfg = _smtp_config(db)
    if not cfg["host"] or cfg["host"] in {"smtp.example.com", "localhost"}:
        logger.warning("SMTP not configured — verification link for %s: %s", to_email, verify_link)
        return False

    html = f"""
    <div style="font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px;">
      <h2 style="margin: 0 0 16px;">Confirm your email</h2>
      <p style="color: #4b5168; line-height: 1.55;">
        Welcome to Gootier — let's make sure this address is yours. Click the button
        below to confirm. The link expires in 24 hours.
      </p>
      <p style="margin: 28px 0;">
        <a href="{verify_link}"
           style="display: inline-block; padding: 12px 22px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                  color: #fff; text-decoration: none; border-radius: 10px; font-weight: 600;">
          Confirm email
        </a>
      </p>
      <p style="color: #8a90a6; font-size: 13px; line-height: 1.55;">
        Didn't sign up? You can ignore this email and the link will expire on its own.
      </p>
    </div>
    """

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.ehlo()
            s.starttls()
            if cfg["username"]:
                s.login(cfg["username"], cfg["password"])
            msg = EmailMessage()
            msg["From"] = cfg["from_email"]
            msg["To"] = to_email
            msg["Subject"] = "Confirm your Gootier email"
            msg.set_content(f"Confirm your email: {verify_link}\n\nLink expires in 24 hours.")
            msg.add_alternative(html, subtype="html")
            s.send_message(msg)
        return True
    except Exception as e:
        logger.warning("SMTP send failed (%s) — verification link for %s: %s",
                       e, to_email, verify_link)
        return False


def send_password_reset(to_email: str, reset_link: str,
                        db: Optional[Session] = None) -> bool:
    cfg = _smtp_config(db)
    # Sentinel hosts from .env.example aren't real SMTP — treat as not configured.
    if not cfg["host"] or cfg["host"] in {"smtp.example.com", "localhost"}:
        logger.warning("SMTP not configured — password reset link for %s: %s", to_email, reset_link)
        return False

    html = f"""
    <div style="font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px;">
      <h2 style="margin: 0 0 16px;">Reset your Gootier password</h2>
      <p style="color: #4b5168; line-height: 1.55;">
        Someone (hopefully you) asked to reset the password for this account.
        Click the button below to choose a new one. The link expires in 1 hour.
      </p>
      <p style="margin: 28px 0;">
        <a href="{reset_link}"
           style="display: inline-block; padding: 12px 22px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                  color: #fff; text-decoration: none; border-radius: 10px; font-weight: 600;">
          Reset password
        </a>
      </p>
      <p style="color: #8a90a6; font-size: 13px; line-height: 1.55;">
        If you didn't request this, you can ignore the email — your password won't change.
      </p>
    </div>
    """

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.ehlo()
            s.starttls()
            if cfg["username"]:
                s.login(cfg["username"], cfg["password"])
            msg = EmailMessage()
            msg["From"] = cfg["from_email"]
            msg["To"] = to_email
            msg["Subject"] = "Reset your Gootier password"
            msg.set_content(f"Reset your password: {reset_link}\n\nLink expires in 1 hour.")
            msg.add_alternative(html, subtype="html")
            s.send_message(msg)
        return True
    except Exception as e:
        logger.warning("SMTP send failed (%s) — password reset link for %s: %s",
                       e, to_email, reset_link)
        return False


def send_blast_email(subject: str, body_html: str, recipients: List[str]) -> Tuple[int, int]:
    cfg = _smtp_config()
    if not cfg["host"]:
        return 0, len(recipients)

    sent = failed = 0
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.ehlo()
            s.starttls()
            if cfg["username"]:
                s.login(cfg["username"], cfg["password"])
            for to_addr in recipients:
                msg = EmailMessage()
                msg["From"] = cfg["from_email"]
                msg["To"] = to_addr
                msg["Subject"] = subject
                msg.set_content("This email requires an HTML-capable client.")
                msg.add_alternative(body_html, subtype="html")
                try:
                    s.send_message(msg)
                    sent += 1
                except Exception:
                    failed += 1
    except Exception:
        return sent, len(recipients) - sent
    return sent, failed
