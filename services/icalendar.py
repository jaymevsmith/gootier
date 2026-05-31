"""iCalendar (RFC 5545) feed generator.

We expose a per-user .ics URL secured by a random token in the path. Google
Calendar, Outlook, Apple Calendar — anything that speaks the standard — can
subscribe to it. The calendar clients poll on their own schedule (Google
~12h, Outlook ~3h, Apple every 5 min) so Gootier-side changes flow out
without any push API or OAuth integration.

Hand-rolled to avoid a new dependency. The format is plain text with strict
rules: CRLF line endings, lines folded at 75 octets, special chars in TEXT
fields escaped.
"""
import json
from datetime import datetime, timedelta
from typing import Iterable, List

from models import EmailBlast, SocialConnection, SocialPost, User

DEFAULT_EVENT_MIN = 30
CRLF = "\r\n"


def _esc(text: str) -> str:
    """Escape per RFC 5545 §3.3.11 (TEXT type)."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "")
            .replace(",", "\\,")
            .replace(";", "\\;")
    )


def _fold(line: str) -> str:
    """Fold any line longer than 75 octets onto continuation lines starting
    with a single space, per RFC 5545 §3.1."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out: List[str] = []
    chunk = bytearray()
    buf = bytearray()
    for ch in line.encode("utf-8"):
        buf.append(ch)
        if len(buf) >= 73:
            out.append(buf.decode("utf-8", errors="ignore"))
            buf = bytearray()
    if buf:
        out.append(buf.decode("utf-8", errors="ignore"))
    return out[0] + CRLF + CRLF.join(" " + s for s in out[1:])


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _vevent(uid: str, dtstart: datetime, dtend: datetime, summary: str,
            description: str = "", url: str = "", status: str = "CONFIRMED") -> str:
    fields = [
        "BEGIN:VEVENT",
        _fold(f"UID:{uid}"),
        _fold(f"DTSTAMP:{_utc(datetime.utcnow())}"),
        _fold(f"DTSTART:{_utc(dtstart)}"),
        _fold(f"DTEND:{_utc(dtend)}"),
        _fold(f"SUMMARY:{_esc(summary)}"),
    ]
    if description:
        fields.append(_fold(f"DESCRIPTION:{_esc(description)}"))
    if url:
        fields.append(_fold(f"URL:{url}"))
    fields.append(f"STATUS:{status}")
    fields.append("END:VEVENT")
    return CRLF.join(fields)


def _connection_labels(db, post: SocialPost) -> str:
    if not post.connection_ids:
        return ""
    ids = [int(x) for x in post.connection_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return ""
    conns = db.query(SocialConnection).filter(SocialConnection.id.in_(ids)).all()
    return ", ".join(f"{c.platform}/{c.account_name}" for c in conns)


def build_ics(db, user: User, app_url: str) -> str:
    """Render the user's scheduled posts + blasts + recently published items
    as a VCALENDAR string. Looks back 30 days and forward 90 days."""
    now = datetime.utcnow()
    window_start = now - timedelta(days=30)
    window_end = now + timedelta(days=90)

    events: List[str] = []

    posts: Iterable[SocialPost] = (
        db.query(SocialPost)
        .filter(SocialPost.user_id == user.id)
        .filter(SocialPost.scheduled_at != None)  # noqa: E711
        .filter(SocialPost.scheduled_at >= window_start)
        .filter(SocialPost.scheduled_at <= window_end)
        .filter(SocialPost.status != "cancelled")
        .all()
    )
    for p in posts:
        if not p.scheduled_at:
            continue
        targets = _connection_labels(db, p)
        summary_prefix = "✓" if p.status == "published" else ("⚠" if p.status == "failed" else "•")
        summary = f"{summary_prefix} Social post"
        if targets:
            summary += f" — {targets}"
        body_preview = (p.content or "")[:240]
        description_lines = [
            f"Status: {p.status}",
            f"Targets: {targets}" if targets else "",
            "",
            body_preview,
        ]
        if p.image_url:
            description_lines.append(f"\nImage: {p.image_url}")
        if p.video_url:
            description_lines.append(f"Video: {p.video_url}")
        events.append(_vevent(
            uid=f"social-post-{p.id}@gootier",
            dtstart=p.scheduled_at,
            dtend=p.scheduled_at + timedelta(minutes=DEFAULT_EVENT_MIN),
            summary=summary,
            description="\n".join(line for line in description_lines if line is not None),
            url=f"{app_url}/calendar",
            status="CANCELLED" if p.status in ("failed",) else "CONFIRMED",
        ))

    blasts: Iterable[EmailBlast] = (
        db.query(EmailBlast)
        .filter(EmailBlast.user_id == user.id)
        .filter(EmailBlast.scheduled_at != None)  # noqa: E711
        .filter(EmailBlast.scheduled_at >= window_start)
        .filter(EmailBlast.scheduled_at <= window_end)
        .filter(EmailBlast.status != "cancelled")
        .all()
    )
    for b in blasts:
        if not b.scheduled_at:
            continue
        prefix = "✓" if b.status == "sent" else ("⚠" if b.status == "failed" else "✉")
        summary = f"{prefix} Email blast — {b.subject or '(no subject)'}"
        description_lines = [
            f"Status: {b.status}",
            f"Recipients: {b.recipient_count}",
        ]
        events.append(_vevent(
            uid=f"email-blast-{b.id}@gootier",
            dtstart=b.scheduled_at,
            dtend=b.scheduled_at + timedelta(minutes=DEFAULT_EVENT_MIN),
            summary=summary,
            description="\n".join(description_lines),
            url=f"{app_url}/blasts",
            status="CANCELLED" if b.status == "failed" else "CONFIRMED",
        ))

    cal_name = f"Gootier — {user.nickname or user.username}"
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Gootier//Marketing Scheduler//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"NAME:{_esc(cal_name)}"),
        _fold(f"X-WR-CALNAME:{_esc(cal_name)}"),
        _fold("X-WR-CALDESC:Scheduled posts and email blasts from Gootier."),
        # Tell well-behaved clients to refresh every 15 minutes (Google /
        # Outlook ignore this but Apple Calendar respects it).
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]
    footer = ["END:VCALENDAR"]

    return CRLF.join(header + events + footer) + CRLF
