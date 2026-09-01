"""tests/test_handoff_migration.py"""
from sqlalchemy import create_engine, inspect

from database import Base
import models  # noqa: F401


def test_jhome_sub_column_exists_on_a_fresh_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert "jhome_sub" in cols
