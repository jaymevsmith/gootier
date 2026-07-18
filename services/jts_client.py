"""Raw HTTP client for the Jhome Token Service (JTS) wallet/debit API.

Deliberately thin — no fail-open/fail-closed policy here (that lives in
services/token_wallet.py). This module talks HTTP and raises typed errors;
callers decide what to do with them."""
from typing import Optional

import httpx

from services.env_config import get_env


class JTSError(Exception):
    """Any non-2xx JTS response that isn't a typed insufficient-tokens case."""


class InsufficientTokensError(JTSError):
    def __init__(self, balance_tokens: int, tokens_required: int,
                 balance_display: int, tokens_required_display: int):
        self.balance_tokens = balance_tokens
        self.tokens_required = tokens_required
        self.balance_display = balance_display
        self.tokens_required_display = tokens_required_display
        super().__init__(
            f"insufficient tokens: have {balance_display}, need {tokens_required_display}"
        )


class JTSClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = (base_url or get_env(
            "TOKEN_SERVICE_URL", "https://jhome-token-service-production.up.railway.app"
        )).rstrip("/")
        self.api_key = api_key or get_env("TOKEN_SERVICE_API_KEY", "")
        self._client = httpx.Client(transport=transport, timeout=10)

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    def ensure_wallet(self, external_user_id: str, email: str = "") -> int:
        resp = self._client.post(
            f"{self.base_url}/wallets",
            headers=self._headers(),
            json={"external_user_id": external_user_id, "email": email},
        )
        if resp.status_code != 200:
            raise JTSError(f"ensure_wallet failed: {resp.status_code} {resp.text}")
        return int(resp.json()["wallet_id"])

    def get_balance(self, wallet_id: int) -> int:
        resp = self._client.get(
            f"{self.base_url}/wallets/{wallet_id}/balance",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise JTSError(f"get_balance failed: {resp.status_code} {resp.text}")
        return int(resp.json()["balance_tokens"])

    def debit(self, wallet_id: int, model_key: str, request_id: str, **usage) -> dict:
        resp = self._client.post(
            f"{self.base_url}/wallets/{wallet_id}/debit",
            headers=self._headers(),
            json={"model_key": model_key, "request_id": request_id, **usage},
        )
        if resp.status_code == 402:
            payload = resp.json()
            # JTS wraps error details under a `detail` key by FastAPI HTTPException
            # convention in production, but callers (and this test suite) may also
            # get the fields flat at the top level. Handle both shapes.
            body = payload.get("detail", payload) if isinstance(payload.get("detail"), dict) else payload
            raise InsufficientTokensError(
                balance_tokens=body["balance_tokens"],
                tokens_required=body["tokens_required"],
                balance_display=body["balance_display"],
                tokens_required_display=body["tokens_required_display"],
            )
        if resp.status_code != 200:
            raise JTSError(f"debit failed: {resp.status_code} {resp.text}")
        return resp.json()
