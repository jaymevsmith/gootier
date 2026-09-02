"""Guards on the test suite's own database and network isolation.

These exist because of a concrete failure: the two signup tests in
tests/test_affiliates_integration.py passed or failed depending on whether the
developer running them happened to have a `./gootier.db` file in the working
directory. `services/env_config.get_env()` opens its own
`database.SessionLocal()` instead of taking the request's session, so
`app.dependency_overrides[get_db]` never reached it and those reads went to
whatever `DATABASE_URL` points at — by default the developer's real local
database. See tests/conftest.py for the fixtures that close both holes.
"""
import httpx
import pytest

import database
from models import EnvConfig
from services.env_config import get_env


def test_session_local_is_bound_to_the_test_database():
    """Nothing opened via `SessionLocal()` may reach `./gootier.db`."""
    bind = database.SessionLocal.kw.get("bind")
    assert bind is not None
    assert bind.url.database == ":memory:", (
        f"SessionLocal is bound to {bind.url!r} — tests would read the "
        "developer's real database"
    )


def test_get_env_reads_the_test_database(db):
    """The exact call that broke signup: `get_env()` opens its own session, so
    it has to land in the same per-test database the request session uses."""
    db.add(EnvConfig(key="APP_URL", value="https://isolated.test", group_name="app"))
    db.commit()

    assert get_env("APP_URL") == "https://isolated.test"


def test_get_env_falls_back_when_the_key_is_unset(db):
    """An empty test database must read cleanly rather than raising
    `no such table: env_configs` — the original failure's actual symptom."""
    assert get_env("DEFINITELY_NOT_SET_ANYWHERE", "fallback") == "fallback"


def test_real_outbound_http_is_blocked():
    """`JTSClient` defaults to the *production* Token Service URL, so an
    unpatched wallet call in a test would hit the live service and pass."""
    with pytest.raises(RuntimeError, match="Real network call attempted"):
        httpx.Client(timeout=1).get("https://example.invalid/")
