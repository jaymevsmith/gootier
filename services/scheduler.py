import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List

from database import SessionLocal
from models import EmailBlast, MediaJob, SocialConnection, SocialPost
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
    # If this post is waiting on AI-generated media jobs, hold publishing
    # until those jobs are either done or failed. (Failed media jobs just
    # mean the post publishes without that asset.)
    image_url, video_url, wait = _resolve_media_for_post(db, post)
    if wait:
        logger.info("post %s waiting on AI media jobs — will retry next tick", post.id)
        return

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
        image_url=image_url,
        video_url=video_url,
    )
    # Persist any media URLs that we lazily resolved so subsequent reads see them.
    if image_url and image_url != post.image_url:
        post.image_url = image_url
    if video_url and video_url != post.video_url:
        post.video_url = video_url
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


def _resolve_media_for_post(db, post: SocialPost):
    """Returns (image_url, video_url, wait_flag).

    - image_url / video_url: the URL to publish (post field if set, else the
      result_url from the linked MediaJob if it's done).
    - wait_flag: True if any linked job is still queued/running — caller
      should skip this tick.
    """
    image_url = post.image_url
    video_url = post.video_url
    wait = False

    for slot, job_id in (("image", post.image_job_id), ("video", post.video_job_id)):
        if not job_id:
            continue
        job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
        if not job:
            continue
        if job.status in ("queued", "running"):
            wait = True
            continue
        if job.status == "done" and job.result_url:
            if slot == "image" and not image_url:
                image_url = job.result_url
            elif slot == "video" and not video_url:
                video_url = job.result_url
        # failed / cancelled jobs are treated as "no media available" — post still publishes

    return image_url, video_url, wait


async def _refresh_recent_analytics() -> None:
    """Walk recently-published posts and refresh per-platform insights.

    Caps the per-tick budget so a quiet account stays cheap; busy accounts
    will catch up across multiple ticks.
    """
    from datetime import timedelta
    from services.analytics import fetch_post_metrics
    cutoff = datetime.utcnow() - timedelta(days=7)
    db = SessionLocal()
    try:
        # Prefer posts whose analytics we've never fetched, then oldest fetched.
        posts = (
            db.query(SocialPost)
            .filter(SocialPost.status.in_(("published", "partial")))
            .filter(SocialPost.created_at >= cutoff)
            .order_by(SocialPost.analytics_fetched_at.asc().nullsfirst())
            .limit(20)
            .all()
        )
        for post in posts:
            try:
                await fetch_post_metrics(db, post)
            except Exception as e:
                logger.warning("analytics refresh failed for post %s: %s", post.id, e)
    finally:
        db.close()


async def scheduler_loop() -> None:
    logger.info("Gootier scheduler loop starting")
    tick = 0
    ANALYTICS_EVERY_TICKS = 10  # 10 minutes when INTERVAL_SECONDS=60
    while True:
        tick += 1
        try:
            await _process_due_posts()
            _process_due_blasts()
            if tick % ANALYTICS_EVERY_TICKS == 0:
                await _refresh_recent_analytics()
        except Exception as e:
            logger.exception("Scheduler tick failed: %s", e)
        await asyncio.sleep(INTERVAL_SECONDS)
