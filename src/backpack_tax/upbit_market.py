from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests


UPBIT_TICKER_URL = "https://api.upbit.com/v1/ticker"
UPBIT_MINUTE_CANDLES_URL = "https://api.upbit.com/v1/candles/minutes/1"
MARKET = "KRW-USDT"


def _response_rows(url: str, *, params: dict[str, object]) -> list[dict[str, Any]]:
    response = requests.get(
        url,
        params=params,
        headers={"Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError("업비트 시세 응답이 비어 있습니다.")
    return rows


def fetch_upbit_usdt_ticker() -> dict[str, object]:
    row = _response_rows(UPBIT_TICKER_URL, params={"markets": MARKET})[0]
    return {
        "market": MARKET,
        "price": float(row["trade_price"]),
        "signed_change_rate": float(row["signed_change_rate"]),
        "timestamp": int(row["timestamp"]),
        "source_url": f"{UPBIT_TICKER_URL}?markets={MARKET}",
    }


def fetch_upbit_usdt_history(*, count: int = 60) -> dict[str, object]:
    safe_count = max(1, min(int(count), 200))
    rows = _response_rows(
        UPBIT_MINUTE_CANDLES_URL,
        params={"market": MARKET, "count": safe_count},
    )
    candles = []
    for row in reversed(rows):
        candle_at = datetime.fromisoformat(str(row["candle_date_time_utc"])).replace(
            tzinfo=UTC
        )
        candles.append(
            {
                "timestamp": int(candle_at.timestamp() * 1000),
                "open": float(row["opening_price"]),
                "high": float(row["high_price"]),
                "low": float(row["low_price"]),
                "close": float(row["trade_price"]),
            }
        )
    return {
        "market": MARKET,
        "unit": "1m",
        "candles": candles,
        "points": [
            {"timestamp": candle["timestamp"], "price": candle["close"]}
            for candle in candles
        ],
        "source_url": f"{UPBIT_MINUTE_CANDLES_URL}?market={MARKET}&count={safe_count}",
    }
