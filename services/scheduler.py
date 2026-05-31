import asyncio
import json
import logging
from datetime import datetime
from typing import List

from database import SessionLocal
from models import EmailBlast, SocialConnection, SocialPost
from services.email_utils import send_blast_email
from services.social_publish import publish_to_connections

logger = logging.getLogger("gootier.scheduler")
INTERVAL_SECONDS = 60


async def _process_due_posts() -> None:
    db = SessionLocal()
    try:
        due = (
            db.query(SocialPost)
            .filter(SocialPost.status == "pending")
            .filter(SocialPost.scheduled_at != None)  # noqa: E711
            .filter(SocialPost.scheduled_at <= datetime.utcnow())
            .all()
        )
        for post in due:
            await _publish_post(db, post)
    finally:
        db.close()


async def _publish_post(db, post: SocialPost) -> None:
    conn_ids = [int(x) for x in (post.connection_ids or "").split(",") if x.strip().isdigit()]
    connections: List[SocialConnection] = (
        db.query(SocialConnection)
        .filter(SocialConnection.id.in_(conn_ids))
        .filter(SocialConnection.user_id == post.user_id)
        .filter(SocialConnection.is_active == True)  # noqa: E712
        .all()
    )
    if not connections:
        post.status = "failed"
        post.publish_results = json.dumps({"error": "No active connections"})
        db.commit()
        return

    results = await publish_to_connections(
        connections, post.content,
        link_url=post.link_url,
        image_url=post.image_url,
        video_url=post.video_url,
    )
    successes = sum(1 for r in results.values() if r.get("success"))
    if successes == len(connections):
        post.status = "published"
    elif successes > 0:
        post.status = "partial"
    else:
        post.status = "failed"
    post.publish_results = json.dumps({str(k): v for k, v in results.items()})
    post.published_at = datetime.utcnow()
    db.commit()


def _process_due_blasts() -> None:
    db = SessionLocal()
    try:
        due = (
            db.query(EmailBlast)
            .filter(EmailBlast.status == "pending")
            .filter(EmailBlast.scheduled_at != None)  # noqa: E711
            .filter(EmailBlast.scheduled_at <= datetime.utcnow())
            .all()
        )
        for blast in due:
            recipients = [r.strip() for r in (blast.recipient_list or "").splitlines() if r.strip()]
            sent, failed = send_blast_email(blast.subject, blast.body_html, recipients)
            blast.sent_count = sent
            blast.failed_count = failed
            if failed == 0 and sent > 0:
                blast.status = "sent"
            elif sent > 0:
                blast.status = "partial"
            else:
                blast.status = "failed"
            db.commit()
    finally:
        db.close()


async def scheduler_loop() -> None:
    logger.info("Gootier scheduler loop starting")
    while True:
        try:
            await _process_due_posts()
            _process_due_blasts()
        except Exception as e:
            logger.exception("Scheduler tick failed: %s", e)
        await asyncio.sleep(INTERVAL_SECONDS)
