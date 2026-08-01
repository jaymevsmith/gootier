"""tests/test_media_routes_mark_failed.py

Regression test for the "_mark_failed must never overwrite an already-done
job" guard. Without the guard, a transport error raised by a post-success
debit_after_success(...) call (e.g. a raw httpx.ConnectError/TimeoutException
that isn't caught internally, unlike InsufficientTokensError/JTSError) would
propagate into the surrounding except block and clobber a job's committed
"done" status + result_url back to "failed", even though the media asset was
already durably saved.
"""
from datetime import datetime

from models import MediaJob, User
from routes.media_routes import _mark_failed


def _user(db) -> User:
    u = User(username="u1", email="u1@test.com", hashed_password="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _job(db, user, status: str) -> MediaJob:
    job = MediaJob(
        user_id=user.id,
        kind="image",
        provider="fal",
        model_key="some-model",
        model_endpoint="fal-ai/some-model",
        prompt="a cat",
        status=status,
        result_url="https://example.com/result.png" if status == "done" else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_mark_failed_does_not_overwrite_already_done_job(db):
    user = _user(db)
    job = _job(db, user, status="done")

    _mark_failed(db, user, job, "some post-success transport error")

    assert job.status == "done"
    assert job.result_url == "https://example.com/result.png"
    assert job.error is None
    assert job.completed_at is None


def test_mark_failed_still_marks_non_done_job_as_failed(db):
    user = _user(db)
    job = _job(db, user, status="running")

    _mark_failed(db, user, job, "fal submission failed")

    assert job.status == "failed"
    assert job.error == "fal submission failed"
    assert isinstance(job.completed_at, datetime)
