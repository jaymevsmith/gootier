"""Signup must not fail *after* it has already committed the account.

`routes/auth_routes.py::signup_submit` commits the new User, then does a tail of
best-effort work: JTS wallet creation, affiliate reporting, the verification
email and the welcome email. Three of those four were wrapped; the verification
email was not, and neither was the `_app_url()` call feeding it.

That matters because `_app_url()` -> `get_env("APP_URL")` and (one level
deeper) `send_email_verification()` -> `_smtp_config()` -> five more
`get_env()` calls each open their own `database.SessionLocal()`. A database
blip in any of them raised straight out of the handler — returning a 500 to
someone whose account *had* been created and who never received a session
cookie, with no way to tell from the error that they now have an account.

These tests pin the fallback behaviour. See tests/conftest.py's
`unavailable_config_db` for how the blip is simulated.
"""
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from auth import COOKIE_NAME
from routes import auth_routes
from tests.test_affiliates_integration import _signup_form


def _request(scheme="https", host="gootier.test"):
    """A bare Starlette Request, enough for `_app_url()`'s URL fallback."""
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/signup",
        "scheme": scheme,
        "server": (host, 443 if scheme == "https" else 80),
        "headers": [(b"host", host.encode())],
        "query_string": b"",
    })


def test_app_url_falls_back_to_the_request_url_when_the_config_read_fails(
    unavailable_config_db,
):
    """`_app_url()` already has a perfectly good fallback — the request's own
    scheme and host. A failed `get_env()` should reach it, not escape."""
    assert auth_routes._app_url(_request()) == "https://gootier.test"


def test_app_url_still_prefers_a_configured_value(db):
    """The fallback must not shadow a real APP_URL when the database is fine."""
    from models import EnvConfig

    db.add(EnvConfig(key="APP_URL", value="https://app.gootier.com/", group_name="app"))
    db.commit()

    assert auth_routes._app_url(_request()) == "https://app.gootier.com"


def test_signup_completes_when_the_config_database_is_unavailable(
    client, unavailable_config_db,
):
    """The regression this whole module exists for: every `get_env()` in the
    post-commit tail raises, and signup must still finish — 303 to /dashboard
    with a session cookie, so the account the handler just committed is usable
    rather than stranded behind a 500."""
    with patch("services.token_wallet.ensure_wallet"):
        resp = _signup_form(client, username="dbblip", email="dbblip@example.com")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp.cookies


def test_signup_completes_when_the_verification_email_step_raises(client):
    """Same guarantee for any other failure in that step — a broken SMTP
    helper, a template error, or the `db.commit()` inside
    `create_verification_token`."""
    with patch("services.token_wallet.ensure_wallet"), \
         patch.object(auth_routes, "trigger_verification_email",
                      side_effect=OperationalError("stmt", {}, Exception("db gone"))):
        resp = _signup_form(client, username="mailfail", email="mailfail@example.com")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp.cookies


def test_signup_still_reports_the_account_as_created_in_the_database(
    client, test_engine, unavailable_config_db,
):
    """A 303 alone could in principle come from a rolled-back transaction, so
    check the row actually survived the failing tail."""
    from sqlalchemy.orm import sessionmaker
    from models import User

    with patch("services.token_wallet.ensure_wallet"):
        _signup_form(client, username="survives", email="survives@example.com")

    session = sessionmaker(bind=test_engine)()
    try:
        user = session.query(User).filter(User.username == "survives").first()
        assert user is not None
        assert user.email == "survives@example.com"
    finally:
        session.close()
