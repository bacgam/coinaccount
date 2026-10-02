from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal
from typing import Any

from backpack_bot.client import BackpackAPIError, BackpackClient
from backpack_bot.config import ConfigError, load_bot_settings, load_settings
from backpack_bot.stream import monitor_market
from backpack_bot.storage import MarketDataStore
from backpack_bot.history import INTERVAL_SECONDS, download_history
from backpack_bot.paper import PaperBroker
from backpack_bot.strategy import (
    Candle,
    analyze_ema_trend,
    analyze_ema_vwma_long,
    analyze_smc_swing_bos_long,
    analyze_bollinger_cci_long,
)
from backpack_bot.backtest import (
    run_ema_backtest,
    run_ema_vwma_long_backtest,
    run_smc_swing_bos_long_backtest,
    run_bollinger_cci_long_backtest,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _build_client(*, require_credentials: bool = False) -> BackpackClient:
    settings = load_settings(require_credentials=require_credentials)
    return BackpackClient(
        base_url=settings.api_base_url,
        api_key=settings.api_key,
        api_secret=settings.api_secret,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backpack-bot",
        description="Backpack 거래소 연결 확인 도구 (현재 주문 기능 없음)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="API 서버 상태 확인")

    markets_parser = subparsers.add_parser("markets", help="거래 가능 마켓 목록 확인")
    markets_parser.add_argument("--limit", type=int, default=10, help="출력 개수 (기본 10)")

    ticker_parser = subparsers.add_parser("ticker", help="마켓 가격 정보 확인")
    ticker_parser.add_argument("symbol", help="예: SOL_USDC 또는 SOL_USDC_PERP")

    subparsers.add_parser("market", help="선택한 선물 마켓의 주문 단위와 상태 확인")
    subparsers.add_parser("price", help="선택한 선물 마켓의 마크 가격과 펀딩 확인")

    monitor_parser = subparsers.add_parser("monitor", help="선택한 선물의 실시간 데이터 보기")
    monitor_parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="테스트 실행 시간. 생략하면 Ctrl+C까지 계속 실행",
    )

    record_parser = subparsers.add_parser("record", help="실시간 데이터를 SQLite에 저장")
    record_parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="테스트 저장 시간. 생략하면 Ctrl+C까지 계속 실행",
    )

    subparsers.add_parser("db-status", help="저장된 실시간 데이터 개수 확인")
    subparsers.add_parser("paper-init", help="모의 계좌를 안전하게 초기화")
    subparsers.add_parser("paper-status", help="모의 계좌와 평가 손익 확인")
    subparsers.add_parser("strategy-status", help="EMA 전략의 최신 완료 캔들 상태 확인")
    subparsers.add_parser("backtest", help="현재 EMA 설정으로 30일 백테스트")
    subparsers.add_parser("funding-history", help="선물 펀딩 이력을 SQLite에 저장")

    history_parser = subparsers.add_parser("history", help="과거 캔들을 SQLite에 저장")
    history_parser.add_argument(
        "--interval",
        choices=tuple(INTERVAL_SECONDS),
        default=None,
        help="캔들 주기. 생략하면 bot.toml 설정 사용",
    )
    history_parser.add_argument(
        "--days",
        type=float,
        default=None,
        help="현재부터 과거 며칠을 받을지 지정. 생략하면 bot.toml 설정 사용",
    )

    subparsers.add_parser("balance", help="내 계정 잔고 확인 (읽기 전용 키 필요)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            _print_json(_build_client().get_status())
        elif args.command == "markets":
            if args.limit < 1:
                raise ValueError("--limit은 1 이상이어야 합니다.")
            markets = _build_client().get_markets()
            compact = [
                {
                    "symbol": market.get("symbol"),
                    "marketType": market.get("marketType"),
                    "baseSymbol": market.get("baseSymbol"),
                    "quoteSymbol": market.get("quoteSymbol"),
                }
                for market in markets[: args.limit]
            ]
            _print_json(compact)
        elif args.command == "ticker":
            ticker = _build_client().get_ticker(args.symbol)
            if ticker is None:
                print("해당 마켓의 티커 데이터가 없습니다.")
            else:
                _print_json(ticker)
        elif args.command == "market":
            bot = load_bot_settings()
            market = _build_client().get_market(bot.symbol)
            _print_json(
                {
                    "mode": bot.mode,
                    "symbol": market.get("symbol"),
                    "marketType": market.get("marketType"),
                    "orderBookState": market.get("orderBookState"),
                    "priceFilter": market.get("filters", {}).get("price"),
                    "quantityFilter": market.get("filters", {}).get("quantity"),
                    "fundingInterval": market.get("fundingInterval"),
                }
            )
        elif args.command == "price":
            bot = load_bot_settings()
            _print_json(_build_client().get_mark_price(bot.symbol))
        elif args.command == "monitor":
            bot = load_bot_settings()
            print(f"{bot.symbol} 실시간 모니터를 시작합니다. 종료: Ctrl+C")
            asyncio.run(monitor_market(bot.symbol, run_seconds=args.seconds))
        elif args.command == "record":
            bot = load_bot_settings()
            print(f"{bot.symbol} 데이터를 SQLite에 저장합니다. 종료: Ctrl+C")
            print(f"DB: {bot.database_path}")
            with MarketDataStore(bot.database_path) as store:
                asyncio.run(
                    monitor_market(
                        bot.symbol,
                        run_seconds=args.seconds,
                        on_snapshot=store.insert_state,
                    )
                )
                stats = store.statistics(bot.symbol)
            print(f"현재 저장 행 수: {stats['rows']}")
        elif args.command == "db-status":
            bot = load_bot_settings()
            with MarketDataStore(bot.database_path) as store:
                snapshot_stats = store.statistics(bot.symbol)
                kline_stats = store.kline_statistics(bot.symbol, bot.history_interval)
                gap_count = store.kline_gap_count(
                    bot.symbol,
                    bot.history_interval,
                    INTERVAL_SECONDS[bot.history_interval],
                )
            _print_json(
                {
                    "database": str(bot.database_path),
                    "symbol": bot.symbol,
                    "snapshots": snapshot_stats,
                    "klines": {
                        "interval": bot.history_interval,
                        **kline_stats,
                        "internalGaps": gap_count,
                    },
                }
            )
        elif args.command == "history":
            bot = load_bot_settings()
            interval = args.interval or bot.history_interval
            days = args.days if args.days is not None else bot.history_days
            if days <= 0:
                raise ValueError("--days는 0보다 커야 합니다.")
            end_time = int(time.time())
            start_time = end_time - int(days * 24 * 60 * 60)
            print(
                f"{bot.symbol} {interval} 캔들 "
                f"최근 {days:g}일을 다운로드합니다."
            )
            with MarketDataStore(bot.database_path) as store:
                received = download_history(
                    _build_client(),
                    store,
                    symbol=bot.symbol,
                    interval=interval,
                    start_time=start_time,
                    end_time=end_time,
                )
                stats = store.kline_statistics(bot.symbol, interval)
                gap_count = store.kline_gap_count(
                    bot.symbol, interval, INTERVAL_SECONDS[interval]
                )
            print(f"이번에 받은 캔들: {received}개")
            print(f"DB에 저장된 고유 캔들: {stats['rows']}개")
            print(f"범위: {stats['first_start']} ~ {stats['last_start']} UTC")
            print(f"거래소 원본 내부 누락: {gap_count}개")
        elif args.command == "paper-init":
            bot = load_bot_settings()
            account_info = _build_client(require_credentials=True).get_account()
            maker_fee_bps = Decimal(account_info["futuresMakerFee"])
            taker_fee_bps = Decimal(account_info["futuresTakerFee"])
            with MarketDataStore(bot.database_path) as store:
                broker = PaperBroker(store.connection)
                account = broker.initialize(
                    bot.paper_initial_balance,
                    maker_fee_bps=maker_fee_bps,
                    taker_fee_bps=taker_fee_bps,
                    max_leverage=bot.paper_max_leverage,
                    daily_loss_limit_pct=bot.paper_daily_loss_limit_pct,
                    risk_per_trade_pct=bot.paper_risk_per_trade_pct,
                    max_daily_entries=bot.paper_max_daily_entries,
                )
            _print_json(
                {
                    "mode": bot.mode,
                    "initialBalance": str(account.initial_balance),
                    "currency": "USDC",
                    "makerFeeBps": str(account.maker_fee_bps),
                    "takerFeeBps": str(account.taker_fee_bps),
                    "maxLeverage": str(account.max_leverage),
                    "dailyLossLimitPct": str(account.daily_loss_limit_pct),
                    "riskPerTradePct": str(account.risk_per_trade_pct),
                    "maxDailyEntries": account.max_daily_entries,
                    "exchangeOrdersEnabled": False,
                }
            )
        elif args.command == "paper-status":
            bot = load_bot_settings()
            mark_price = Decimal(_build_client().get_mark_price(bot.symbol)["markPrice"])
            with MarketDataStore(bot.database_path) as store:
                status = PaperBroker(store.connection).status(bot.symbol, mark_price)
            _print_json(
                {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in status.items()
                }
            )
        elif args.command == "strategy-status":
            bot = load_bot_settings()
            with MarketDataStore(bot.database_path) as store:
                rows = store.load_klines(
                    bot.symbol,
                    bot.strategy_interval,
                    completed_before_s=int(time.time()),
                )
            candles = [Candle.from_row(row) for row in rows]
            required_candles = max(bot.ema_slow_period, bot.vwma_period)
            if bot.strategy == "smc_swing_bos_long" and not bot.smc_require_ema_filter:
                required_candles = bot.smc_swing_length
            elif bot.strategy == "bollinger_cci_long":
                required_candles = max(bot.bollinger_period, bot.cci_period)
            if len(candles) < required_candles:
                raise ValueError(
                    f"{bot.strategy_interval} 완료 캔들이 부족합니다. "
                    "먼저 과거 데이터를 다운로드하세요."
                )
            if bot.strategy == "bollinger_cci_long":
                latest = analyze_bollinger_cci_long(
                    candles,
                    bollinger_period=bot.bollinger_period,
                    bollinger_stddev_multiple=bot.bollinger_stddev_multiple,
                    cci_period=bot.cci_period,
                )[-1]
                _print_json(
                    {
                        "strategy": bot.strategy,
                        "symbol": bot.symbol,
                        "interval": bot.strategy_interval,
                        "completedCandleStart": latest.candle.start_time,
                        "completedCandleEnd": latest.candle.end_time,
                        "close": str(latest.candle.close),
                        "low": str(latest.candle.low),
                        "bollingerBasis": str(latest.basis),
                        "bollingerUpper": str(latest.upper_band),
                        "bollingerLower": str(latest.lower_band),
                        "cci": str(latest.cci),
                        "lowerBandTouched": latest.lower_band_touched,
                        "cciRisingTowardZero": latest.cci_rising_to_zero,
                        "newSignal": latest.signal,
                        "longOnly": True,
                        "ordersCreated": False,
                    }
                )
            elif bot.strategy == "smc_swing_bos_long":
                latest = analyze_smc_swing_bos_long(
                    candles,
                    fast_period=bot.ema_fast_period,
                    slow_period=bot.ema_slow_period,
                    swing_length=bot.smc_swing_length,
                    require_ema_filter=bot.smc_require_ema_filter,
                )[-1]
                _print_json(
                    {
                        "strategy": bot.strategy,
                        "symbol": bot.symbol,
                        "interval": bot.strategy_interval,
                        "completedCandleStart": latest.candle.start_time,
                        "completedCandleEnd": latest.candle.end_time,
                        "close": str(latest.candle.close),
                        "emaFast": str(latest.fast_ema),
                        "emaSlow": str(latest.slow_ema),
                        "emaTrendAligned": latest.ema_trend_aligned,
                        "emaBothRising": latest.ema_both_rising,
                        "emaFilterRequired": bot.smc_require_ema_filter,
                        "swingLength": bot.smc_swing_length,
                        "latestSwingHigh": str(latest.swing_high),
                        "latestSwingLow": str(latest.swing_low),
                        "structureTrend": latest.structure_trend,
                        "bullishSwingBos": latest.bullish_swing_bos,
                        "regime": latest.regime,
                        "newSignal": latest.signal,
                        "longOnly": True,
                        "ordersCreated": False,
                    }
                )
            elif bot.strategy == "ema_vwma_long":
                latest = analyze_ema_vwma_long(
                    candles,
                    fast_period=bot.ema_fast_period,
                    slow_period=bot.ema_slow_period,
                    vwma_period=bot.vwma_period,
                )[-1]
                _print_json(
                    {
                        "strategy": bot.strategy,
                        "symbol": bot.symbol,
                        "interval": bot.strategy_interval,
                        "completedCandleStart": latest.candle.start_time,
                        "completedCandleEnd": latest.candle.end_time,
                        "close": str(latest.candle.close),
                        "emaFast": str(latest.fast_ema),
                        "emaSlow": str(latest.slow_ema),
                        "vwma": str(latest.vwma),
                        "trendAligned": latest.trend_aligned,
                        "vwmaCrossedAboveEmaSlow": latest.crossed_above,
                        "regime": latest.regime,
                        "newCrossSignal": latest.signal,
                        "longOnly": True,
                        "ordersCreated": False,
                    }
                )
            else:
                latest = analyze_ema_trend(
                    candles,
                    fast_period=bot.ema_fast_period,
                    slow_period=bot.ema_slow_period,
                    atr_period=bot.atr_period,
                )[-1]
                stop_distance = (
                    latest.atr * bot.atr_stop_multiple
                    if latest.atr is not None
                    else None
                )
                _print_json(
                    {
                        "strategy": bot.strategy,
                        "symbol": bot.symbol,
                        "interval": bot.strategy_interval,
                        "completedCandleStart": latest.candle.start_time,
                        "completedCandleEnd": latest.candle.end_time,
                        "close": str(latest.candle.close),
                        "emaFast": str(latest.fast_ema),
                        "emaSlow": str(latest.slow_ema),
                        "atr": str(latest.atr),
                        "atrStopDistance": str(stop_distance),
                        "regime": latest.regime,
                        "newCrossSignal": latest.signal,
                        "ordersCreated": False,
                    }
                )
        elif args.command == "funding-history":
            bot = load_bot_settings()
            rates = _build_client().get_funding_rates(bot.symbol, limit=10_000)
            with MarketDataStore(bot.database_path) as store:
                saved = store.upsert_funding_rates(rates)
            _print_json(
                {
                    "symbol": bot.symbol,
                    "received": len(rates),
                    "savedOrUpdated": saved,
                    "newest": rates[0]["intervalEndTimestamp"] if rates else None,
                    "oldest": rates[-1]["intervalEndTimestamp"] if rates else None,
                }
            )
        elif args.command == "backtest":
            bot = load_bot_settings()
            with MarketDataStore(bot.database_path) as store:
                rows = store.load_klines(
                    bot.symbol,
                    bot.strategy_interval,
                    completed_before_s=int(time.time()),
                )
                if not rows:
                    raise ValueError(
                        f"{bot.strategy_interval} 완료 캔들이 없습니다. "
                        "먼저 history 명령을 실행하세요."
                    )
                paper_account = PaperBroker(store.connection).account()
                funding_rates = store.load_funding_rates(
                    bot.symbol,
                    start_time_s=rows[0]["start_time_s"],
                    end_time_s=rows[-1]["start_time_s"],
                )
            candles = [Candle.from_row(row) for row in rows]
            if not funding_rates:
                raise ValueError(
                    "펀딩 이력이 없습니다. 먼저 funding-history 명령을 실행하세요."
                )
            market = _build_client().get_market(bot.symbol)
            quantity_step = Decimal(market["filters"]["quantity"]["stepSize"])

            def execute(
                sample: list[Candle], *, trade_start_time_s: int | None = None
            ):
                if bot.strategy == "bollinger_cci_long":
                    return run_bollinger_cci_long_backtest(
                        sample,
                        starting_equity=bot.paper_initial_balance,
                        bollinger_period=bot.bollinger_period,
                        bollinger_stddev_multiple=bot.bollinger_stddev_multiple,
                        cci_period=bot.cci_period,
                        margin_allocation_pct=bot.margin_allocation_pct,
                        position_notional_multiple=bot.position_notional_multiple,
                        stop_loss_equity_pct=bot.stop_loss_equity_pct,
                        take_profit_margin_pct=bot.take_profit_margin_pct,
                        daily_loss_limit_pct=bot.paper_daily_loss_limit_pct,
                        max_daily_entries=bot.paper_max_daily_entries,
                        taker_fee_bps=paper_account.taker_fee_bps,
                        slippage_bps=bot.backtest_slippage_bps,
                        quantity_step=quantity_step,
                        funding_rates=funding_rates,
                        trade_start_time_s=trade_start_time_s,
                    )
                if bot.strategy == "smc_swing_bos_long":
                    return run_smc_swing_bos_long_backtest(
                        sample,
                        starting_equity=bot.paper_initial_balance,
                        fast_period=bot.ema_fast_period,
                        slow_period=bot.ema_slow_period,
                        swing_length=bot.smc_swing_length,
                        require_ema_filter=bot.smc_require_ema_filter,
                        margin_allocation_pct=bot.margin_allocation_pct,
                        position_notional_multiple=bot.position_notional_multiple,
                        stop_loss_equity_pct=bot.stop_loss_equity_pct,
                        take_profit_margin_pct=bot.take_profit_margin_pct,
                        daily_loss_limit_pct=bot.paper_daily_loss_limit_pct,
                        max_daily_entries=bot.paper_max_daily_entries,
                        taker_fee_bps=paper_account.taker_fee_bps,
                        slippage_bps=bot.backtest_slippage_bps,
                        quantity_step=quantity_step,
                        funding_rates=funding_rates,
                        trade_start_time_s=trade_start_time_s,
                    )
                if bot.strategy == "ema_vwma_long":
                    return run_ema_vwma_long_backtest(
                        sample,
                        starting_equity=bot.paper_initial_balance,
                        fast_period=bot.ema_fast_period,
                        slow_period=bot.ema_slow_period,
                        vwma_period=bot.vwma_period,
                        margin_allocation_pct=bot.margin_allocation_pct,
                        position_notional_multiple=bot.position_notional_multiple,
                        stop_loss_equity_pct=bot.stop_loss_equity_pct,
                        take_profit_margin_pct=bot.take_profit_margin_pct,
                        daily_loss_limit_pct=bot.paper_daily_loss_limit_pct,
                        max_daily_entries=bot.paper_max_daily_entries,
                        taker_fee_bps=paper_account.taker_fee_bps,
                        slippage_bps=bot.backtest_slippage_bps,
                        quantity_step=quantity_step,
                        funding_rates=funding_rates,
                        trade_start_time_s=trade_start_time_s,
                    )
                return run_ema_backtest(
                    sample,
                    starting_equity=bot.paper_initial_balance,
                    fast_period=bot.ema_fast_period,
                    slow_period=bot.ema_slow_period,
                    atr_period=bot.atr_period,
                    atr_stop_multiple=bot.atr_stop_multiple,
                    risk_per_trade_pct=bot.paper_risk_per_trade_pct,
                    max_leverage=bot.paper_max_leverage,
                    daily_loss_limit_pct=bot.paper_daily_loss_limit_pct,
                    max_daily_entries=bot.paper_max_daily_entries,
                    taker_fee_bps=paper_account.taker_fee_bps,
                    slippage_bps=bot.backtest_slippage_bps,
                    quantity_step=quantity_step,
                    funding_rates=funding_rates,
                    trade_start_time_s=trade_start_time_s,
                )

            def summarize(result, sample: list[Candle]) -> dict[str, object]:
                summary = {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in result.summary().items()
                }
                top_three = sum(
                    sorted(
                        (trade.net_pnl for trade in result.trades), reverse=True
                    )[:3],
                    Decimal("0"),
                )
                summary.update(
                    {
                        "periodStart": sample[0].start_time,
                        "periodEnd": sample[-1].end_time,
                        "topThreeTradesNetPnl": str(top_three),
                    }
                )
                return summary

            split = len(candles) * 2 // 3
            first_sample = candles[:split]
            last_sample = candles[split:]
            warmup_count = max(
                bot.ema_slow_period,
                bot.vwma_period,
                bot.smc_swing_length,
                bot.bollinger_period,
                bot.cci_period,
            ) + 1
            last_with_warmup = candles[max(0, split - warmup_count) :]
            _print_json(
                {
                    "symbol": bot.symbol,
                    "interval": bot.strategy_interval,
                    "assumptions": {
                        "longOnly": bot.strategy in {
                            "ema_vwma_long",
                            "smc_swing_bos_long",
                            "bollinger_cci_long",
                        },
                        "entrySignal": (
                            "BB_LOWER_TOUCH_AND_CCI_RECOVERY"
                            if bot.strategy == "bollinger_cci_long"
                            else (
                                "BULLISH_SWING_BOS"
                                if bot.strategy == "smc_swing_bos_long"
                                else "VWMA_CROSS"
                            )
                        ),
                        "bollingerPeriod": bot.bollinger_period,
                        "bollingerStdDevMultiple": str(
                            bot.bollinger_stddev_multiple
                        ),
                        "cciPeriod": bot.cci_period,
                        "smcSwingLength": bot.smc_swing_length,
                        "emaFilterRequired": bot.smc_require_ema_filter,
                        "marginAllocationPct": str(bot.margin_allocation_pct),
                        "positionNotionalMultiple": str(
                            bot.position_notional_multiple
                        ),
                        "stopLossEquityPct": str(bot.stop_loss_equity_pct),
                        "takeProfitMarginPct": str(bot.take_profit_margin_pct),
                        "dailyLossLimitPct": str(bot.paper_daily_loss_limit_pct),
                        "maxDailyEntries": bot.paper_max_daily_entries,
                        "takerFeeBpsPerFill": str(paper_account.taker_fee_bps),
                        "slippageBpsPerFill": str(bot.backtest_slippage_bps),
                        "fundingIncluded": True,
                    },
                    "fullPeriod": summarize(execute(candles), candles),
                    "firstTwoThirds": summarize(execute(first_sample), first_sample),
                    "lastThird": summarize(
                        execute(
                            last_with_warmup,
                            trade_start_time_s=last_sample[0].start_time_s,
                        ),
                        last_sample,
                    ),
                }
            )
        elif args.command == "balance":
            _print_json(_build_client(require_credentials=True).get_balances())
        return 0
    except (BackpackAPIError, ConfigError, ValueError) as exc:
        print(f"오류: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n사용자가 모니터를 종료했습니다.")
        return 0
