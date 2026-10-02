from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backpack_bot.risk import calculate_position_size
from backpack_bot.strategy import (
    Candle,
    analyze_ema_trend,
    analyze_ema_vwma_long,
    analyze_smc_swing_bos_long,
    analyze_bollinger_cci_long,
)


ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")


@dataclass(frozen=True)
class BacktestTrade:
    entry_time: str
    exit_time: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    exit_reason: str


@dataclass(frozen=True)
class BacktestResult:
    candle_count: int
    entry_count: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    stopped_trades: int
    daily_loss_blocks: int
    daily_entry_blocks: int
    starting_equity: Decimal
    ending_equity: Decimal
    net_profit: Decimal
    return_pct: Decimal
    max_drawdown_pct: Decimal
    fees_paid: Decimal
    funding_pnl: Decimal
    trades: tuple[BacktestTrade, ...]

    def summary(self) -> dict[str, object]:
        win_rate = (
            Decimal(self.winning_trades) / Decimal(self.closed_trades) * ONE_HUNDRED
            if self.closed_trades
            else ZERO
        )
        return {
            "candleCount": self.candle_count,
            "entryCount": self.entry_count,
            "closedTrades": self.closed_trades,
            "winningTrades": self.winning_trades,
            "losingTrades": self.losing_trades,
            "winRatePct": win_rate,
            "stoppedTrades": self.stopped_trades,
            "dailyLossBlocks": self.daily_loss_blocks,
            "dailyEntryBlocks": self.daily_entry_blocks,
            "startingEquity": self.starting_equity,
            "endingEquity": self.ending_equity,
            "netProfit": self.net_profit,
            "returnPct": self.return_pct,
            "maxDrawdownPct": self.max_drawdown_pct,
            "feesPaid": self.fees_paid,
            "fundingPnl": self.funding_pnl,
        }


def _adverse_fill(price: Decimal, order_side: str, slippage_rate: Decimal) -> Decimal:
    if order_side == "Bid":
        return price * (Decimal("1") + slippage_rate)
    return price * (Decimal("1") - slippage_rate)


def run_ema_backtest(
    candles: list[Candle],
    *,
    starting_equity: Decimal,
    fast_period: int,
    slow_period: int,
    atr_period: int,
    atr_stop_multiple: Decimal,
    risk_per_trade_pct: Decimal,
    max_leverage: Decimal,
    daily_loss_limit_pct: Decimal,
    max_daily_entries: int,
    taker_fee_bps: Decimal,
    slippage_bps: Decimal,
    quantity_step: Decimal,
    funding_rates: dict[int, Decimal] | None = None,
) -> BacktestResult:
    if len(candles) < slow_period + 2:
        raise ValueError("백테스트에 필요한 캔들이 부족합니다.")
    points = analyze_ema_trend(
        candles,
        fast_period=fast_period,
        slow_period=slow_period,
        atr_period=atr_period,
    )
    fee_rate = taker_fee_bps / TEN_THOUSAND
    slippage_rate = slippage_bps / TEN_THOUSAND
    cash = starting_equity
    fees_paid = ZERO
    funding_pnl = ZERO
    position_side = 0
    quantity = ZERO
    entry_price = ZERO
    entry_fee = ZERO
    entry_time = ""
    stop_price = ZERO
    pending_signal: tuple[str, Decimal] | None = None
    trades: list[BacktestTrade] = []
    entry_count = 0
    stopped_trades = 0
    daily_loss_blocks = 0
    daily_entry_blocks = 0
    current_day = ""
    daily_start_equity = starting_equity
    daily_entries = 0
    daily_halted = False
    peak_equity = starting_equity
    max_drawdown_pct = ZERO

    def equity_at(price: Decimal) -> Decimal:
        return cash + Decimal(position_side) * quantity * (price - entry_price)

    def update_drawdown(equity: Decimal) -> None:
        nonlocal peak_equity, max_drawdown_pct
        peak_equity = max(peak_equity, equity)
        if peak_equity > ZERO:
            drawdown = (peak_equity - equity) / peak_equity * ONE_HUNDRED
            max_drawdown_pct = max(max_drawdown_pct, drawdown)

    def update_daily_halt(equity: Decimal) -> None:
        nonlocal daily_halted
        daily_loss = max(ZERO, daily_start_equity - equity)
        daily_limit = daily_start_equity * daily_loss_limit_pct / ONE_HUNDRED
        daily_halted = daily_halted or daily_loss >= daily_limit

    def close_position(price: Decimal, exit_time: str, reason: str) -> None:
        nonlocal cash, fees_paid, position_side, quantity, entry_price
        nonlocal entry_fee, entry_time, stop_price, stopped_trades
        order_side = "Ask" if position_side > 0 else "Bid"
        fill_price = _adverse_fill(price, order_side, slippage_rate)
        gross_pnl = Decimal(position_side) * quantity * (fill_price - entry_price)
        exit_fee = quantity * fill_price * fee_rate
        cash += gross_pnl - exit_fee
        fees_paid += exit_fee
        trade_fees = entry_fee + exit_fee
        trades.append(
            BacktestTrade(
                entry_time=entry_time,
                exit_time=exit_time,
                side="LONG" if position_side > 0 else "SHORT",
                quantity=quantity,
                entry_price=entry_price,
                exit_price=fill_price,
                gross_pnl=gross_pnl,
                fees=trade_fees,
                net_pnl=gross_pnl - trade_fees,
                exit_reason=reason,
            )
        )
        if reason == "ATR_STOP":
            stopped_trades += 1
        position_side = 0
        quantity = ZERO
        entry_price = ZERO
        entry_fee = ZERO
        entry_time = ""
        stop_price = ZERO

    for index, (candle, point) in enumerate(zip(candles, points)):
        day = candle.start_time[:10]
        if day != current_day:
            current_day = day
            daily_start_equity = equity_at(candle.open)
            daily_entries = 0
            daily_halted = False

        funding_rate = (funding_rates or {}).get(candle.start_time_s)
        if funding_rate is not None and position_side != 0:
            funding_payment = (
                -Decimal(position_side) * quantity * candle.open * funding_rate
            )
            cash += funding_payment
            funding_pnl += funding_payment

        open_equity = equity_at(candle.open)
        update_daily_halt(open_equity)
        update_drawdown(open_equity)

        if pending_signal is not None:
            desired_side = 1 if pending_signal[0] == "LONG" else -1
            stop_distance = pending_signal[1] * atr_stop_multiple
            if position_side != 0 and position_side != desired_side:
                close_position(candle.open, candle.start_time, "EMA_CROSS")
                open_equity = cash
                update_daily_halt(open_equity)

            if position_side == 0:
                if daily_halted:
                    daily_loss_blocks += 1
                elif daily_entries >= max_daily_entries:
                    daily_entry_blocks += 1
                else:
                    order_side = "Bid" if desired_side > 0 else "Ask"
                    fill_price = _adverse_fill(candle.open, order_side, slippage_rate)
                    size = calculate_position_size(
                        equity=cash,
                        entry_price=fill_price,
                        stop_distance=stop_distance,
                        risk_pct=risk_per_trade_pct,
                        max_leverage=max_leverage,
                        quantity_step=quantity_step,
                        entry_fee_bps=taker_fee_bps,
                    )
                    if size.quantity > ZERO:
                        position_side = desired_side
                        quantity = size.quantity
                        entry_price = fill_price
                        entry_fee = quantity * entry_price * fee_rate
                        cash -= entry_fee
                        fees_paid += entry_fee
                        entry_time = candle.start_time
                        stop_price = (
                            entry_price - stop_distance
                            if position_side > 0
                            else entry_price + stop_distance
                        )
                        daily_entries += 1
                        entry_count += 1
            pending_signal = None

        if position_side > 0 and candle.low <= stop_price:
            stop_base = min(candle.open, stop_price)
            close_position(stop_base, candle.end_time, "ATR_STOP")
        elif position_side < 0 and candle.high >= stop_price:
            stop_base = max(candle.open, stop_price)
            close_position(stop_base, candle.end_time, "ATR_STOP")

        close_equity = equity_at(candle.close)
        update_daily_halt(close_equity)
        update_drawdown(close_equity)

        if point.signal is not None and point.atr is not None and index + 1 < len(candles):
            pending_signal = (point.signal, point.atr)

    if position_side != 0:
        last = candles[-1]
        close_position(last.close, last.end_time, "END_OF_TEST")
        update_drawdown(cash)

    winning_trades = sum(1 for trade in trades if trade.net_pnl > ZERO)
    losing_trades = len(trades) - winning_trades
    net_profit = cash - starting_equity
    return BacktestResult(
        candle_count=len(candles),
        entry_count=entry_count,
        closed_trades=len(trades),
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        stopped_trades=stopped_trades,
        daily_loss_blocks=daily_loss_blocks,
        daily_entry_blocks=daily_entry_blocks,
        starting_equity=starting_equity,
        ending_equity=cash,
        net_profit=net_profit,
        return_pct=net_profit / starting_equity * ONE_HUNDRED,
        max_drawdown_pct=max_drawdown_pct,
        fees_paid=fees_paid,
        funding_pnl=funding_pnl,
        trades=tuple(trades),
    )


def _run_fixed_long_signal_backtest(
    candles: list[Candle],
    *,
    entry_signals: list[bool],
    starting_equity: Decimal,
    margin_allocation_pct: Decimal,
    position_notional_multiple: Decimal,
    stop_loss_equity_pct: Decimal,
    take_profit_margin_pct: Decimal,
    daily_loss_limit_pct: Decimal,
    max_daily_entries: int,
    taker_fee_bps: Decimal,
    slippage_bps: Decimal,
    quantity_step: Decimal,
    funding_rates: dict[int, Decimal] | None = None,
    trade_start_time_s: int | None = None,
) -> BacktestResult:
    """Backtest completed-candle long signals with fixed equity-based exits."""
    if len(candles) < 2:
        raise ValueError("There are not enough candles for this backtest.")
    if len(entry_signals) != len(candles):
        raise ValueError("Entry signals must match the candle count.")
    if quantity_step <= ZERO:
        raise ValueError("Quantity step must be greater than zero.")
    fee_rate = taker_fee_bps / TEN_THOUSAND
    slippage_rate = slippage_bps / TEN_THOUSAND
    cash = starting_equity
    fees_paid = ZERO
    funding_pnl = ZERO
    quantity = ZERO
    entry_price = ZERO
    entry_fee = ZERO
    entry_time = ""
    stop_price = ZERO
    take_profit_price = ZERO
    pending_entry = False
    trades: list[BacktestTrade] = []
    entry_count = 0
    stopped_trades = 0
    daily_loss_blocks = 0
    daily_entry_blocks = 0
    current_day = ""
    daily_start_equity = starting_equity
    daily_entries = 0
    daily_halted = False
    peak_equity = starting_equity
    max_drawdown_pct = ZERO

    def equity_at(price: Decimal) -> Decimal:
        return cash + quantity * (price - entry_price)

    def update_drawdown(equity: Decimal) -> None:
        nonlocal peak_equity, max_drawdown_pct
        peak_equity = max(peak_equity, equity)
        if peak_equity > ZERO:
            drawdown = (peak_equity - equity) / peak_equity * ONE_HUNDRED
            max_drawdown_pct = max(max_drawdown_pct, drawdown)

    def update_daily_halt(equity: Decimal) -> None:
        nonlocal daily_halted
        daily_loss = max(ZERO, daily_start_equity - equity)
        daily_limit = daily_start_equity * daily_loss_limit_pct / ONE_HUNDRED
        daily_halted = daily_halted or daily_loss >= daily_limit

    def close_position(price: Decimal, exit_time: str, reason: str) -> None:
        nonlocal cash, fees_paid, quantity, entry_price, entry_fee, entry_time
        nonlocal stop_price, take_profit_price, stopped_trades
        fill_price = _adverse_fill(price, "Ask", slippage_rate)
        gross_pnl = quantity * (fill_price - entry_price)
        exit_fee = quantity * fill_price * fee_rate
        cash += gross_pnl - exit_fee
        fees_paid += exit_fee
        trade_fees = entry_fee + exit_fee
        trades.append(
            BacktestTrade(
                entry_time=entry_time,
                exit_time=exit_time,
                side="LONG",
                quantity=quantity,
                entry_price=entry_price,
                exit_price=fill_price,
                gross_pnl=gross_pnl,
                fees=trade_fees,
                net_pnl=gross_pnl - trade_fees,
                exit_reason=reason,
            )
        )
        if reason == "STOP_LOSS":
            stopped_trades += 1
        quantity = ZERO
        entry_price = ZERO
        entry_fee = ZERO
        entry_time = ""
        stop_price = ZERO
        take_profit_price = ZERO

    for index, (candle, entry_signal) in enumerate(zip(candles, entry_signals)):
        day = candle.start_time[:10]
        if day != current_day:
            current_day = day
            daily_start_equity = equity_at(candle.open)
            daily_entries = 0
            daily_halted = False

        funding_rate = (funding_rates or {}).get(candle.start_time_s)
        if funding_rate is not None and quantity > ZERO:
            funding_payment = -(quantity * candle.open * funding_rate)
            cash += funding_payment
            funding_pnl += funding_payment

        open_equity = equity_at(candle.open)
        update_daily_halt(open_equity)
        update_drawdown(open_equity)

        trading_period_started = (
            trade_start_time_s is None or candle.start_time_s >= trade_start_time_s
        )
        if pending_entry:
            if quantity == ZERO:
                if not trading_period_started:
                    pass
                elif daily_halted:
                    daily_loss_blocks += 1
                elif daily_entries >= max_daily_entries:
                    daily_entry_blocks += 1
                else:
                    equity_before_entry = cash
                    fill_price = _adverse_fill(candle.open, "Bid", slippage_rate)
                    target_notional = (
                        equity_before_entry
                        * position_notional_multiple
                        / (Decimal("1") + fee_rate * position_notional_multiple)
                    )
                    raw_quantity = target_notional / fill_price
                    sized_quantity = (raw_quantity // quantity_step) * quantity_step
                    if sized_quantity > ZERO:
                        margin_reference = (
                            equity_before_entry * margin_allocation_pct / ONE_HUNDRED
                        )
                        stop_amount = (
                            equity_before_entry * stop_loss_equity_pct / ONE_HUNDRED
                        )
                        take_profit_amount = (
                            margin_reference * take_profit_margin_pct / ONE_HUNDRED
                        )
                        quantity = sized_quantity
                        entry_price = fill_price
                        entry_fee = quantity * entry_price * fee_rate
                        cash -= entry_fee
                        fees_paid += entry_fee
                        entry_time = candle.start_time
                        stop_price = entry_price - stop_amount / quantity
                        take_profit_price = entry_price + take_profit_amount / quantity
                        daily_entries += 1
                        entry_count += 1
            pending_entry = False

        if quantity > ZERO:
            stop_touched = candle.low <= stop_price
            take_profit_touched = candle.high >= take_profit_price
            if stop_touched:
                close_position(
                    min(candle.open, stop_price), candle.end_time, "STOP_LOSS"
                )
            elif take_profit_touched:
                close_position(
                    max(candle.open, take_profit_price),
                    candle.end_time,
                    "TAKE_PROFIT",
                )

        close_equity = equity_at(candle.close)
        update_daily_halt(close_equity)
        update_drawdown(close_equity)

        if entry_signal and index + 1 < len(candles):
            pending_entry = True

    if quantity > ZERO:
        last = candles[-1]
        close_position(last.close, last.end_time, "END_OF_TEST")
        update_drawdown(cash)

    winning_trades = sum(1 for trade in trades if trade.net_pnl > ZERO)
    losing_trades = len(trades) - winning_trades
    net_profit = cash - starting_equity
    evaluated_candle_count = sum(
        1
        for candle in candles
        if trade_start_time_s is None or candle.start_time_s >= trade_start_time_s
    )
    return BacktestResult(
        candle_count=evaluated_candle_count,
        entry_count=entry_count,
        closed_trades=len(trades),
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        stopped_trades=stopped_trades,
        daily_loss_blocks=daily_loss_blocks,
        daily_entry_blocks=daily_entry_blocks,
        starting_equity=starting_equity,
        ending_equity=cash,
        net_profit=net_profit,
        return_pct=net_profit / starting_equity * ONE_HUNDRED,
        max_drawdown_pct=max_drawdown_pct,
        fees_paid=fees_paid,
        funding_pnl=funding_pnl,
        trades=tuple(trades),
    )


def run_ema_vwma_long_backtest(
    candles: list[Candle],
    *,
    starting_equity: Decimal,
    fast_period: int,
    slow_period: int,
    vwma_period: int,
    margin_allocation_pct: Decimal,
    position_notional_multiple: Decimal,
    stop_loss_equity_pct: Decimal,
    take_profit_margin_pct: Decimal,
    daily_loss_limit_pct: Decimal,
    max_daily_entries: int,
    taker_fee_bps: Decimal,
    slippage_bps: Decimal,
    quantity_step: Decimal,
    funding_rates: dict[int, Decimal] | None = None,
    trade_start_time_s: int | None = None,
) -> BacktestResult:
    if len(candles) < max(slow_period, vwma_period) + 2:
        raise ValueError("There are not enough candles for this backtest.")
    points = analyze_ema_vwma_long(
        candles,
        fast_period=fast_period,
        slow_period=slow_period,
        vwma_period=vwma_period,
    )
    return _run_fixed_long_signal_backtest(
        candles,
        entry_signals=[point.signal == "LONG" for point in points],
        starting_equity=starting_equity,
        margin_allocation_pct=margin_allocation_pct,
        position_notional_multiple=position_notional_multiple,
        stop_loss_equity_pct=stop_loss_equity_pct,
        take_profit_margin_pct=take_profit_margin_pct,
        daily_loss_limit_pct=daily_loss_limit_pct,
        max_daily_entries=max_daily_entries,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
        quantity_step=quantity_step,
        funding_rates=funding_rates,
        trade_start_time_s=trade_start_time_s,
    )


def run_smc_swing_bos_long_backtest(
    candles: list[Candle],
    *,
    starting_equity: Decimal,
    fast_period: int,
    slow_period: int,
    swing_length: int,
    require_ema_filter: bool = True,
    margin_allocation_pct: Decimal,
    position_notional_multiple: Decimal,
    stop_loss_equity_pct: Decimal,
    take_profit_margin_pct: Decimal,
    daily_loss_limit_pct: Decimal,
    max_daily_entries: int,
    taker_fee_bps: Decimal,
    slippage_bps: Decimal,
    quantity_step: Decimal,
    funding_rates: dict[int, Decimal] | None = None,
    trade_start_time_s: int | None = None,
) -> BacktestResult:
    required_history = (
        max(slow_period, swing_length) if require_ema_filter else swing_length
    )
    if len(candles) < required_history + 2:
        raise ValueError("There are not enough candles for this backtest.")
    points = analyze_smc_swing_bos_long(
        candles,
        fast_period=fast_period,
        slow_period=slow_period,
        swing_length=swing_length,
        require_ema_filter=require_ema_filter,
    )
    return _run_fixed_long_signal_backtest(
        candles,
        entry_signals=[point.signal == "LONG" for point in points],
        starting_equity=starting_equity,
        margin_allocation_pct=margin_allocation_pct,
        position_notional_multiple=position_notional_multiple,
        stop_loss_equity_pct=stop_loss_equity_pct,
        take_profit_margin_pct=take_profit_margin_pct,
        daily_loss_limit_pct=daily_loss_limit_pct,
        max_daily_entries=max_daily_entries,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
        quantity_step=quantity_step,
        funding_rates=funding_rates,
        trade_start_time_s=trade_start_time_s,
    )


def run_bollinger_cci_long_backtest(
    candles: list[Candle],
    *,
    starting_equity: Decimal,
    bollinger_period: int,
    bollinger_stddev_multiple: Decimal,
    cci_period: int,
    margin_allocation_pct: Decimal,
    position_notional_multiple: Decimal,
    stop_loss_equity_pct: Decimal,
    take_profit_margin_pct: Decimal,
    daily_loss_limit_pct: Decimal,
    max_daily_entries: int,
    taker_fee_bps: Decimal,
    slippage_bps: Decimal,
    quantity_step: Decimal,
    funding_rates: dict[int, Decimal] | None = None,
    trade_start_time_s: int | None = None,
) -> BacktestResult:
    required_history = max(bollinger_period, cci_period)
    if len(candles) < required_history + 2:
        raise ValueError("There are not enough candles for this backtest.")
    points = analyze_bollinger_cci_long(
        candles,
        bollinger_period=bollinger_period,
        bollinger_stddev_multiple=bollinger_stddev_multiple,
        cci_period=cci_period,
    )
    return _run_fixed_long_signal_backtest(
        candles,
        entry_signals=[point.signal == "LONG" for point in points],
        starting_equity=starting_equity,
        margin_allocation_pct=margin_allocation_pct,
        position_notional_multiple=position_notional_multiple,
        stop_loss_equity_pct=stop_loss_equity_pct,
        take_profit_margin_pct=take_profit_margin_pct,
        daily_loss_limit_pct=daily_loss_limit_pct,
        max_daily_entries=max_daily_entries,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
        quantity_step=quantity_step,
        funding_rates=funding_rates,
        trade_start_time_s=trade_start_time_s,
    )
