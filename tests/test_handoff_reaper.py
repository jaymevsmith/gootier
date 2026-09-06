"""tests/test_handoff_reaper.py"""
from datetime import datetime, timedelta

from models import HandoffToken, User
from services.handoff import hash_token, reap_expired_tokens


def _make_user(db):
    user = User(username="reaper-target", email="reaper-target@example.com",
                hashed_password="x", role="client", tier="trial")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_token(db, user, *, expires_at, used_at=None):
    row = HandoffToken(
        token_hash=hash_token(f"tok-{expires_at.isoformat()}-{used_at}"),
        user_id=user.id,
        expires_at=expires_at,
        used_at=used_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_reap_deletes_tokens_expired_more_than_a_day_ago(db):
    user = _make_user(db)
    old_id = _make_token(db, user, expires_at=datetime.utcnow() - timedelta(days=2)).id

    deleted = reap_expired_tokens(db)

    assert deleted == 1
    assert db.query(HandoffToken).filter(HandoffToken.id == old_id).first() is None


def test_reap_keeps_tokens_within_the_retention_window(db):
    user = _make_user(db)
    recently_expired = _make_token(db, user, expires_at=datetime.utcnow() - timedelta(minutes=30))
    still_valid = _make_token(db, user, expires_at=datetime.utcnow() + timedelta(minutes=2))

    deleted = reap_expired_tokens(db)

    assert deleted == 0
    assert db.query(HandoffToken).count() == 2
    assert db.query(HandoffToken).filter(HandoffToken.id == recently_expired.id).first() is not None
    assert db.query(HandoffToken).filter(HandoffToken.id == still_valid.id).first() is not None


def test_reap_deletes_used_tokens_once_past_retention(db):
    user = _make_user(db)
    used_long_ago_id = _make_token(
        db, user,
        expires_at=datetime.utcnow() - timedelta(days=3),
        used_at=datetime.utcnow() - timedelta(days=3),
    ).id

    deleted = reap_expired_tokens(db)

    assert deleted == 1
    assert db.query(HandoffToken).filter(HandoffToken.id == used_long_ago_id).first() is None
