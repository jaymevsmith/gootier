"""The `db=` parameter on `services.env_config.get_env`.

`get_env()` historically opened its own `database.SessionLocal()` for every
lookup. That made config reads invisible to `app.dependency_overrides[get_db]`,
and put a second connection on the critical path of requests that already held
one — the failure this repo hit three times (see HANDOFF.md). Callers holding a
session now pass it; the no-argument form is unchanged for the callers that
genuinely have none.
"""
import pytest
from sqlalchemy.orm import sessionmaker

from models import EnvConfig, User
from services.env_config import get_env


def test_reads_through_a_supplied_session(db):
    db.add(EnvConfig(key="APP_URL", value="https://passed.test", group_name="app"))
    db.commit()

    assert get_env("APP_URL", db=db) == "https://passed.test"


def test_a_supplied_session_removes_the_dependency_on_session_local(
    db, orphaned_session_local,
):
    """The payoff: with `SessionLocal` pointed at a schema-less engine, the
    lookup still succeeds, because it never opens one."""
    db.add(EnvConfig(key="APP_URL", value="https://passed.test", group_name="app"))
    db.commit()

    assert get_env("APP_URL", db=db) == "https://passed.test"

    # ...and the no-argument form is the thing that breaks, confirming the
    # fixture is actually in force rather than the assertion above being free.
    with pytest.raises(Exception):
        get_env("APP_URL")


def test_the_supplied_session_is_left_open(db):
    """Ownership stays with the caller — closing it here would break the
    request handler mid-flight."""
    get_env("APP_URL", db=db)

    assert db.is_active
    db.query(User).count()  # still usable


def test_falls_back_to_the_default_through_a_supplied_session(db):
    assert get_env("NOT_SET_ANYWHERE", "fallback", db=db) == "fallback"


def test_an_empty_stored_value_falls_through_to_the_default(db):
    """A cleared key means "unset", not "empty string" — this is how
    `set_env(value=None)` restores the `os.environ` fallback."""
    db.add(EnvConfig(key="APP_URL", value=None, group_name="app"))
    db.commit()

    assert get_env("APP_URL", "fallback", db=db) == "fallback"


def test_does_not_flush_the_callers_pending_work(test_engine):
    """A config read must not have side effects on the caller's session. Uses
    an autoflush session on purpose: `SessionLocal` sets autoflush=False, so
    only a session configured the other way exercises the `no_autoflush` guard.
    """
    session = sessionmaker(bind=test_engine, autoflush=True)()
    try:
        pending = User(
            username="notyet",
            email="notyet@example.com",
            hashed_password="x",
            role="client",
            tier="trial",
        )
        session.add(pending)

        get_env("APP_URL", db=session)

        assert pending in session.new, "get_env flushed the caller's pending insert"
    finally:
        session.rollback()
        session.close()


def test_db_cannot_be_passed_positionally(db):
    """Keyword-only: a session landing in `default` by position would silently
    return a Session object as the config value."""
    with pytest.raises(TypeError):
        get_env("APP_URL", "", db)
