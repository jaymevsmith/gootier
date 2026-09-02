"""tests/test_token_wallet.py"""
import pytest
from fastapi import HTTPException

from models import User
from services import token_wallet
from services.jts_client import InsufficientTokensError, JTSError


class FakeJTSClient:
    def __init__(self, balance=2_000_000, raise_on_debit=None, raise_on_ensure_wallet=None):
        self.balance = balance
        self.debit_calls = []
        self.ensure_wallet_calls = []
        self.raise_on_debit = raise_on_debit
        self.raise_on_ensure_wallet = raise_on_ensure_wallet

    def ensure_wallet(self, external_user_id, email="", customer_ref=None):
        self.ensure_wallet_calls.append((external_user_id, customer_ref))
        if self.raise_on_ensure_wallet is not None:
            raise self.raise_on_ensure_wallet
        return 999

    def get_balance(self, wallet_id):
        return self.balance

    def debit(self, wallet_id, model_key, request_id, **usage):
        self.debit_calls.append({"wallet_id": wallet_id, "model_key": model_key,
                                  "request_id": request_id, **usage})
        if self.raise_on_debit is not None:
            raise self.raise_on_debit
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
    assert fake.ensure_wallet_calls == [(str(user.id), None)]

    # second call doesn't hit JTS again
    token_wallet.ensure_wallet(db, user)
    assert fake.ensure_wallet_calls == [(str(user.id), None)]


def test_ensure_wallet_forwards_customer_ref_when_present(db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)
    user.jhome_sub = "sub-abc"
    db.commit()

    token_wallet.ensure_wallet(db, user)

    assert fake.ensure_wallet_calls == [(str(user.id), "sub-abc")]


def test_ensure_wallet_sends_no_customer_ref_when_jhome_sub_is_unset(db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)  # jhome_sub defaults to None

    token_wallet.ensure_wallet(db, user)

    assert fake.ensure_wallet_calls == [(str(user.id), None)]


def test_link_wallet_to_customer_calls_the_service_even_when_a_wallet_is_already_cached(
        db, monkeypatch):
    """The exact bug this function exists to fix: a user who already has a
    wallet (e.g. from normal signup) must still get grouped when their
    jhome_sub is set later. ensure_wallet would short-circuit on the cached id
    and never send customer_ref at all."""
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)
    user.jts_wallet_id = 555  # simulates a wallet already created at signup
    user.jhome_sub = "sub-existing-user"
    db.commit()

    result = token_wallet.link_wallet_to_customer(db, user)

    assert fake.ensure_wallet_calls == [(str(user.id), "sub-existing-user")]
    # FakeJTSClient always answers 999 -- it keeps no per-user wallet map --
    # so the returned id is the service's answer, not the cached 555.
    assert result == 999


def test_link_wallet_to_customer_does_not_overwrite_an_already_cached_wallet_id(
        db, monkeypatch):
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)
    user.jts_wallet_id = 555
    user.jhome_sub = "sub-x"
    db.commit()

    token_wallet.link_wallet_to_customer(db, user)

    db.refresh(user)
    assert user.jts_wallet_id == 555  # unchanged -- FakeJTSClient returns 999


def test_link_wallet_to_customer_caches_the_id_when_there_is_none_yet(db, monkeypatch):
    """A brand-new user created by the handoff itself still gets the id
    persisted, exactly as ensure_wallet would have."""
    fake = FakeJTSClient()
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)
    user.jhome_sub = "sub-brand-new"
    db.commit()
    assert user.jts_wallet_id is None

    result = token_wallet.link_wallet_to_customer(db, user)

    assert result == 999
    db.refresh(user)
    assert user.jts_wallet_id == 999


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


def test_debit_after_success_never_raises_on_insufficient_tokens(db, monkeypatch):
    fake = FakeJTSClient(raise_on_debit=InsufficientTokensError(
        balance_tokens=100, tokens_required=8100,
        balance_display=0, tokens_required_display=8,
    ))
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    result = token_wallet.debit_after_success(
        db, user, model_key="fal-nano-banana-2",
        request_id="gootier-mediajob-124", units=1,
    )

    assert result is None


def test_debit_after_success_never_raises_on_generic_jts_error(db, monkeypatch):
    fake = FakeJTSClient(raise_on_debit=JTSError("debit failed: 500 boom"))
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    result = token_wallet.debit_after_success(
        db, user, model_key="fal-nano-banana-2",
        request_id="gootier-mediajob-125", units=1,
    )

    assert result is None


def test_debit_after_success_never_raises_on_non_jts_exception(db, monkeypatch):
    """A raw transport-level failure (e.g. httpx connect/read timeout) is not a
    JTSError subclass. debit_after_success must still swallow it and return
    None rather than let it escape into a caller that already has a
    successful result (e.g. the compose/ai-plan synchronous handler)."""
    fake = FakeJTSClient(raise_on_debit=ConnectionError("connection refused"))
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)

    result = token_wallet.debit_after_success(
        db, user, model_key="anthropic-sonnet-5",
        request_id="gootier-aiplan-127", input_tokens=100, output_tokens=200,
    )

    assert result is None


def test_debit_after_success_never_raises_when_ensure_wallet_fails(db, monkeypatch):
    """Reproduces the bug: wallet id isn't cached yet (e.g. debit called from a
    different request context, like the fal webhook handler) and JTS's
    ensure-wallet endpoint is transiently unreachable. debit_after_success must
    still return None rather than let the JTSError escape."""
    fake = FakeJTSClient(raise_on_ensure_wallet=JTSError("ensure_wallet failed: 500 boom"))
    monkeypatch.setattr(token_wallet, "_client", lambda: fake)
    user = _user(db)
    assert user.jts_wallet_id is None  # not cached yet, forces the ensure_wallet call

    result = token_wallet.debit_after_success(
        db, user, model_key="fal-nano-banana-2",
        request_id="gootier-mediajob-126", units=1,
    )

    assert result is None
