"""tests/test_handoff_tokens.py"""
from datetime import datetime, timedelta

from services.handoff import generate_token, hash_token, default_expiry


def test_generate_token_is_random_and_url_safe():
    a, b = generate_token(), generate_token()
    assert a != b
    assert len(a) > 20
    assert all(c.isalnum() or c in "-_" for c in a)


def test_hash_token_is_deterministic_sha256():
    token = "fixed-value-for-this-test"
    h1, h2 = hash_token(token), hash_token(token)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest length
    assert h1 != token


def test_default_expiry_is_two_minutes_out():
    from datetime import timedelta
    before = datetime.utcnow()
    expiry = default_expiry()
    after = datetime.utcnow()
    assert timedelta(minutes=1, seconds=55) <= (expiry - before) <= timedelta(minutes=2, seconds=5)
    assert expiry > after
