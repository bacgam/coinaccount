from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


DEFAULT_WS_URL = "wss://ws.backpack.exchange"


@dataclass
class MarketState:
    symbol: str
    bid_price: str | None = None
    ask_price: str | None = None
    mark_price: str | None = None
    index_price: str | None = None
    funding_rate: str | None = None
    book_event_time_us: int | None = None
    book_engine_time_us: int | None = None
    mark_event_time_us: int | None = None
    mark_engine_time_us: int | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.bid_price and self.ask_price and self.mark_price)

    def apply(self, envelope: dict[str, Any]) -> bool:
        """Apply a Backpack stream envelope. Return True for known data."""
        stream = envelope.get("stream", "")
        data = envelope.get("data")
        if not isinstance(data, dict):
            return False

        if stream == f"bookTicker.{self.symbol}":
            self.ask_price = data.get("a")
            self.bid_price = data.get("b")
            self.book_event_time_us = data.get("E")
            self.book_engine_time_us = data.get("T")
            return True
        if stream == f"markPrice.{self.symbol}":
            self.mark_price = data.get("p")
            self.funding_rate = data.get("f")
            self.index_price = data.get("i")
            self.mark_event_time_us = data.get("E")
            self.mark_engine_time_us = data.get("T")
            return True
        return False

    def display_line(self, now: datetime | None = None) -> str:
        timestamp = (now or datetime.now()).astimezone().strftime("%H:%M:%S")
        return (
            f"[{timestamp}] {self.symbol} | "
            f"bid {self.bid_price or '-'} | ask {self.ask_price or '-'} | "
            f"mark {self.mark_price or '-'} | funding {self.funding_rate or '-'}"
        )


async def _monitor_forever(
    symbol: str,
    *,
    ws_url: str,
    print_interval_seconds: float,
    output: Callable[[str], None],
    on_snapshot: Callable[[MarketState], None] | None,
) -> None:
    streams = [f"bookTicker.{symbol}", f"markPrice.{symbol}"]
    state = MarketState(symbol=symbol)
    last_printed_at = 0.0

    async for websocket in connect(
        ws_url,
        compression="deflate",
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        max_queue=64,
    ):
        try:
            await websocket.send(json.dumps({"method": "SUBSCRIBE", "params": streams}))
            output(f"연결됨: {', '.join(streams)}")

            async for raw_message in websocket:
                try:
                    envelope = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not state.apply(envelope):
                    continue

                current_time = time.monotonic()
                if current_time - last_printed_at >= print_interval_seconds:
                    output(state.display_line())
                    if on_snapshot is not None and state.is_complete:
                        on_snapshot(state)
                    last_printed_at = current_time
        except ConnectionClosed:
            output("연결이 끊어졌습니다. 자동으로 재연결합니다...")
            continue


async def monitor_market(
    symbol: str,
    *,
    run_seconds: float | None = None,
    ws_url: str = DEFAULT_WS_URL,
    print_interval_seconds: float = 1.0,
    output: Callable[[str], None] = print,
    on_snapshot: Callable[[MarketState], None] | None = None,
) -> None:
    """Monitor public market streams, optionally for a fixed test duration."""
    if run_seconds is not None and run_seconds <= 0:
        raise ValueError("실행 시간은 0초보다 커야 합니다.")
    if print_interval_seconds <= 0:
        raise ValueError("출력 주기는 0초보다 커야 합니다.")

    task = _monitor_forever(
        symbol.upper(),
        ws_url=ws_url,
        print_interval_seconds=print_interval_seconds,
        output=output,
        on_snapshot=on_snapshot,
    )
    if run_seconds is None:
        await task
        return

    try:
        async with asyncio.timeout(run_seconds):
            await task
    except TimeoutError:
        output(f"{run_seconds:g}초 연결 테스트가 완료되었습니다.")
