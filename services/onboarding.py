"""Onboarding email drip — checks once a day per user, sends the next due
email if its preconditions are met and we haven't already sent it.

Dripped emails (all keyed by days since signup):
  day 0 (signup): welcome — fired by the signup flow itself, not the drip
  day 1: 'connect a social account' — if connections == 0
  day 3: 'try the AI builder' — if pending+published posts == 0
  day 7: 'half-way check-in' — light engagement nudge
  day 12: 'trial ends in 2 days' — if still on trial tier
  day 14: 'trial ended — pick a plan' — if still on trial tier

Idempotency: each send writes an ActionLog row with
action = "ONBOARDING_<key>". We skip a step if such a row already exists
for the user.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Tuple

from sqlalchemy.orm import Session

from models import ActionLog, EmailBlast, SocialConnection, SocialPost, User, log_action
from services.email_utils import _smtp_config
from services.env_config import get_env

logger = logging.getLogger("gootier.onboarding")


def _action_key(step: str) -> str:
    return f"ONBOARDING_{step.upper()}"


def _has_been_sent(db: Session, user_id: int, step: str) -> bool:
    return db.query(ActionLog).filter(
        ActionLog.user_id == user_id,
        ActionLog.action == _action_key(step),
    ).first() is not None


def _user_age_days(user: User) -> int:
    if not user.created_at:
        return 0
    return (datetime.utcnow() - user.created_at).days


def _connection_count(db: Session, user_id: int) -> int:
    return db.query(SocialConnection).filter(
        SocialConnection.user_id == user_id,
        SocialConnection.is_active == True,  # noqa: E712
    ).count()


def _post_count(db: Session, user_id: int) -> int:
    return db.query(SocialPost).filter(SocialPost.user_id == user_id).count()


# ----------------------------------------------------------------------------
# Email body builders — plain text + HTML for each step.
# ----------------------------------------------------------------------------

def _app_url() -> str:
    return get_env("APP_URL", "").rstrip("/") or "https://gootier-prod.up.railway.app"


def _html_shell(title: str, body_html: str, cta_label: str = None, cta_url: str = None) -> str:
    cta = ""
    if cta_label and cta_url:
        cta = f"""
        <p style="margin:28px 0;">
          <a href="{cta_url}"
             style="display:inline-block; padding:12px 22px;
                    background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
                    color:#fff; text-decoration:none; border-radius:10px;
                    font-weight:600;">{cta_label}</a>
        </p>"""
    return f"""<div style="font-family:-apple-system,system-ui,sans-serif; max-width:480px; margin:0 auto; padding:32px;">
  <h2 style="margin:0 0 16px;">{title}</h2>
  {body_html}
  {cta}
  <hr style="border:none; border-top:1px solid #eef0f6; margin:32px 0 16px;">
  <p style="color:#8a90a6; font-size:12px;">Powered by Gootier &middot; <a href="{_app_url()}" style="color:#8a90a6;">{_app_url().replace('https://','').replace('http://','')}</a></p>
</div>"""


STEPS = [
    {
        "key": "connect_social",
        "trigger_day": 1,
        "subject": "Connect your first social account in 60 seconds",
        "applies": lambda db, u: _connection_count(db, u.id) == 0,
        "build": lambda u: (
            f"Quick nudge: most Gootier users post their first thing within their first day. The biggest blocker is just getting a social account connected.",
            _html_shell(
                f"Hey {u.nickname or u.username},",
                "<p style='color:#4b5168; line-height:1.55;'>Quick nudge: most Gootier users post their first thing within their first day. The biggest blocker is just <strong>getting one social account connected</strong>.</p>"
                "<p style='color:#4b5168; line-height:1.55;'>One click, one OAuth handshake — no API keys to dig up. We'll handle the rest.</p>",
                "Connect a social account", f"{_app_url()}/connections",
            ),
        ),
    },
    {
        "key": "try_ai_builder",
        "trigger_day": 3,
        "subject": "Let the AI draft your next 6 posts",
        "applies": lambda db, u: _post_count(db, u.id) == 0,
        "build": lambda u: (
            "Drop your marketing plan + cadence into the AI builder and watch it draft a full week of content in 20 seconds.",
            _html_shell(
                f"{u.nickname or u.username}, want a head start?",
                "<p style='color:#4b5168; line-height:1.55;'>Drop your marketing plan and posting cadence into the AI builder and watch it draft a full week of content in about 20 seconds.</p>"
                "<p style='color:#4b5168; line-height:1.55;'>You'll be able to review every item, regenerate any you don't love, and schedule them all in one click.</p>",
                "Open the AI builder", f"{_app_url()}/ai-builder",
            ),
        ),
    },
    {
        "key": "halfway_checkin",
        "trigger_day": 7,
        "subject": "How's your first week with Gootier going?",
        "applies": lambda db, u: True,
        "build": lambda u: (
            "You're a week in. Quick check: anything getting in your way?",
            _html_shell(
                f"Halfway through your trial, {u.nickname or u.username}.",
                "<p style='color:#4b5168; line-height:1.55;'>You're a week into Gootier. Hit reply on this email if anything's getting in your way — we read every one.</p>"
                "<p style='color:#4b5168; line-height:1.55;'>If you haven't tried the <strong>AI-generated images</strong> yet, that's the moment most users say it clicks for them.</p>",
                "Generate an image campaign", f"{_app_url()}/ai-builder",
            ),
        ),
    },
    {
        "key": "trial_ends_soon",
        "trigger_day": 12,
        "subject": "Your Gootier trial ends in 2 days",
        "applies": lambda db, u: u.tier == "trial",
        "build": lambda u: (
            "Heads up: 2 days left on your trial. Pick a plan to keep your scheduled content firing.",
            _html_shell(
                "Two days left on your trial.",
                "<p style='color:#4b5168; line-height:1.55;'>To keep your scheduled posts firing and your connected accounts publishing, lock in a plan before your trial ends.</p>"
                "<p style='color:#4b5168; line-height:1.55;'>Bronze is $9/mo, Silver $29/mo, Gold $99/mo — pick what fits the volume you're shipping.</p>",
                "See plans", f"{_app_url()}/billing",
            ),
        ),
    },
    {
        "key": "trial_ended",
        "trigger_day": 14,
        "subject": "Your trial ended — keep your campaigns going",
        "applies": lambda db, u: u.tier == "trial",
        "build": lambda u: (
            "Your trial just ended. Pick a plan to resume publishing your scheduled content.",
            _html_shell(
                "Trial ended — pick a plan to resume.",
                "<p style='color:#4b5168; line-height:1.55;'>Your trial just ended. Scheduled posts and AI generations are paused until you pick a plan.</p>"
                "<p style='color:#4b5168; line-height:1.55;'>Everything you've set up — connections, library, scheduled queue — is safe and waiting.</p>",
                "Pick a plan", f"{_app_url()}/billing",
            ),
        ),
    },
]


def _send_one(to_email: str, subject: str, text_body: str, html_body: str) -> bool:
    """Direct SMTP send (no quota — onboarding is system-generated)."""
    import smtplib
    from email.message import EmailMessage
    cfg = _smtp_config()
    if not cfg["host"] or cfg["host"] in {"smtp.example.com", "localhost"}:
        logger.warning("SMTP not configured — onboarding email to %s: %s", to_email, subject)
        return False
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.ehlo()
            s.starttls()
            if cfg["username"]:
                s.login(cfg["username"], cfg["password"])
            msg = EmailMessage()
            msg["From"] = cfg["from_email"]
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
            s.send_message(msg)
        return True
    except Exception as e:
        logger.warning("Onboarding email send failed (%s): %s", to_email, e)
        return False


def send_welcome_email(user: User) -> bool:
    """Fire the day-0 welcome separately from the drip — invoked from the
    signup flow so it lands in the user's inbox right after they sign up."""
    text = "Welcome to Gootier! Your 14-day trial just started. First step: connect a social account."
    html = _html_shell(
        f"Welcome to Gootier, {user.nickname or user.username}.",
        "<p style='color:#4b5168; line-height:1.55;'>Your 14-day trial just started. Here's the fastest path to your first published post:</p>"
        "<ol style='color:#4b5168; line-height:1.7;'>"
        "<li>Connect a social account (one OAuth click — Facebook, Instagram, X, LinkedIn, or TikTok).</li>"
        "<li>Compose a post or let the AI builder draft a full campaign.</li>"
        "<li>Schedule it &mdash; we publish at the moment it's due.</li>"
        "</ol>"
        "<p style='color:#4b5168; line-height:1.55;'>Anything goes wrong, hit reply — humans read these.</p>",
        "Open Gootier", _app_url() + "/dashboard",
    )
    return _send_one(user.email, "Welcome to Gootier — let's ship your first post", text, html)


def process_drip(db: Session) -> dict:
    """Walk every user in their first 30 days, run each step's `applies`
    predicate, send if not already sent. Returns a small summary dict."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    users = db.query(User).filter(
        User.is_active == True,  # noqa: E712
        User.created_at >= cutoff,
    ).all()

    sent: List[Tuple[int, str]] = []
    skipped = 0

    for user in users:
        age_days = _user_age_days(user)
        for step in STEPS:
            if age_days < step["trigger_day"]:
                continue
            if _has_been_sent(db, user.id, step["key"]):
                continue
            try:
                if not step["applies"](db, user):
                    # Still mark as 'handled' so we don't retry every tick
                    # when the precondition won't ever flip (e.g. user posted
                    # in the meantime — no need to send try_ai_builder).
                    log_action(db, user, _action_key(step["key"]), "User", str(user.id),
                               detail=f"skipped: precondition not met (age={age_days}d)")
                    skipped += 1
                    continue
                text, html = step["build"](user)
                delivered = _send_one(user.email, step["subject"], text, html)
                log_action(
                    db, user, _action_key(step["key"]), "User", str(user.id),
                    detail=f"delivered={delivered} age={age_days}d",
                )
                sent.append((user.id, step["key"]))
            except Exception as e:
                logger.exception("Onboarding step %s failed for user %s: %s",
                                 step["key"], user.id, e)
    return {"sent": sent, "skipped": skipped, "users_scanned": len(users)}
