from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from backpack_tax.db import TaxStore
from backpack_tax.historical_pricing import (
    CoinGeckoHistoricalClient,
    HistoricalPriceClient,
    refresh_historical_valuations,
)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class DomesticPriceSession:
    def get(self, _url: str, *, params: dict[str, object], **_kwargs: object) -> FakeResponse:
        assert params["to"] == "2026-08-01T09:01:00"
        prices = {"KRW-XRP": "3000", "KRW-USDT": "1500"}
        market = str(params["market"])
        return FakeResponse(
            [
                {
                    "market": market,
                    "candle_date_time_utc": "2026-08-01T00:00:00",
                    "trade_price": prices[market],
                }
            ]
        )


class OldPriceSession:
    def get(self, _url: str, *, params: dict[str, object], **_kwargs: object) -> FakeResponse:
        return FakeResponse(
            [
                {
                    "market": params["market"],
                    "candle_date_time_utc": "2026-07-31T20:00:00",
                    "trade_price": "1",
                }
            ]
        )


class BackpackPriceSession:
    def get(self, _url: str, *, params: dict[str, object], **_kwargs: object) -> FakeResponse:
        assert params["symbol"] == "SOL_USDC"
        return FakeResponse(
            [
                {
                    "start": "2026-08-01T00:00:00",
                    "end": "2026-08-01T00:01:00",
                    "open": "5",
                    "high": "5",
                    "low": "5",
                    "close": "5",
                    "volume": "1",
                }
            ]
        )


class RateLimitedSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, _url: str, *, params: dict[str, object], **_kwargs: object) -> FakeResponse:
        self.calls += 1
        if self.calls == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": "0"})
        return FakeResponse(
            [
                {
                    "market": params["market"],
                    "candle_date_time_utc": "2026-08-01T00:00:00",
                    "trade_price": "3000",
                }
            ]
        )


class CoinGeckoPriceSession:
    def get(self, url: str, *, params: dict[str, object], **_kwargs: object) -> FakeResponse:
        assert url.endswith("/coins/plasma/market_chart/range")
        assert params["interval"] == "hourly"
        price = "700" if params["vs_currency"] == "krw" else "0.5"
        return FakeResponse({"prices": [[1785542400000, price]]})


def _insert_pending_transfer(
    store: TaxStore,
    *,
    quantity: str,
    asset: str = "XRP",
) -> int:
    cursor = store.connection.execute(
        """
        INSERT INTO normalized_events (
            source_type, occurred_at_utc, event_type, asset, quantity,
            classification_status, classification_reason, created_at_utc
        ) VALUES (
            'BITHUMB', '2026-08-01T00:00:30+00:00', 'DEPOSIT', ?, ?,
            'NEEDS_REVIEW', 'test', '2026-08-01T00:00:00+00:00'
        )
        """,
        (asset, quantity),
    )
    store.connection.commit()
    return int(cursor.lastrowid)


def test_historical_domestic_prices_are_stored_and_drive_dust(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    with TaxStore(database) as store:
        event_id = _insert_pending_transfer(store, quantity="0.4")
        result = refresh_historical_valuations(
            store,
            client=HistoricalPriceClient(DomesticPriceSession()),  # type: ignore[arg-type]
        )
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        evidence_count = store.connection.execute(
            "SELECT COUNT(*) FROM price_evidence"
        ).fetchone()[0]
        statistics = store.review_statistics()

    assert result == {"intrinsic": 0, "attempted": 1, "valued": 1, "unpriced": 0}
    assert valuation["valuation_status"] == "VALUED"
    assert valuation["usd_value"] == "0.8"
    assert valuation["unit_price_usd"] == "2"
    assert valuation["valuation_method"] == "ASSET_KRW_USDT_1M"
    assert evidence_count == 2
    assert statistics["dust_pending"] == 1
    assert statistics["unpriced_pending"] == 0


def test_old_or_missing_price_stays_unpriced_and_is_never_dust(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    with TaxStore(database) as store:
        event_id = _insert_pending_transfer(store, quantity="0.00001")
        result = refresh_historical_valuations(
            store,
            client=HistoricalPriceClient(OldPriceSession()),  # type: ignore[arg-type]
        )
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        statistics = store.review_statistics()
        approved = store.approve_dust_events(
            decided_at_utc="2026-08-01T01:00:00+00:00"
        )

    assert result["unpriced"] == 1
    assert valuation["valuation_status"] == "UNPRICED"
    assert valuation["usd_value"] is None
    assert "15분" in valuation["reason"]
    assert statistics["dust_pending"] == 0
    assert statistics["unpriced_pending"] == 1
    assert approved == 0


def test_domestic_missing_market_uses_coingecko_krw_and_usd_fallback(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.db"
    with TaxStore(database) as store:
        event_id = _insert_pending_transfer(store, quantity="40", asset="XPL")
        result = refresh_historical_valuations(
            store,
            client=HistoricalPriceClient(OldPriceSession()),  # type: ignore[arg-type]
            fallback_client=CoinGeckoHistoricalClient(  # type: ignore[arg-type]
                CoinGeckoPriceSession()
            ),
        )
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        evidence = store.connection.execute(
            "SELECT quote_asset, source_name FROM price_evidence ORDER BY quote_asset"
        ).fetchall()

    assert result["valued"] == 1
    assert result["unpriced"] == 0
    assert valuation["valuation_method"] == "COINGECKO_KRW_USD_HOURLY_FALLBACK"
    assert valuation["confidence"] == "MEDIUM"
    assert valuation["native_value"] == "28000"
    assert valuation["native_currency"] == "KRW"
    assert valuation["usd_value"] == "20.0"
    assert [(row["quote_asset"], row["source_name"]) for row in evidence] == [
        ("KRW", "COINGECKO"),
        ("USD", "COINGECKO"),
    ]


def test_backpack_transfer_uses_event_time_usdc_candle(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    with TaxStore(database) as store:
        cursor = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, classification_reason, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-08-01T00:00:30+00:00', 'WITHDRAWAL',
                'SOL', '0.1', 'NEEDS_REVIEW', 'test', '2026-08-01'
            )
            """
        )
        store.connection.commit()
        event_id = int(cursor.lastrowid)
        refresh_historical_valuations(
            store,
            client=HistoricalPriceClient(BackpackPriceSession()),  # type: ignore[arg-type]
        )
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()

    assert valuation["valuation_status"] == "VALUED"
    assert valuation["usd_value"] == "0.5"
    assert valuation["valuation_method"] == "ASSET_USDC_1M"


def test_krw_withdrawal_is_native_valued_without_coin_price_lookup(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    with TaxStore(database) as store:
        cursor = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                gross_amount, fee_asset, fee_quantity, classification_status,
                classification_reason, created_at_utc
            ) VALUES (
                'BITHUMB', '2026-08-20T08:50:12+00:00', 'WITHDRAWAL',
                'KRW', '173220', '172220', 'KRW', '1000', 'NEEDS_REVIEW',
                'test', '2026-08-20'
            )
            """
        )
        store.connection.commit()
        event_id = int(cursor.lastrowid)
        store.seed_intrinsic_event_valuations()
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        statistics = store.review_statistics()

    assert valuation["valuation_status"] == "VALUED"
    assert valuation["valuation_method"] == "NATIVE_KRW_AMOUNT"
    assert valuation["native_value"] == "173220"
    assert valuation["native_currency"] == "KRW"
    assert valuation["usd_value"] is None
    assert statistics["unpriced_pending"] == 0
    assert statistics["dust_pending"] == 0


def test_price_client_retries_rate_limit_response() -> None:
    session = RateLimitedSession()
    client = HistoricalPriceClient(
        session,  # type: ignore[arg-type]
        request_interval_seconds=0,
        max_rate_limit_retries=1,
    )

    candle = client.candle(
        source_type="UPBIT",
        asset="XRP",
        quote_asset="KRW",
        occurred_at=datetime(2026, 8, 1, 0, 0, 30, tzinfo=UTC),
    )

    assert session.calls == 2
    assert candle.price == 3000


def test_refresh_automatically_processes_more_than_old_fifty_event_batch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.db"
    with TaxStore(database) as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, classification_reason, created_at_utc
            ) VALUES (
                'BITHUMB', '2026-08-01T00:00:30+00:00', 'DEPOSIT', 'XRP',
                '0.4', 'NEEDS_REVIEW', 'test', '2026-08-01'
            )
            """,
            [() for _ in range(75)],
        )
        store.connection.commit()
        result = refresh_historical_valuations(
            store,
            client=HistoricalPriceClient(DomesticPriceSession()),  # type: ignore[arg-type]
        )

    assert result["attempted"] == 75
    assert result["valued"] == 75
    assert result["unpriced"] == 0
