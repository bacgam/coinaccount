from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from typing import Any

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class BackpackAPIError(RuntimeError):
    """A safe, user-facing Backpack API error."""


def _stringify_signing_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_signing_message(
    instruction: str,
    params: Mapping[str, Any] | None,
    timestamp: int,
    window: int,
) -> str:
    """Build the alphabetically ordered message required by Backpack."""
    parts = [f"instruction={instruction}"]
    for key, value in sorted((params or {}).items()):
        if value is not None:
            parts.append(f"{key}={_stringify_signing_value(value)}")
    parts.extend((f"timestamp={timestamp}", f"window={window}"))
    return "&".join(parts)


class BackpackClient:
    """Minimal client restricted to public data and read-only balances."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.backpack.exchange",
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout_seconds: float = 10.0,
        window_ms: int = 5_000,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout_seconds = timeout_seconds
        self.window_ms = window_ms
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "backpack-trading-bot/0.1.0"})

    def _private_key(self) -> Ed25519PrivateKey:
        if not self.api_secret:
            raise BackpackAPIError("API Secret이 설정되지 않았습니다.")
        try:
            secret_bytes = base64.b64decode(self.api_secret, validate=True)
            return Ed25519PrivateKey.from_private_bytes(secret_bytes)
        except (ValueError, TypeError) as exc:
            raise BackpackAPIError(
                "API Secret 형식이 올바르지 않습니다. Backpack에서 발급된 값을 확인하세요."
            ) from exc

    def _signed_headers(
        self, instruction: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        if not self.api_key:
            raise BackpackAPIError("API Key가 설정되지 않았습니다.")

        timestamp = int(time.time() * 1_000)
        message = build_signing_message(
            instruction=instruction,
            params=params,
            timestamp=timestamp,
            window=self.window_ms,
        )
        signature = self._private_key().sign(message.encode("utf-8"))

        return {
            "X-API-Key": self.api_key,
            "X-Signature": base64.b64encode(signature).decode("ascii"),
            "X-Timestamp": str(timestamp),
            "X-Window": str(self.window_ms),
        }

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        instruction: str | None = None,
    ) -> Any:
        headers = self._signed_headers(instruction, params) if instruction else None
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise BackpackAPIError(f"Backpack API 연결에 실패했습니다: {exc}") from exc

        if not response.ok:
            detail = response.text.strip()[:300]
            if response.status_code == 401:
                detail = "인증에 실패했습니다. API Key와 API Secret을 다시 확인하세요."
            raise BackpackAPIError(
                f"Backpack API 오류 (HTTP {response.status_code}): {detail or '응답 내용 없음'}"
            )

        if response.status_code == 204:
            return None
        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise BackpackAPIError("Backpack API가 올바른 JSON을 반환하지 않았습니다.") from exc

    def get_status(self) -> dict[str, Any]:
        return self._get("/api/v1/status")

    def get_markets(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/markets")

    def get_assets(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/assets")

    def get_market(self, symbol: str) -> dict[str, Any]:
        return self._get("/api/v1/market", params={"symbol": symbol.upper()})

    def get_mark_price(self, symbol: str) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        mark_prices = self._get(
            "/api/v1/markPrices", params={"symbol": normalized_symbol}
        )
        for mark_price in mark_prices:
            if mark_price.get("symbol") == normalized_symbol:
                return mark_price
        raise BackpackAPIError(f"{normalized_symbol}의 마크 가격을 찾지 못했습니다.")

    def get_funding_rates(
        self, symbol: str, *, limit: int = 1_000, offset: int = 0
    ) -> list[dict[str, Any]]:
        return self._get(
            "/api/v1/fundingRates",
            params={"limit": limit, "offset": offset, "symbol": symbol.upper()},
        )

    def get_ticker(self, symbol: str) -> dict[str, Any] | None:
        return self._get("/api/v1/ticker", params={"symbol": symbol.upper()})

    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        price_type: str = "Last",
    ) -> list[dict[str, Any]]:
        return self._get(
            "/api/v1/klines",
            params={
                "endTime": end_time,
                "interval": interval,
                "priceType": price_type,
                "startTime": start_time,
                "symbol": symbol.upper(),
            },
        )

    def get_balances(self) -> dict[str, dict[str, str]]:
        return self._get("/api/v1/capital", instruction="balanceQuery")

    def get_account(self) -> dict[str, Any]:
        return self._get("/api/v1/account", instruction="accountQuery")

    def get_settlements(
        self, *, limit: int = 1_000, offset: int = 0, sort_direction: str = "Asc"
    ) -> list[dict[str, Any]]:
        return self._get(
            "/wapi/v1/history/settlement",
            params={
                "limit": limit,
                "offset": offset,
                "sortDirection": sort_direction,
            },
            instruction="settlementHistoryQueryAll",
        )
