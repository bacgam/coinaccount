from __future__ import annotations

import time
from collections.abc import Callable

from backpack_bot.client import BackpackClient
from backpack_bot.storage import MarketDataStore


INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}


def download_history(
    client: BackpackClient,
    store: MarketDataStore,
    *,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    price_type: str = "Last",
    candles_per_request: int = 1_000,
    output: Callable[[str], None] = print,
) -> int:
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"지원하지 않는 캔들 주기입니다: {interval}")
    if start_time >= end_time:
        raise ValueError("시작 시간은 종료 시간보다 이전이어야 합니다.")
    if candles_per_request < 1:
        raise ValueError("요청당 캔들 수는 1 이상이어야 합니다.")

    interval_seconds = INTERVAL_SECONDS[interval]
    chunk_seconds = interval_seconds * candles_per_request
    cursor = start_time
    received = 0

    while cursor < end_time:
        chunk_end = min(cursor + chunk_seconds, end_time)
        candles = client.get_klines(
            symbol,
            interval,
            cursor,
            chunk_end,
            price_type=price_type,
        )
        received += store.upsert_klines(symbol, interval, price_type, candles)
        output(f"{cursor} ~ {chunk_end}: {len(candles)}개 수신")
        cursor = chunk_end
        if cursor < end_time:
            time.sleep(0.05)

    return received

