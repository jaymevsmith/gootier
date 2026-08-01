"""tests/test_jts_client.py"""
import httpx
import pytest

from services.jts_client import JTSClient, JTSError, InsufficientTokensError


def _client(handler) -> JTSClient:
    transport = httpx.MockTransport(handler)
    return JTSClient(base_url="https://jts.test", api_key="jts_test", transport=transport)


def test_ensure_wallet_returns_wallet_id():
    def handler(request):
        assert request.headers["X-API-Key"] == "jts_test"
        assert request.url.path == "/wallets"
        return httpx.Response(200, json={"wallet_id": 42, "balance_tokens": 2000000})
    wallet_id = _client(handler).ensure_wallet(external_user_id="7", email="a@b.com")
    assert wallet_id == 42


def test_get_balance_returns_raw_tokens():
    def handler(request):
        assert request.url.path == "/wallets/42/balance"
        return httpx.Response(200, json={"wallet_id": 42, "balance_tokens": 500000, "balance_display": 500})
    balance = _client(handler).get_balance(42)
    assert balance == 500000


def test_debit_sends_model_key_usage_and_request_id():
    captured = {}
    def handler(request):
        captured["body"] = httpx.Request(request.method, request.url, content=request.content).content
        import json as _json
        captured["json"] = _json.loads(request.content)
        return httpx.Response(200, json={
            "tokens_charged": 8100, "tokens_charged_display": 8,
            "balance_tokens": 99991900, "balance_display": 99992,
        })
    result = _client(handler).debit(
        wallet_id=42, model_key="fal-nano-banana-2", request_id="gootier-mediajob-9",
        units=1,
    )
    assert captured["json"] == {
        "model_key": "fal-nano-banana-2", "units": 1, "request_id": "gootier-mediajob-9",
    }
    assert result["tokens_charged"] == 8100


def test_debit_raises_insufficient_tokens_on_402():
    def handler(request):
        return httpx.Response(402, json={
            "error": "insufficient tokens", "balance_tokens": 100, "balance_display": 0,
            "tokens_required": 8100, "tokens_required_display": 8,
        })
    with pytest.raises(InsufficientTokensError) as exc_info:
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)
    assert exc_info.value.balance_tokens == 100
    assert exc_info.value.tokens_required == 8100


def test_debit_raises_jts_error_on_unexpected_status():
    def handler(request):
        return httpx.Response(500, text="boom")
    with pytest.raises(JTSError):
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)


def test_debit_raises_insufficient_tokens_on_402_wrapped_in_detail():
    def handler(request):
        return httpx.Response(402, json={"detail": {
            "error": "insufficient tokens", "balance_tokens": 100, "balance_display": 0,
            "tokens_required": 8100, "tokens_required_display": 8,
        }})
    with pytest.raises(InsufficientTokensError) as exc_info:
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)
    assert exc_info.value.balance_tokens == 100
    assert exc_info.value.tokens_required == 8100


def test_ensure_wallet_raises_jts_error_on_unexpected_status():
    def handler(request):
        return httpx.Response(500, text="boom")
    with pytest.raises(JTSError):
        _client(handler).ensure_wallet(external_user_id="7", email="a@b.com")


def test_get_balance_raises_jts_error_on_unexpected_status():
    def handler(request):
        return httpx.Response(500, text="boom")
    with pytest.raises(JTSError):
        _client(handler).get_balance(42)


def test_debit_raises_jts_error_on_402_with_missing_key():
    def handler(request):
        return httpx.Response(402, json={"detail": {
            "balance_tokens": 100, "tokens_required": 8100,
        }})
    with pytest.raises(JTSError):
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)


def test_debit_raises_jts_error_on_402_with_non_json_body():
    def handler(request):
        return httpx.Response(402, text="Payment required")
    with pytest.raises(JTSError):
        _client(handler).debit(wallet_id=42, model_key="fal-nano-banana-2",
                                request_id="gootier-mediajob-9", units=1)
