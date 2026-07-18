"""tests/test_token_wallet.py"""
import pytest
from fastapi import HTTPException

from models import User
from services import token_wallet


class FakeJTSClient:
    def __init__(self, balance=2_000_000):
        self.balance = balance
        self.debit_calls = []
        self.ensure_wallet_calls = []

    def ensure_wallet(self, external_user_id, email=""):
        self.ensure_wallet_calls.append(external_user_id)
        return 999

    def get_balance(self, wallet_id):
        return self.balance

    def debit(self, wallet_id, model_key, request_id, **usage):
        self.debit_calls.append({"wallet_id": wallet_id, "model_key": model_key,
                                  "request_id": request_id, **usage})
        return {"tokens_charged": 8100, "balance_tokens": self.balance - 8100}


def _user(db) -> User:
    u = User(username="u1", email="u1@test.com", hashed_password="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_ensure_wallet_caches_id_on_user(db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    wallet_id = token_wallet.ensure_wallet(db, user)

    assert wallet_id == 999
    assert user.jts_wallet_id == 999
    assert fake.ensure_wallet_calls == [str(user.id)]

    # second call doesn't hit JTS again
    token_wallet.ensure_wallet(db, user)
    assert fake.ensure_wallet_calls == [str(user.id)]


def test_check_sufficient_raises_402_when_estimate_exceeds_balance(db, monkeypatch):
    fake = FakeJTSClient(balance=1000)
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    with pytest.raises(HTTPException) as exc_info:
        token_wallet.check_sufficient(db, user, estimated_tokens=5000)
    assert exc_info.value.status_code == 402


def test_check_sufficient_passes_when_balance_covers_estimate(db, monkeypatch):
    fake = FakeJTSClient(balance=2_000_000)
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    token_wallet.check_sufficient(db, user, estimated_tokens=5000)  # should not raise


def test_debit_after_success_passes_through_usage_and_request_id(db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    result = token_wallet.debit_after_success(
        db, user, model_key="fal-nano-banana-2",
        request_id="gootier-mediajob-123", units=1,
    )

    assert result["tokens_charged"] == 8100
    assert fake.debit_calls == [{
        "wallet_id": 999, "model_key": "fal-nano-banana-2",
        "request_id": "gootier-mediajob-123", "units": 1,
    }]
