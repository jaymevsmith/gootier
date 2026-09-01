"""tests/test_handoff_migration.py"""
from sqlalchemy import create_engine, inspect

from database import Base
import models  # noqa: F401


def test_jhome_sub_column_exists_on_a_fresh_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert "jhome_sub" in cols


def test_handoff_tokens_table_exists_on_a_fresh_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    tables = inspect(engine).get_table_names()
    assert "handoff_tokens" in tables
    cols = {c["name"] for c in inspect(engine).get_columns("handoff_tokens")}
    assert {"id", "token_hash", "user_id", "expires_at", "used_at", "created_at"} <= cols


def test_gootier_internal_key_is_a_known_env_key():
    from models import KNOWN_ENV_KEYS
    keys = {k for k, *_ in KNOWN_ENV_KEYS}
    assert "GOOTIER_INTERNAL_KEY" in keys
