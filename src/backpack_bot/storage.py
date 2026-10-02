from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backpack_bot.stream import MarketState


class MarketDataStore:
    """SQLite storage for one-second combined public market snapshots."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                bid_price TEXT NOT NULL,
                ask_price TEXT NOT NULL,
                mark_price TEXT NOT NULL,
                index_price TEXT,
                funding_rate TEXT,
                book_event_time_us INTEGER,
                book_engine_time_us INTEGER,
                mark_event_time_us INTEGER,
                mark_engine_time_us INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol_time
            ON market_snapshots(symbol, recorded_at_ms);

            CREATE TABLE IF NOT EXISTS klines (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                price_type TEXT NOT NULL,
                start_time_s INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                open TEXT NOT NULL,
                high TEXT NOT NULL,
                low TEXT NOT NULL,
                close TEXT NOT NULL,
                volume TEXT NOT NULL,
                quote_volume TEXT,
                trades TEXT,
                PRIMARY KEY (symbol, interval, price_type, start_time_s)
            );

            CREATE INDEX IF NOT EXISTS idx_klines_symbol_interval_time
            ON klines(symbol, interval, start_time_s);

            CREATE TABLE IF NOT EXISTS funding_rates (
                symbol TEXT NOT NULL,
                interval_end_s INTEGER NOT NULL,
                interval_end_time TEXT NOT NULL,
                funding_rate TEXT NOT NULL,
                PRIMARY KEY (symbol, interval_end_s)
            );
            """
        )
        self.connection.commit()

    def insert_state(self, state: MarketState) -> int:
        if not state.is_complete:
            raise ValueError("호가와 마크 가격이 모두 있어야 저장할 수 있습니다.")
        cursor = self.connection.execute(
            """
            INSERT INTO market_snapshots (
                recorded_at_ms,
                symbol,
                bid_price,
                ask_price,
                mark_price,
                index_price,
                funding_rate,
                book_event_time_us,
                book_engine_time_us,
                mark_event_time_us,
                mark_engine_time_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1_000),
                state.symbol,
                state.bid_price,
                state.ask_price,
                state.mark_price,
                state.index_price,
                state.funding_rate,
                state.book_event_time_us,
                state.book_engine_time_us,
                state.mark_event_time_us,
                state.mark_engine_time_us,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def statistics(self, symbol: str | None = None) -> dict[str, Any]:
        where = " WHERE symbol = ?" if symbol else ""
        params = (symbol,) if symbol else ()
        row = self.connection.execute(
            f"""
            SELECT
                COUNT(*) AS rows,
                MIN(recorded_at_ms) AS first_recorded_at_ms,
                MAX(recorded_at_ms) AS last_recorded_at_ms
            FROM market_snapshots{where}
            """,
            params,
        ).fetchone()
        return dict(row)

    @staticmethod
    def _iso_to_unix_seconds(value: str) -> int:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())

    def upsert_klines(
        self,
        symbol: str,
        interval: str,
        price_type: str,
        candles: list[dict[str, Any]],
    ) -> int:
        rows = [
            (
                symbol,
                interval,
                price_type,
                self._iso_to_unix_seconds(candle["start"]),
                candle["start"],
                candle["end"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
                candle.get("quoteVolume"),
                candle.get("trades"),
            )
            for candle in candles
        ]
        self.connection.executemany(
            """
            INSERT INTO klines (
                symbol, interval, price_type, start_time_s, start_time, end_time,
                open, high, low, close, volume, quote_volume, trades
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, price_type, start_time_s) DO UPDATE SET
                end_time = excluded.end_time,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                quote_volume = excluded.quote_volume,
                trades = excluded.trades
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def kline_statistics(
        self, symbol: str, interval: str, price_type: str = "Last"
    ) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS rows,
                MIN(start_time) AS first_start,
                MAX(start_time) AS last_start
            FROM klines
            WHERE symbol = ? AND interval = ? AND price_type = ?
            """,
            (symbol, interval, price_type),
        ).fetchone()
        return dict(row)

    def kline_gap_count(
        self,
        symbol: str,
        interval: str,
        interval_seconds: int,
        price_type: str = "Last",
    ) -> int:
        rows = self.connection.execute(
            """
            SELECT start_time_s
            FROM klines
            WHERE symbol = ? AND interval = ? AND price_type = ?
            ORDER BY start_time_s
            """,
            (symbol, interval, price_type),
        ).fetchall()
        timestamps = [int(row[0]) for row in rows]
        return sum(
            max(0, (current - previous) // interval_seconds - 1)
            for previous, current in zip(timestamps, timestamps[1:])
        )

    def load_klines(
        self,
        symbol: str,
        interval: str,
        *,
        price_type: str = "Last",
        completed_before_s: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT start_time_s, start_time, end_time, open, high, low, close,
                   volume, quote_volume, trades
            FROM klines
            WHERE symbol = ? AND interval = ? AND price_type = ?
            ORDER BY start_time_s
            """,
            (symbol, interval, price_type),
        ).fetchall()
        result = [dict(row) for row in rows]
        if completed_before_s is not None:
            result = [
                row
                for row in result
                if self._iso_to_unix_seconds(row["end_time"]) <= completed_before_s
            ]
        return result

    def upsert_funding_rates(self, rates: list[dict[str, Any]]) -> int:
        rows = [
            (
                rate["symbol"],
                self._iso_to_unix_seconds(rate["intervalEndTimestamp"]),
                rate["intervalEndTimestamp"],
                rate["fundingRate"],
            )
            for rate in rates
        ]
        self.connection.executemany(
            """
            INSERT INTO funding_rates (
                symbol, interval_end_s, interval_end_time, funding_rate
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, interval_end_s) DO UPDATE SET
                interval_end_time = excluded.interval_end_time,
                funding_rate = excluded.funding_rate
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def load_funding_rates(
        self, symbol: str, *, start_time_s: int, end_time_s: int
    ) -> dict[int, Decimal]:
        rows = self.connection.execute(
            """
            SELECT interval_end_s, funding_rate
            FROM funding_rates
            WHERE symbol = ? AND interval_end_s >= ? AND interval_end_s <= ?
            ORDER BY interval_end_s
            """,
            (symbol, start_time_s, end_time_s),
        ).fetchall()
        return {int(row["interval_end_s"]): Decimal(row["funding_rate"]) for row in rows}

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> MarketDataStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
