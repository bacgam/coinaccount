from __future__ import annotations

import base64
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backpack_bot.client import BackpackClient, build_signing_message
from backpack_bot.stream import MarketState
from backpack_bot.storage import MarketDataStore
from backpack_bot.history import download_history
from backpack_bot.paper import PaperBroker
from backpack_bot.strategy import (
    Candle,
    analyze_ema_trend,
    analyze_ema_vwma_long,
    analyze_smc_swing_bos_long,
    analyze_bollinger_cci_long,
    exponential_moving_average,
    volume_weighted_moving_average,
)
from backpack_bot.risk import calculate_position_size
from backpack_bot.backtest import (
    run_ema_backtest,
    run_ema_vwma_long_backtest,
    run_smc_swing_bos_long_backtest,
    run_bollinger_cci_long_backtest,
)


def test_build_signing_message_sorts_params_and_formats_boolean() -> None:
    message = build_signing_message(
        instruction="example",
        params={"symbol": "SOL_USDC", "postOnly": True, "ignored": None},
        timestamp=1_614_550_000_000,
        window=5_000,
    )

    assert message == (
        "instruction=example&postOnly=true&symbol=SOL_USDC"
        "&timestamp=1614550000000&window=5000"
    )


def test_signed_headers_produce_verifiable_signature(monkeypatch) -> None:
    seed = bytes(range(32))
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private_key.public_key()
    client = BackpackClient(
        api_key="test-public-key",
        api_secret=base64.b64encode(seed).decode("ascii"),
    )
    monkeypatch.setattr("backpack_bot.client.time.time", lambda: 1_614_550_000.0)

    headers = client._signed_headers("balanceQuery")

    expected_message = b"instruction=balanceQuery&timestamp=1614550000000&window=5000"
    public_key.verify(base64.b64decode(headers["X-Signature"]), expected_message)
    assert headers["X-API-Key"] == "test-public-key"
    assert headers["X-Timestamp"] == "1614550000000"
    assert headers["X-Window"] == "5000"


def test_get_mark_price_selects_requested_symbol() -> None:
    class FakeClient(BackpackClient):
        def _get(self, path, *, params=None, instruction=None):
            assert path == "/api/v1/markPrices"
            assert params == {"symbol": "BTC_USDC_PERP"}
            return [
                {
                    "symbol": "BTC_USDC_PERP",
                    "markPrice": "77000.1",
                    "fundingRate": "0.00001",
                }
            ]

    result = FakeClient().get_mark_price("btc_usdc_perp")

    assert result["markPrice"] == "77000.1"


def test_get_settlements_uses_read_only_history_endpoint() -> None:
    class FakeClient(BackpackClient):
        def _get(self, path, *, params=None, instruction=None):
            assert path == "/wapi/v1/history/settlement"
            assert params == {"limit": 1000, "offset": 0, "sortDirection": "Asc"}
            assert instruction == "settlementHistoryQueryAll"
            return [{"engineSequence": 1}]

    assert FakeClient().get_settlements() == [{"engineSequence": 1}]


def test_market_state_combines_book_and_mark_price() -> None:
    state = MarketState(symbol="BTC_USDC_PERP")

    assert state.apply(
        {
            "stream": "bookTicker.BTC_USDC_PERP",
            "data": {"b": "77000.0", "a": "77000.1"},
        }
    )
    assert state.apply(
        {
            "stream": "markPrice.BTC_USDC_PERP",
            "data": {"p": "77001.0", "i": "77002.0", "f": "0.00001"},
        }
    )

    assert state.bid_price == "77000.0"
    assert state.ask_price == "77000.1"
    assert state.mark_price == "77001.0"
    assert state.index_price == "77002.0"
    assert state.funding_rate == "0.00001"
    assert state.is_complete


def test_market_data_store_inserts_complete_snapshot(tmp_path) -> None:
    state = MarketState(
        symbol="BTC_USDC_PERP",
        bid_price="77000.0",
        ask_price="77000.1",
        mark_price="77001.0",
        index_price="77002.0",
        funding_rate="0.00001",
        book_event_time_us=100,
        book_engine_time_us=101,
        mark_event_time_us=102,
        mark_engine_time_us=103,
    )

    with MarketDataStore(tmp_path / "test.db") as store:
        row_id = store.insert_state(state)
        stats = store.statistics("BTC_USDC_PERP")

    assert row_id == 1
    assert stats["rows"] == 1
    assert stats["first_recorded_at_ms"] is not None


def test_download_history_upserts_duplicate_candles(tmp_path) -> None:
    candle = {
        "start": "2026-08-23 00:00:00",
        "end": "2026-08-23 00:01:00",
        "open": "77000",
        "high": "77100",
        "low": "76900",
        "close": "77050",
        "volume": "10",
        "quoteVolume": "770500",
        "trades": "42",
    }

    class FakeClient:
        def get_klines(self, *args, **kwargs):
            return [candle]

    with MarketDataStore(tmp_path / "test.db") as store:
        for _ in range(2):
            download_history(
                FakeClient(),
                store,
                symbol="BTC_USDC_PERP",
                interval="1m",
                start_time=1_787_443_200,
                end_time=1_787_443_260,
                output=lambda _: None,
            )
        stats = store.kline_statistics("BTC_USDC_PERP", "1m")

    assert stats["rows"] == 1


def test_kline_gap_count_reports_missing_intervals(tmp_path) -> None:
    candles = []
    for minute in (0, 2):
        candles.append(
            {
                "start": f"2026-08-23 00:0{minute}:00",
                "end": f"2026-08-23 00:0{minute + 1}:00",
                "open": "1",
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "0",
                "quoteVolume": "0",
                "trades": "0",
            }
        )

    with MarketDataStore(tmp_path / "test.db") as store:
        store.upsert_klines("BTC_USDC_PERP", "1m", "Last", candles)
        gaps = store.kline_gap_count("BTC_USDC_PERP", "1m", 60)

    assert gaps == 1


def test_paper_broker_long_round_trip_calculates_pnl_and_fees(tmp_path) -> None:
    with MarketDataStore(tmp_path / "test.db") as store:
        broker = PaperBroker(store.connection)
        broker.initialize(
            Decimal("1000"),
            maker_fee_bps=Decimal("2"),
            taker_fee_bps=Decimal("5"),
            max_leverage=Decimal("2"),
            daily_loss_limit_pct=Decimal("2"),
            risk_per_trade_pct=Decimal("0.5"),
            max_daily_entries=10,
        )
        broker.apply_fill(
            symbol="BTC_USDC_PERP",
            side="Bid",
            quantity=Decimal("0.01"),
            price=Decimal("70000"),
        )
        position = broker.apply_fill(
            symbol="BTC_USDC_PERP",
            side="Ask",
            quantity=Decimal("0.01"),
            price=Decimal("71000"),
        )
        status = broker.status("BTC_USDC_PERP", Decimal("71000"))

    assert position.quantity == Decimal("0")
    assert status["realizedPnl"] == Decimal("10.00")
    assert status["feesPaid"] == Decimal("0.705")
    assert status["equity"] == Decimal("1009.295")


def test_paper_broker_rejects_fill_over_max_leverage(tmp_path) -> None:
    with MarketDataStore(tmp_path / "test.db") as store:
        broker = PaperBroker(store.connection)
        broker.initialize(
            Decimal("1000"),
            maker_fee_bps=Decimal("2"),
            taker_fee_bps=Decimal("5"),
            max_leverage=Decimal("2"),
            daily_loss_limit_pct=Decimal("2"),
            risk_per_trade_pct=Decimal("0.5"),
            max_daily_entries=10,
        )

        try:
            broker.apply_fill(
                symbol="BTC_USDC_PERP",
                side="Bid",
                quantity=Decimal("0.03"),
                price=Decimal("70000"),
            )
        except ValueError as exc:
            assert "최대 레버리지" in str(exc)
        else:
            raise AssertionError("레버리지 초과 체결이 거부되지 않았습니다.")

        position = broker.position("BTC_USDC_PERP")

    assert position.quantity == Decimal("0")


def test_daily_loss_limit_blocks_increase_but_allows_close(tmp_path) -> None:
    with MarketDataStore(tmp_path / "test.db") as store:
        broker = PaperBroker(store.connection)
        broker.initialize(
            Decimal("1000"),
            maker_fee_bps=Decimal("2"),
            taker_fee_bps=Decimal("5"),
            max_leverage=Decimal("2"),
            daily_loss_limit_pct=Decimal("2"),
            risk_per_trade_pct=Decimal("0.5"),
            max_daily_entries=10,
        )
        broker.apply_fill(
            symbol="BTC_USDC_PERP",
            side="Bid",
            quantity=Decimal("10"),
            price=Decimal("100"),
        )
        status = broker.status("BTC_USDC_PERP", Decimal("98"))

        assert status["tradingHalted"] is True
        assert status["dailyLoss"] == Decimal("20.500")

        try:
            broker.apply_fill(
                symbol="BTC_USDC_PERP",
                side="Bid",
                quantity=Decimal("1"),
                price=Decimal("98"),
            )
        except ValueError as exc:
            assert "일일 손실 한도" in str(exc)
        else:
            raise AssertionError("손실 한도 이후 증액 체결이 거부되지 않았습니다.")

        closed = broker.apply_fill(
            symbol="BTC_USDC_PERP",
            side="Ask",
            quantity=Decimal("10"),
            price=Decimal("98"),
        )

    assert closed.quantity == Decimal("0")


def test_exponential_moving_average_uses_sma_seed() -> None:
    values = [Decimal("1"), Decimal("2"), Decimal("3")]

    result = exponential_moving_average(values, period=2)

    assert result == [None, Decimal("1.5"), Decimal("2.5")]


def test_volume_weighted_moving_average_uses_close_and_volume() -> None:
    candles = [
        Candle(
            start_time_s=index * 60,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
        )
        for index, (close, volume) in enumerate(
            ((Decimal("1"), Decimal("1")), (Decimal("3"), Decimal("3")))
        )
    ]

    result = volume_weighted_moving_average(candles, period=2)

    assert result == [None, Decimal("2.5")]


def test_ema_vwma_long_signal_requires_trend_and_completed_cross() -> None:
    closes = [Decimal(value) for value in (1, 1, 1, 1, 1, 3)]
    volumes = [Decimal(value) for value in (1, 1, 1, 1, 100, 100)]
    candles = [
        Candle(
            start_time_s=index * 60,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]

    points = analyze_ema_vwma_long(
        candles, fast_period=2, slow_period=4, vwma_period=2
    )

    assert points[-2].signal is None
    assert points[-1].signal == "LONG"
    assert points[-1].trend_aligned is True
    assert points[-1].crossed_above is True


def test_smc_bullish_swing_bos_requires_rising_ema_alignment() -> None:
    closes = [Decimal(value) for value in (1, 4, 3, 4, 6)]
    highs = [Decimal(value) for value in (1, 5, "3.5", "4.5", "6.5")]
    lows = [Decimal(value) for value in (0, 1, 2, "2.5", 3)]
    candles = [
        Candle(
            start_time_s=index * 300,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=highs[index],
            low=lows[index],
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    points = analyze_smc_swing_bos_long(
        candles, fast_period=2, slow_period=3, swing_length=2
    )

    assert points[-2].signal is None
    assert points[-1].swing_high == Decimal("5")
    assert points[-1].bullish_swing_bos is True
    assert points[-1].ema_trend_aligned is True
    assert points[-1].ema_both_rising is True
    assert points[-1].signal == "LONG"


def test_smc_bullish_choch_is_not_treated_as_bos() -> None:
    closes = [Decimal(value) for value in (1, 4, 3, 4, 6, 1, 3, 7)]
    highs = [Decimal(value) for value in (1, 5, "3.5", "4.5", "6.5", 2, "3.5", "7.5")]
    lows = [Decimal(value) for value in (0, 1, 2, "2.5", 3, "0.5", "1.5", 4)]
    candles = [
        Candle(
            start_time_s=index * 300,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=highs[index],
            low=lows[index],
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    points = analyze_smc_swing_bos_long(
        candles, fast_period=2, slow_period=3, swing_length=2
    )

    assert points[5].structure_trend == -1
    assert points[-1].structure_trend == 1
    assert points[-1].bullish_swing_bos is False
    assert points[-1].signal is None


def test_smc_bos_can_emit_signal_with_ema_filter_disabled() -> None:
    closes = [Decimal(value) for value in (1, 4, 3, 4, 6)]
    highs = [Decimal(value) for value in (1, 5, "3.5", "4.5", "6.5")]
    lows = [Decimal(value) for value in (0, 1, 2, "2.5", 3)]
    candles = [
        Candle(
            start_time_s=index * 300,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=highs[index],
            low=lows[index],
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    point = analyze_smc_swing_bos_long(
        candles,
        fast_period=2,
        slow_period=10,
        swing_length=2,
        require_ema_filter=False,
    )[-1]

    assert point.slow_ema is None
    assert point.bullish_swing_bos is True
    assert point.signal == "LONG"


def test_bollinger_lower_touch_and_cci_recovery_emit_long() -> None:
    closes = [Decimal(value) for value in (10, 9, 8, "8.5")]
    highs = [Decimal(value) for value in ("10.2", "9.2", "8.2", "8.7")]
    lows = [Decimal(value) for value in ("9.8", "8.8", "7.8", "7.6")]
    candles = [
        Candle(
            start_time_s=index * 14_400,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=highs[index],
            low=lows[index],
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    points = analyze_bollinger_cci_long(
        candles,
        bollinger_period=3,
        bollinger_stddev_multiple=Decimal("2"),
        cci_period=3,
    )

    assert points[-2].cci == Decimal("-100")
    assert Decimal("-100") <= points[-1].cci < Decimal("0")
    assert points[-1].lower_band_touched is True
    assert points[-1].cci_rising_to_zero is True
    assert points[-1].signal == "LONG"


def test_ema_trend_emits_long_only_on_completed_cross() -> None:
    closes = [Decimal(value) for value in (3, 2, 1, 2, 3)]
    candles = [
        Candle(
            start_time_s=index * 60,
            start_time=f"start-{index}",
            end_time=f"end-{index}",
            open=close,
            high=close + Decimal("1"),
            low=close - Decimal("1"),
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    points = analyze_ema_trend(
        candles, fast_period=2, slow_period=3, atr_period=2
    )

    assert points[-2].signal is None
    assert points[-1].signal == "LONG"
    assert points[-1].regime == "LONG"


def test_daily_entry_limit_does_not_block_exit(tmp_path) -> None:
    with MarketDataStore(tmp_path / "test.db") as store:
        broker = PaperBroker(store.connection)
        broker.initialize(
            Decimal("1000"),
            maker_fee_bps=Decimal("2"),
            taker_fee_bps=Decimal("5"),
            max_leverage=Decimal("2"),
            daily_loss_limit_pct=Decimal("2"),
            risk_per_trade_pct=Decimal("0.5"),
            max_daily_entries=1,
        )
        broker.apply_fill(
            symbol="BTC_USDC_PERP",
            side="Bid",
            quantity=Decimal("1"),
            price=Decimal("100"),
        )
        closed = broker.apply_fill(
            symbol="BTC_USDC_PERP",
            side="Ask",
            quantity=Decimal("1"),
            price=Decimal("100"),
        )
        assert closed.quantity == Decimal("0")

        try:
            broker.apply_fill(
                symbol="BTC_USDC_PERP",
                side="Bid",
                quantity=Decimal("1"),
                price=Decimal("100"),
            )
        except ValueError as exc:
            assert "하루 신규 진입 한도" in str(exc)
        else:
            raise AssertionError("하루 신규 진입 한도가 적용되지 않았습니다.")


def test_position_size_uses_atr_risk_and_quantity_step() -> None:
    result = calculate_position_size(
        equity=Decimal("1000"),
        entry_price=Decimal("77000"),
        stop_distance=Decimal("500"),
        risk_pct=Decimal("0.5"),
        max_leverage=Decimal("2"),
        quantity_step=Decimal("0.00001"),
        entry_fee_bps=Decimal("5"),
    )

    assert result.quantity == Decimal("0.01000")
    assert result.risk_budget == Decimal("5.0")
    assert result.limited_by == "RISK"


def test_backtest_enters_on_candle_after_cross() -> None:
    closes = [Decimal(value) for value in (3, 2, 1, 2, 3, 4)]
    candles = [
        Candle(
            start_time_s=index * 60,
            start_time=f"2026-01-01 00:0{index}:00",
            end_time=f"2026-01-01 00:0{index + 1}:00",
            open=close,
            high=close + Decimal("0.1"),
            low=close - Decimal("0.1"),
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    result = run_ema_backtest(
        candles,
        starting_equity=Decimal("1000"),
        fast_period=2,
        slow_period=3,
        atr_period=2,
        atr_stop_multiple=Decimal("2"),
        risk_per_trade_pct=Decimal("0.5"),
        max_leverage=Decimal("2"),
        daily_loss_limit_pct=Decimal("2"),
        max_daily_entries=10,
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        quantity_step=Decimal("0.00001"),
    )

    assert result.entry_count == 1
    assert result.closed_trades == 1
    assert result.trades[0].entry_time == candles[5].start_time


def test_backtest_applies_positive_funding_cost_to_long() -> None:
    closes = [Decimal(value) for value in (3, 2, 1, 2, 3, 4, 5)]
    candles = [
        Candle(
            start_time_s=index * 60,
            start_time=f"2026-01-01 00:0{index}:00",
            end_time=f"2026-01-01 00:0{index + 1}:00",
            open=close,
            high=close + Decimal("0.1"),
            low=close - Decimal("0.1"),
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]
    common = {
        "starting_equity": Decimal("1000"),
        "fast_period": 2,
        "slow_period": 3,
        "atr_period": 2,
        "atr_stop_multiple": Decimal("2"),
        "risk_per_trade_pct": Decimal("0.5"),
        "max_leverage": Decimal("2"),
        "daily_loss_limit_pct": Decimal("2"),
        "max_daily_entries": 10,
        "taker_fee_bps": Decimal("5"),
        "slippage_bps": Decimal("2"),
        "quantity_step": Decimal("0.00001"),
    }

    without_funding = run_ema_backtest(candles, **common)
    with_funding = run_ema_backtest(
        candles,
        funding_rates={candles[6].start_time_s: Decimal("0.001")},
        **common,
    )

    assert with_funding.funding_pnl < Decimal("0")
    assert with_funding.ending_equity < without_funding.ending_equity


def test_ema_vwma_backtest_enters_next_candle_and_takes_profit() -> None:
    closes = [Decimal(value) for value in (1, 1, 1, 1, 1, 3, 3)]
    volumes = [Decimal(value) for value in (1, 1, 1, 1, 100, 100, 1)]
    candles = [
        Candle(
            start_time_s=index * 300,
            start_time=f"2026-01-01 00:{index * 5:02d}:00",
            end_time=f"2026-01-01 00:{index * 5 + 5:02d}:00",
            open=close,
            high=Decimal("3.1") if index == 6 else close,
            low=Decimal("3.0") if index == 6 else close,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]

    result = run_ema_vwma_long_backtest(
        candles,
        starting_equity=Decimal("1000"),
        fast_period=2,
        slow_period=4,
        vwma_period=2,
        margin_allocation_pct=Decimal("20"),
        position_notional_multiple=Decimal("3"),
        stop_loss_equity_pct=Decimal("1"),
        take_profit_margin_pct=Decimal("10"),
        daily_loss_limit_pct=Decimal("2"),
        max_daily_entries=10,
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        quantity_step=Decimal("0.00001"),
    )

    assert result.entry_count == 1
    assert result.trades[0].entry_time == candles[6].start_time
    assert result.trades[0].side == "LONG"
    assert result.trades[0].exit_reason == "TAKE_PROFIT"
    assert result.trades[0].quantity * result.trades[0].entry_price <= Decimal("3000")


def test_ema_vwma_backtest_uses_stop_first_when_both_levels_touch() -> None:
    closes = [Decimal(value) for value in (1, 1, 1, 1, 1, 3, 3)]
    volumes = [Decimal(value) for value in (1, 1, 1, 1, 100, 100, 1)]
    candles = [
        Candle(
            start_time_s=index * 300,
            start_time=f"2026-01-01 00:{index * 5:02d}:00",
            end_time=f"2026-01-01 00:{index * 5 + 5:02d}:00",
            open=close,
            high=Decimal("3.1") if index == 6 else close,
            low=Decimal("2.9") if index == 6 else close,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]

    result = run_ema_vwma_long_backtest(
        candles,
        starting_equity=Decimal("1000"),
        fast_period=2,
        slow_period=4,
        vwma_period=2,
        margin_allocation_pct=Decimal("20"),
        position_notional_multiple=Decimal("3"),
        stop_loss_equity_pct=Decimal("1"),
        take_profit_margin_pct=Decimal("10"),
        daily_loss_limit_pct=Decimal("2"),
        max_daily_entries=10,
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        quantity_step=Decimal("0.00001"),
    )

    assert result.trades[0].exit_reason == "STOP_LOSS"
    assert result.stopped_trades == 1


def test_smc_swing_bos_backtest_enters_on_next_candle() -> None:
    closes = [Decimal(value) for value in (1, 4, 3, 4, 6, 6)]
    highs = [Decimal(value) for value in (1, 5, "3.5", "4.5", "6.5", "6.2")]
    lows = [Decimal(value) for value in (0, 1, 2, "2.5", 3, 6)]
    candles = [
        Candle(
            start_time_s=index * 300,
            start_time=f"2026-01-01 00:{index * 5:02d}:00",
            end_time=f"2026-01-01 00:{index * 5 + 5:02d}:00",
            open=close,
            high=highs[index],
            low=lows[index],
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    result = run_smc_swing_bos_long_backtest(
        candles,
        starting_equity=Decimal("1000"),
        fast_period=2,
        slow_period=3,
        swing_length=2,
        margin_allocation_pct=Decimal("20"),
        position_notional_multiple=Decimal("3"),
        stop_loss_equity_pct=Decimal("1"),
        take_profit_margin_pct=Decimal("10"),
        daily_loss_limit_pct=Decimal("2"),
        max_daily_entries=10,
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        quantity_step=Decimal("0.00001"),
    )

    assert result.entry_count == 1
    assert result.trades[0].entry_time == candles[5].start_time
    assert result.trades[0].side == "LONG"
    assert result.trades[0].exit_reason == "TAKE_PROFIT"


def test_bollinger_cci_backtest_enters_on_next_candle() -> None:
    closes = [Decimal(value) for value in (10, 9, 8, "8.5", "8.5")]
    highs = [Decimal(value) for value in ("10.2", "9.2", "8.2", "8.7", "8.8")]
    lows = [Decimal(value) for value in ("9.8", "8.8", "7.8", "7.6", "8.4")]
    candles = [
        Candle(
            start_time_s=index * 14_400,
            start_time=f"2026-01-01 {index * 4:02d}:00:00",
            end_time=f"2026-01-01 {index * 4 + 4:02d}:00:00",
            open=close,
            high=highs[index],
            low=lows[index],
            close=close,
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]

    result = run_bollinger_cci_long_backtest(
        candles,
        starting_equity=Decimal("1000"),
        bollinger_period=3,
        bollinger_stddev_multiple=Decimal("2"),
        cci_period=3,
        margin_allocation_pct=Decimal("20"),
        position_notional_multiple=Decimal("3"),
        stop_loss_equity_pct=Decimal("5"),
        take_profit_margin_pct=Decimal("30"),
        daily_loss_limit_pct=Decimal("2"),
        max_daily_entries=10,
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        quantity_step=Decimal("0.00001"),
    )

    assert result.entry_count == 1
    assert result.trades[0].entry_time == candles[4].start_time
    assert result.trades[0].side == "LONG"
    assert result.trades[0].exit_reason == "TAKE_PROFIT"

    split_result = run_bollinger_cci_long_backtest(
        candles,
        starting_equity=Decimal("1000"),
        bollinger_period=3,
        bollinger_stddev_multiple=Decimal("2"),
        cci_period=3,
        margin_allocation_pct=Decimal("20"),
        position_notional_multiple=Decimal("3"),
        stop_loss_equity_pct=Decimal("5"),
        take_profit_margin_pct=Decimal("30"),
        daily_loss_limit_pct=Decimal("2"),
        max_daily_entries=10,
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        quantity_step=Decimal("0.00001"),
        trade_start_time_s=candles[4].start_time_s,
    )
    assert split_result.candle_count == 1
    assert split_result.entry_count == 1
