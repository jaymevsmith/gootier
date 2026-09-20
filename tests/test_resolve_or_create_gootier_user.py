from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from database import Base
from models import User
from routes.internal_routes import resolve_or_create_gootier_user


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def test_a_brand_new_email_creates_a_user(db):
    user = resolve_or_create_gootier_user(
        db, jhome_sub="sub-1", email="new@example.com", email_verified=True, name="New Person")
    assert user.id is not None
    assert user.email == "new@example.com"
    assert user.jhome_sub == "sub-1"
    assert user.tier == "trial"


def test_an_existing_user_found_by_email_is_reused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="bronze"))
    db.commit()

    user = resolve_or_create_gootier_user(
        db, jhome_sub="sub-2", email="jane@example.com", email_verified=True)
    assert user.username == "jane"
    assert user.tier == "bronze"
    assert user.jhome_sub == "sub-2"


def test_a_jhome_sub_already_bound_to_a_different_user_is_refused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial", jhome_sub="sub-taken"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-taken", email="someone-else@example.com", email_verified=True)
    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": "linked_elsewhere"}


def test_an_inactive_user_is_refused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial", is_active=False))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-3", email="jane@example.com", email_verified=True)
    assert exc.value.status_code == 403
    assert exc.value.detail == {"error": "account_inactive"}


def test_an_admin_account_is_refused(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="admin", tier="trial"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-4", email="jane@example.com", email_verified=True)
    assert exc.value.status_code == 403
    assert exc.value.detail == {"error": "admin_account_not_supported"}


def test_an_unverified_caller_cannot_bind_an_existing_account(db):
    db.add(User(username="jane", email="jane@example.com", hashed_password="x",
                role="client", tier="trial"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_or_create_gootier_user(
            db, jhome_sub="sub-5", email="jane@example.com", email_verified=False)
    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": "unverified_caller_email"}
