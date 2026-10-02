from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from backpack_tax.db import TaxStore


UPBIT_CANDLES_URL = "https://api.upbit.com/v1/candles/minutes/1"
BITHUMB_CANDLES_URL = "https://api.bithumb.com/v1/candles/minutes/1"
BACKPACK_KLINES_URL = "https://api.backpack.exchange/api/v1/klines"
BACKPACK_ASSETS_URL = "https://api.backpack.exchange/api/v1/assets"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
COINGECKO_PRO_API_URL = "https://pro-api.coingecko.com/api/v3"
MAX_CANDLE_AGE = timedelta(minutes=15)
MAX_COINGECKO_AGE = timedelta(hours=2)

# 티커가 중복되는 자산은 거래소의 공식 영문 자산명과 CoinGecko 항목을
# 대조한 뒤 여기에 고정합니다. 이름이 모호한 자산을 자동 추측하지 않습니다.
COINGECKO_ID_OVERRIDES = {
    "2Z": "doublezero",
    "APT": "aptos",
    "BP": "backpack",
    "CC": "canton-network",
    "ES": "eclipse-3",
    "FOGO": "fogo",
    "GRVT": "grvt",
    "LIT": "lighter",
    "MEGA": "megaeth",
    "MIRA": "mira-3",
    "SWTCH": "switchboard",
    "XPL": "plasma",
    "ZBT": "zerobase",
}


class PriceLookupError(ValueError):
    pass


@dataclass(frozen=True)
class CandleEvidence:
    asset: str
    quote_asset: str
    observed_at_utc: datetime
    price: Decimal
    source_name: str
    source_url: str
    raw_json: str

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw_json.encode("utf-8")).hexdigest()


def _utc_datetime(value: object) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise PriceLookupError("거래 시각이 없어 과거 가격을 조회할 수 없습니다.")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal(value: object, *, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise PriceLookupError(f"{label} 값이 올바르지 않습니다.") from exc
    if result <= 0:
        raise PriceLookupError(f"{label} 값이 0보다 크지 않습니다.")
    return result


def _row_time(row: dict[str, Any]) -> datetime:
    candle_time = str(row.get("candle_date_time_utc") or "")
    if candle_time:
        return _utc_datetime(candle_time)
    timestamp = row.get("timestamp")
    if timestamp is None:
        raise PriceLookupError("캔들 응답에 시각이 없습니다.")
    return datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC)


class HistoricalPriceClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        request_interval_seconds: float | None = None,
        max_rate_limit_retries: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self._cache: dict[tuple[str, str, str, int], CandleEvidence] = {}
        self.request_interval_seconds = (
            0.0 if session is not None else 0.13
        ) if request_interval_seconds is None else max(0.0, request_interval_seconds)
        self.max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._last_request_at = 0.0

    def _get(self, url: str, *, params: dict[str, object]) -> requests.Response:
        for attempt in range(self.max_rate_limit_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.request_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            response = self.session.get(
                url,
                params=params,
                headers={"Accept": "application/json"},
                timeout=10,
            )
            self._last_request_at = time.monotonic()
            if getattr(response, "status_code", 200) != 429:
                response.raise_for_status()
                return response
            if attempt >= self.max_rate_limit_retries:
                response.raise_for_status()
            retry_after = getattr(response, "headers", {}).get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else 0.5 * (2**attempt)
            except (TypeError, ValueError):
                wait_seconds = 0.5 * (2**attempt)
            time.sleep(max(0.5, min(wait_seconds, 5.0)))
        raise PriceLookupError("거래소 가격 조회 재시도에 실패했습니다.")

    def candle(
        self,
        *,
        source_type: str,
        asset: str,
        quote_asset: str,
        occurred_at: datetime,
    ) -> CandleEvidence:
        minute = int(occurred_at.timestamp()) // 60
        key = (source_type.upper(), asset.upper(), quote_asset.upper(), minute)
        if key not in self._cache:
            if source_type.upper() in {"UPBIT", "BITHUMB"}:
                self._cache[key] = self._domestic_candle(
                    source_type=source_type.upper(),
                    asset=asset.upper(),
                    quote_asset=quote_asset.upper(),
                    occurred_at=occurred_at,
                )
            elif source_type.upper() == "BACKPACK":
                self._cache[key] = self._backpack_candle(
                    asset=asset.upper(),
                    quote_asset=quote_asset.upper(),
                    occurred_at=occurred_at,
                )
            else:
                raise PriceLookupError(f"지원하지 않는 거래소입니다: {source_type}")
        return self._cache[key]

    def _domestic_candle(
        self,
        *,
        source_type: str,
        asset: str,
        quote_asset: str,
        occurred_at: datetime,
    ) -> CandleEvidence:
        base_url = UPBIT_CANDLES_URL if source_type == "UPBIT" else BITHUMB_CANDLES_URL
        market = f"{quote_asset}-{asset}"
        query_to = occurred_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        to_value = (
            query_to.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%dT%H:%M:%S")
            if source_type == "BITHUMB"
            else query_to.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        params = {
            "market": market,
            "to": to_value,
            "count": 1,
        }
        response = self._get(base_url, params=params)
        rows = response.json()
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise PriceLookupError(f"{source_type} {market} 과거 가격이 없습니다.")
        row = rows[0]
        observed_at = _row_time(row)
        self._validate_candle_time(observed_at, occurred_at)
        raw_json = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return CandleEvidence(
            asset=asset,
            quote_asset=quote_asset,
            observed_at_utc=observed_at,
            price=_decimal(row.get("trade_price"), label="캔들 종가"),
            source_name=source_type,
            source_url=f"{base_url}?{urlencode(params)}",
            raw_json=raw_json,
        )

    def _backpack_candle(
        self,
        *,
        asset: str,
        quote_asset: str,
        occurred_at: datetime,
    ) -> CandleEvidence:
        symbol = f"{asset}_{quote_asset}"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": int((occurred_at - MAX_CANDLE_AGE).timestamp()),
            "endTime": int((occurred_at + timedelta(minutes=1)).timestamp()),
            "priceType": "Last",
        }
        response = self._get(BACKPACK_KLINES_URL, params=params)
        rows = response.json()
        if not isinstance(rows, list):
            raise PriceLookupError(f"Backpack {symbol} 과거 가격이 없습니다.")
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                observed_at = _utc_datetime(row.get("start"))
            except (PriceLookupError, ValueError):
                continue
            if observed_at <= occurred_at:
                candidates.append((observed_at, row))
        if not candidates:
            raise PriceLookupError(f"Backpack {symbol} 과거 가격이 없습니다.")
        observed_at, row = max(candidates, key=lambda item: item[0])
        self._validate_candle_time(observed_at, occurred_at)
        raw_json = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return CandleEvidence(
            asset=asset,
            quote_asset=quote_asset,
            observed_at_utc=observed_at,
            price=_decimal(row.get("close"), label="캔들 종가"),
            source_name="BACKPACK",
            source_url=f"{BACKPACK_KLINES_URL}?{urlencode(params)}",
            raw_json=raw_json,
        )

    @staticmethod
    def _validate_candle_time(observed_at: datetime, occurred_at: datetime) -> None:
        age = occurred_at - observed_at
        if age < timedelta(0) or age > MAX_CANDLE_AGE:
            raise PriceLookupError("거래 시각에서 15분 이내인 과거 캔들을 찾지 못했습니다.")


class CoinGeckoHistoricalClient:
    """원 거래소 가격이 없을 때만 사용하는 감사 가능한 대체 가격 조회기."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        request_interval_seconds: float | None = None,
        max_rate_limit_retries: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self.pro_api_key = os.getenv("COINGECKO_PRO_API_KEY", "").strip()
        self.demo_api_key = os.getenv("COINGECKO_API_KEY", "").strip()
        self.base_url = COINGECKO_PRO_API_URL if self.pro_api_key else COINGECKO_API_URL
        self.request_interval_seconds = (
            0.0 if session is not None else 6.0
        ) if request_interval_seconds is None else max(0.0, request_interval_seconds)
        self.max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._last_request_at = 0.0
        self._coin_list: list[dict[str, Any]] | None = None
        self._backpack_ids: dict[str, str] | None = None
        self._id_cache: dict[tuple[str, str], str] = {}
        self._price_cache: dict[tuple[str, str, int], CandleEvidence] = {}

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Coinaccount/0.1 historical-price-evidence",
        }
        if self.pro_api_key:
            headers["x-cg-pro-api-key"] = self.pro_api_key
        elif self.demo_api_key:
            headers["x-cg-demo-api-key"] = self.demo_api_key
        return headers

    def _get(self, url: str, *, params: dict[str, object] | None = None) -> requests.Response:
        for attempt in range(self.max_rate_limit_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.request_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            response = self.session.get(
                url,
                params=params or {},
                headers=self._headers(),
                timeout=30,
            )
            self._last_request_at = time.monotonic()
            status_code = getattr(response, "status_code", 200)
            if status_code == 401:
                raise PriceLookupError(
                    "CoinGecko 무료 API의 과거 조회 허용 범위를 벗어났습니다. "
                    "API 키를 설정하거나 수동 가격 증빙이 필요합니다."
                )
            if status_code != 429:
                response.raise_for_status()
                return response
            if attempt >= self.max_rate_limit_retries:
                raise PriceLookupError(
                    "CoinGecko 무료 API 호출 한도를 초과했습니다. 잠시 후 다시 실행해 주세요."
                )
            retry_after = getattr(response, "headers", {}).get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else 1.5 * (2**attempt)
            except (TypeError, ValueError):
                wait_seconds = 1.5 * (2**attempt)
            time.sleep(max(1.0, min(wait_seconds, 10.0)))
        raise PriceLookupError("CoinGecko 가격 조회 재시도에 실패했습니다.")

    def _load_backpack_ids(self) -> dict[str, str]:
        if self._backpack_ids is None:
            response = self._get(BACKPACK_ASSETS_URL)
            rows = response.json()
            if not isinstance(rows, list):
                raise PriceLookupError("Backpack 자산 식별자 응답이 올바르지 않습니다.")
            self._backpack_ids = {
                str(row.get("symbol") or "").upper(): str(row["coingeckoId"])
                for row in rows
                if isinstance(row, dict) and row.get("symbol") and row.get("coingeckoId")
            }
        return self._backpack_ids

    def _load_coin_list(self) -> list[dict[str, Any]]:
        if self._coin_list is None:
            response = self._get(f"{self.base_url}/coins/list")
            rows = response.json()
            if not isinstance(rows, list):
                raise PriceLookupError("CoinGecko 코인 목록 응답이 올바르지 않습니다.")
            self._coin_list = [row for row in rows if isinstance(row, dict)]
        return self._coin_list

    def coin_id(self, *, source_type: str, asset: str) -> str:
        source = source_type.upper()
        symbol = asset.upper()
        key = (source, symbol)
        if key in self._id_cache:
            return self._id_cache[key]
        coin_id = COINGECKO_ID_OVERRIDES.get(symbol)
        if source == "BACKPACK":
            coin_id = self._load_backpack_ids().get(symbol) or coin_id
        if not coin_id:
            matches = [
                row for row in self._load_coin_list()
                if str(row.get("symbol") or "").upper() == symbol
            ]
            if len(matches) == 1 and matches[0].get("id"):
                coin_id = str(matches[0]["id"])
            elif len(matches) > 1:
                raise PriceLookupError(
                    f"CoinGecko에서 {symbol} 티커가 여러 자산과 겹쳐 자동 선택할 수 없습니다."
                )
        if not coin_id:
            raise PriceLookupError(f"CoinGecko에서 {symbol} 자산 식별자를 찾지 못했습니다.")
        self._id_cache[key] = coin_id
        return coin_id

    def price(
        self,
        *,
        source_type: str,
        asset: str,
        quote_asset: str,
        occurred_at: datetime,
    ) -> CandleEvidence:
        coin_id = self.coin_id(source_type=source_type, asset=asset)
        quote = quote_asset.lower()
        hour = int(occurred_at.timestamp()) // 3600
        key = (coin_id, quote, hour)
        if key in self._price_cache:
            return self._price_cache[key]
        params: dict[str, object] = {
            "vs_currency": quote,
            "from": int((occurred_at - MAX_COINGECKO_AGE).timestamp()),
            "to": int((occurred_at + timedelta(hours=1)).timestamp()),
            "interval": "hourly",
        }
        url = f"{self.base_url}/coins/{coin_id}/market_chart/range"
        response = self._get(url, params=params)
        payload = response.json()
        rows = payload.get("prices") if isinstance(payload, dict) else None
        candidates: list[tuple[datetime, Decimal, list[object]]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                try:
                    observed_at = datetime.fromtimestamp(float(row[0]) / 1000, tz=UTC)
                    price = _decimal(row[1], label="CoinGecko 가격")
                except (PriceLookupError, TypeError, ValueError, OSError):
                    continue
                candidates.append((observed_at, price, row))
        if not candidates:
            raise PriceLookupError(f"CoinGecko에 {coin_id}/{quote.upper()} 과거 가격이 없습니다.")
        observed_at, price, raw_row = min(
            candidates,
            key=lambda item: abs(item[0] - occurred_at),
        )
        if abs(observed_at - occurred_at) > MAX_COINGECKO_AGE:
            raise PriceLookupError("거래 시각에서 2시간 이내인 CoinGecko 가격을 찾지 못했습니다.")
        raw_json = json.dumps(
            {"coin_id": coin_id, "vs_currency": quote, "price_point": raw_row},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence = CandleEvidence(
            asset=asset.upper(),
            quote_asset=quote.upper(),
            observed_at_utc=observed_at,
            price=price,
            source_name="COINGECKO",
            source_url=f"{url}?{urlencode(params)}",
            raw_json=raw_json,
        )
        self._price_cache[key] = evidence
        return evidence


def _valuation_amount(event: dict[str, Any]) -> tuple[Decimal, str, str]:
    quote_asset = str(event.get("quote_asset") or "").upper()
    if event.get("gross_amount") is not None and quote_asset:
        return (
            abs(_decimal(event["gross_amount"], label="거래금액")),
            quote_asset,
            "SOURCE_GROSS_AMOUNT",
        )
    if event.get("realized_pnl") is not None and quote_asset:
        return (
            abs(_decimal(event["realized_pnl"], label="실현손익")),
            quote_asset,
            "SOURCE_REALIZED_PNL",
        )
    asset = str(event.get("asset") or "").upper()
    if event.get("quantity") is None or not asset:
        raise PriceLookupError("평가할 자산 수량이 없습니다.")
    return abs(_decimal(event["quantity"], label="수량")), asset, "HISTORICAL_CANDLE"


def _store_evidence(store: TaxStore, candle: CandleEvidence) -> int:
    return store.upsert_price_evidence(
        asset=candle.asset,
        quote_asset=candle.quote_asset,
        observed_at_utc=candle.observed_at_utc.isoformat(),
        price=candle.price,
        source_name=candle.source_name,
        source_url=candle.source_url,
        raw_sha256=candle.raw_sha256,
        raw_json=candle.raw_json,
    )


def value_event(
    store: TaxStore,
    client: HistoricalPriceClient,
    event: dict[str, Any],
    *,
    fallback_client: CoinGeckoHistoricalClient | None = None,
) -> None:
    event_id = int(event["id"])
    try:
        occurred_at = _utc_datetime(event.get("occurred_at_utc"))
        amount, value_asset, amount_method = _valuation_amount(event)
        source_type = str(event.get("source_type") or "").upper()
        evidence: list[tuple[int, str]] = []
        native_value: Decimal | None = None
        native_currency: str | None = None
        if value_asset == "USD":
            unit_price_usd = Decimal("1")
            method = amount_method
            confidence = "EXACT"
            reason = "원본 USD 금액을 사용했습니다."
        elif value_asset in {"USDC", "USDT"}:
            unit_price_usd = Decimal("1")
            method = f"{amount_method}_STABLECOIN_PARITY"
            confidence = "HIGH"
            reason = f"원본 {value_asset} 금액에 USD 등가 기준을 적용했습니다."
        elif value_asset == "KRW":
            krw_usdt = client.candle(
                source_type=source_type,
                asset="USDT",
                quote_asset="KRW",
                occurred_at=occurred_at,
            )
            evidence.append((_store_evidence(store, krw_usdt), "USD_CONVERSION"))
            unit_price_usd = Decimal("1") / krw_usdt.price
            method = f"{amount_method}_KRW_USDT_1M"
            confidence = "HIGH"
            reason = f"{source_type} 거래 시각의 KRW-USDT 1분봉 종가로 환산했습니다."
        elif source_type in {"UPBIT", "BITHUMB"}:
            try:
                asset_krw = client.candle(
                    source_type=source_type,
                    asset=value_asset,
                    quote_asset="KRW",
                    occurred_at=occurred_at,
                )
                krw_usdt = client.candle(
                    source_type=source_type,
                    asset="USDT",
                    quote_asset="KRW",
                    occurred_at=occurred_at,
                )
                evidence.extend(
                    [
                        (_store_evidence(store, asset_krw), "ASSET_PRICE"),
                        (_store_evidence(store, krw_usdt), "USD_CONVERSION"),
                    ]
                )
                unit_price_usd = asset_krw.price / krw_usdt.price
                native_value = amount * asset_krw.price
                native_currency = "KRW"
                method = "ASSET_KRW_USDT_1M"
                confidence = "HIGH"
                reason = f"{source_type} 거래 시각의 {value_asset}-KRW와 USDT-KRW 1분봉 종가로 환산했습니다."
            except (PriceLookupError, requests.RequestException, ValueError, KeyError) as primary_exc:
                if fallback_client is None:
                    raise primary_exc
                asset_krw = fallback_client.price(
                    source_type=source_type,
                    asset=value_asset,
                    quote_asset="KRW",
                    occurred_at=occurred_at,
                )
                evidence.append(
                    (_store_evidence(store, asset_krw), "ASSET_PRICE_KRW_FALLBACK")
                )
                try:
                    krw_usdt = client.candle(
                        source_type=source_type,
                        asset="USDT",
                        quote_asset="KRW",
                        occurred_at=occurred_at,
                    )
                    evidence.append(
                        (_store_evidence(store, krw_usdt), "USD_CONVERSION")
                    )
                    unit_price_usd = asset_krw.price / krw_usdt.price
                    method = "COINGECKO_KRW_EXCHANGE_USDT_1M_FALLBACK"
                    conversion_reason = f"{source_type} USDT-KRW 1분봉으로 USD 보조값을 환산했습니다."
                except (PriceLookupError, requests.RequestException, ValueError, KeyError):
                    asset_usd = fallback_client.price(
                        source_type=source_type,
                        asset=value_asset,
                        quote_asset="USD",
                        occurred_at=occurred_at,
                    )
                    evidence.append(
                        (_store_evidence(store, asset_usd), "USD_VALUE_FALLBACK")
                    )
                    unit_price_usd = asset_usd.price
                    method = "COINGECKO_KRW_USD_HOURLY_FALLBACK"
                    conversion_reason = "CoinGecko USD 시세로 USD 보조값을 계산했습니다."
                native_value = amount * asset_krw.price
                native_currency = "KRW"
                confidence = "MEDIUM"
                reason = (
                    f"{source_type} 분봉을 찾지 못해 CoinGecko의 거래 시각 인접 과거 시세를 "
                    f"대체 적용했습니다. 국내 거래소 평가액은 KRW 기준이며, {conversion_reason}"
                )
        else:
            try:
                asset_usdc = client.candle(
                    source_type="BACKPACK",
                    asset=value_asset,
                    quote_asset="USDC",
                    occurred_at=occurred_at,
                )
                evidence.append((_store_evidence(store, asset_usdc), "ASSET_PRICE"))
                unit_price_usd = asset_usdc.price
                method = "ASSET_USDC_1M"
                confidence = "HIGH"
                reason = f"Backpack 거래 시각의 {value_asset}-USDC 1분봉 종가로 환산했습니다."
            except (PriceLookupError, requests.RequestException, ValueError, KeyError) as primary_exc:
                if fallback_client is None:
                    raise primary_exc
                asset_usd = fallback_client.price(
                    source_type=source_type,
                    asset=value_asset,
                    quote_asset="USD",
                    occurred_at=occurred_at,
                )
                evidence.append((_store_evidence(store, asset_usd), "ASSET_PRICE_USD_FALLBACK"))
                unit_price_usd = asset_usd.price
                method = "COINGECKO_USD_HOURLY_FALLBACK"
                confidence = "MEDIUM"
                reason = (
                    "Backpack 분봉을 찾지 못해 CoinGecko의 거래 시각 인접 USD 과거 시세를 "
                    "대체 적용했습니다."
                )
            native_value = amount * unit_price_usd
            native_currency = "USD"
        store.upsert_event_valuation(
            event_id=event_id,
            valuation_status="VALUED",
            usd_value=amount * unit_price_usd,
            unit_price_usd=unit_price_usd,
            valuation_method=method,
            confidence=confidence,
            reason=reason,
            valued_at_utc=occurred_at.isoformat(),
            evidence=evidence,
            native_value=native_value,
            native_currency=native_currency,
        )
    except (PriceLookupError, requests.RequestException, ValueError, KeyError) as exc:
        store.upsert_event_valuation(
            event_id=event_id,
            valuation_status="UNPRICED",
            usd_value=None,
            unit_price_usd=None,
            valuation_method="PRICE_UNAVAILABLE",
            confidence="NONE",
            reason=str(exc) or "과거 가격을 확인하지 못했습니다.",
            valued_at_utc=event.get("occurred_at_utc"),
        )


def refresh_historical_valuations(
    store: TaxStore,
    *,
    source_type: str | None = None,
    event_type: str | None = None,
    limit: int = 500,
    client: HistoricalPriceClient | None = None,
    fallback_client: CoinGeckoHistoricalClient | None = None,
) -> dict[str, int]:
    intrinsic = store.seed_intrinsic_event_valuations()
    events = store.list_events_needing_valuation(
        source_type=source_type,
        event_type=event_type,
        limit=limit,
    )
    price_client = client or HistoricalPriceClient()
    fallback_price_client = (
        fallback_client
        if fallback_client is not None or client is not None
        else CoinGeckoHistoricalClient()
    )
    for event in events:
        value_event(
            store,
            price_client,
            event,
            fallback_client=fallback_price_client,
        )
    valued = 0
    unpriced = 0
    if events:
        placeholders = ",".join("?" for _ in events)
        rows = store.connection.execute(
            f"""
            SELECT valuation_status, COUNT(*) AS count
            FROM event_valuations
            WHERE normalized_event_id IN ({placeholders})
            GROUP BY valuation_status
            """,
            [int(event["id"]) for event in events],
        ).fetchall()
        counts = {str(row["valuation_status"]): int(row["count"]) for row in rows}
        valued = counts.get("VALUED", 0)
        unpriced = counts.get("UNPRICED", 0)
    return {
        "intrinsic": intrinsic,
        "attempted": len(events),
        "valued": valued,
        "unpriced": unpriced,
    }
