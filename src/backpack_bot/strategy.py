from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Candle:
    start_time_s: int
    start_time: str
    end_time: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Candle:
        return cls(
            start_time_s=int(row["start_time_s"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]),
        )


@dataclass(frozen=True)
class EmaTrendPoint:
    candle: Candle
    fast_ema: Decimal | None
    slow_ema: Decimal | None
    atr: Decimal | None
    regime: str
    signal: str | None


@dataclass(frozen=True)
class EmaVwmaLongPoint:
    candle: Candle
    fast_ema: Decimal | None
    slow_ema: Decimal | None
    vwma: Decimal | None
    trend_aligned: bool
    crossed_above: bool
    regime: str
    signal: str | None


@dataclass(frozen=True)
class SmcSwingBosLongPoint:
    candle: Candle
    fast_ema: Decimal | None
    slow_ema: Decimal | None
    swing_high: Decimal | None
    swing_low: Decimal | None
    structure_trend: int
    bullish_swing_bos: bool
    ema_trend_aligned: bool
    ema_both_rising: bool
    regime: str
    signal: str | None


@dataclass(frozen=True)
class BollingerCciLongPoint:
    candle: Candle
    basis: Decimal | None
    upper_band: Decimal | None
    lower_band: Decimal | None
    cci: Decimal | None
    lower_band_touched: bool
    cci_rising_to_zero: bool
    regime: str
    signal: str | None


def exponential_moving_average(
    values: list[Decimal], period: int
) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("EMA 주기는 0보다 커야 합니다.")
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result

    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = seed
    multiplier = Decimal("2") / Decimal(period + 1)
    previous = seed
    for index in range(period, len(values)):
        previous = previous + multiplier * (values[index] - previous)
        result[index] = previous
    return result


def average_true_range(
    candles: list[Candle], period: int
) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("ATR 주기는 0보다 커야 합니다.")
    true_ranges: list[Decimal] = []
    for index, candle in enumerate(candles):
        if index == 0:
            true_range = candle.high - candle.low
        else:
            previous_close = candles[index - 1].close
            true_range = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        true_ranges.append(true_range)

    result: list[Decimal | None] = [None] * len(candles)
    if len(true_ranges) < period:
        return result
    previous_atr = sum(true_ranges[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = previous_atr
    for index in range(period, len(true_ranges)):
        previous_atr = (
            previous_atr * Decimal(period - 1) + true_ranges[index]
        ) / Decimal(period)
        result[index] = previous_atr
    return result


def volume_weighted_moving_average(
    candles: list[Candle], period: int
) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("VWMA period must be greater than zero.")
    result: list[Decimal | None] = [None] * len(candles)
    weighted_sum = Decimal("0")
    volume_sum = Decimal("0")
    for index, candle in enumerate(candles):
        weighted_sum += candle.close * candle.volume
        volume_sum += candle.volume
        if index >= period:
            expired = candles[index - period]
            weighted_sum -= expired.close * expired.volume
            volume_sum -= expired.volume
        if index >= period - 1 and volume_sum > 0:
            result[index] = weighted_sum / volume_sum
    return result


def bollinger_bands(
    values: list[Decimal], period: int, standard_deviation_multiple: Decimal
) -> tuple[
    list[Decimal | None], list[Decimal | None], list[Decimal | None]
]:
    if period <= 0:
        raise ValueError("Bollinger period must be greater than zero.")
    if standard_deviation_multiple <= 0:
        raise ValueError("Bollinger standard deviation multiple must be positive.")
    basis_values: list[Decimal | None] = [None] * len(values)
    upper_values: list[Decimal | None] = [None] * len(values)
    lower_values: list[Decimal | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        basis = sum(window, Decimal("0")) / Decimal(period)
        variance = sum(
            ((value - basis) ** 2 for value in window), Decimal("0")
        ) / Decimal(period)
        deviation = variance.sqrt() * standard_deviation_multiple
        basis_values[index] = basis
        upper_values[index] = basis + deviation
        lower_values[index] = basis - deviation
    return basis_values, upper_values, lower_values


def commodity_channel_index(
    candles: list[Candle], period: int
) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("CCI period must be greater than zero.")
    typical_prices = [
        (candle.high + candle.low + candle.close) / Decimal("3")
        for candle in candles
    ]
    result: list[Decimal | None] = [None] * len(candles)
    for index in range(period - 1, len(candles)):
        window = typical_prices[index - period + 1 : index + 1]
        average = sum(window, Decimal("0")) / Decimal(period)
        mean_deviation = sum(
            (abs(value - average) for value in window), Decimal("0")
        ) / Decimal(period)
        if mean_deviation > 0:
            result[index] = (
                typical_prices[index] - average
            ) / (Decimal("0.015") * mean_deviation)
    return result


def analyze_ema_trend(
    candles: list[Candle],
    *,
    fast_period: int,
    slow_period: int,
    atr_period: int,
) -> list[EmaTrendPoint]:
    if fast_period >= slow_period:
        raise ValueError("빠른 EMA 주기는 느린 EMA 주기보다 작아야 합니다.")
    closes = [candle.close for candle in candles]
    fast_values = exponential_moving_average(closes, fast_period)
    slow_values = exponential_moving_average(closes, slow_period)
    atr_values = average_true_range(candles, atr_period)
    points: list[EmaTrendPoint] = []

    for index, candle in enumerate(candles):
        fast = fast_values[index]
        slow = slow_values[index]
        previous_fast = fast_values[index - 1] if index > 0 else None
        previous_slow = slow_values[index - 1] if index > 0 else None
        signal = None
        regime = "WARMUP"
        if fast is not None and slow is not None:
            regime = "LONG" if fast > slow else "SHORT"
            if previous_fast is not None and previous_slow is not None:
                if previous_fast <= previous_slow and fast > slow:
                    signal = "LONG"
                elif previous_fast >= previous_slow and fast < slow:
                    signal = "SHORT"
        points.append(
            EmaTrendPoint(
                candle=candle,
                fast_ema=fast,
                slow_ema=slow,
                atr=atr_values[index],
                regime=regime,
                signal=signal,
            )
        )
    return points


def analyze_ema_vwma_long(
    candles: list[Candle],
    *,
    fast_period: int,
    slow_period: int,
    vwma_period: int,
) -> list[EmaVwmaLongPoint]:
    """Generate a long signal only on a completed VWMA/slow-EMA crossover."""
    if fast_period >= slow_period:
        raise ValueError("The fast EMA period must be shorter than the slow EMA period.")
    closes = [candle.close for candle in candles]
    fast_values = exponential_moving_average(closes, fast_period)
    slow_values = exponential_moving_average(closes, slow_period)
    vwma_values = volume_weighted_moving_average(candles, vwma_period)
    points: list[EmaVwmaLongPoint] = []

    for index, candle in enumerate(candles):
        fast = fast_values[index]
        slow = slow_values[index]
        vwma = vwma_values[index]
        previous_slow = slow_values[index - 1] if index > 0 else None
        previous_vwma = vwma_values[index - 1] if index > 0 else None
        ready = fast is not None and slow is not None and vwma is not None
        trend_aligned = bool(ready and fast > slow)
        crossed_above = bool(
            ready
            and previous_slow is not None
            and previous_vwma is not None
            and previous_vwma <= previous_slow
            and vwma > slow
        )
        signal = "LONG" if trend_aligned and crossed_above else None
        if not ready:
            regime = "WARMUP"
        elif trend_aligned:
            regime = "LONG_ALLOWED"
        else:
            regime = "FILTERED"
        points.append(
            EmaVwmaLongPoint(
                candle=candle,
                fast_ema=fast,
                slow_ema=slow,
                vwma=vwma,
                trend_aligned=trend_aligned,
                crossed_above=crossed_above,
                regime=regime,
                signal=signal,
            )
        )
    return points


def analyze_smc_swing_bos_long(
    candles: list[Candle],
    *,
    fast_period: int,
    slow_period: int,
    swing_length: int,
    require_ema_filter: bool = True,
) -> list[SmcSwingBosLongPoint]:
    """Reproduce LuxAlgo-style swing BOS state and emit filtered long signals.

    A swing uses the published default-style delayed confirmation: the candidate
    candle ``swing_length`` bars ago must exceed all subsequent highs or lows.
    Both bullish and bearish structure breaks are tracked so a bullish CHoCH is
    not incorrectly treated as a bullish BOS.
    """
    if fast_period >= slow_period:
        raise ValueError("The fast EMA period must be shorter than the slow EMA period.")
    if swing_length <= 0:
        raise ValueError("Swing length must be greater than zero.")

    closes = [candle.close for candle in candles]
    fast_values = exponential_moving_average(closes, fast_period)
    slow_values = exponential_moving_average(closes, slow_period)
    points: list[SmcSwingBosLongPoint] = []

    swing_state = 0
    structure_trend = 0
    swing_high: Decimal | None = None
    swing_low: Decimal | None = None
    high_available = True
    low_available = True
    previous_close: Decimal | None = None
    previous_swing_high: Decimal | None = None
    previous_swing_low: Decimal | None = None

    for index, candle in enumerate(candles):
        prior_swing_state = swing_state
        if index >= swing_length:
            candidate = candles[index - swing_length]
            recent = candles[index - swing_length + 1 : index + 1]
            highest_recent = max(item.high for item in recent)
            lowest_recent = min(item.low for item in recent)
            if candidate.high > highest_recent:
                swing_state = 0
            elif candidate.low < lowest_recent:
                swing_state = 1

            if swing_state == 0 and prior_swing_state != 0:
                swing_high = candidate.high
                high_available = True
            elif swing_state == 1 and prior_swing_state != 1:
                swing_low = candidate.low
                low_available = True

        crossed_above = bool(
            swing_high is not None
            and previous_close is not None
            and previous_swing_high is not None
            and candle.close > swing_high
            and previous_close <= previous_swing_high
            and high_available
        )
        crossed_below = bool(
            swing_low is not None
            and previous_close is not None
            and previous_swing_low is not None
            and candle.close < swing_low
            and previous_close >= previous_swing_low
            and low_available
        )

        bullish_swing_bos = False
        if crossed_above:
            bullish_swing_bos = structure_trend >= 0
            high_available = False
            structure_trend = 1
        if crossed_below:
            low_available = False
            structure_trend = -1

        fast = fast_values[index]
        slow = slow_values[index]
        previous_fast = fast_values[index - 1] if index > 0 else None
        previous_slow = slow_values[index - 1] if index > 0 else None
        ready = fast is not None and slow is not None
        ema_trend_aligned = bool(ready and fast > slow)
        ema_both_rising = bool(
            ready
            and previous_fast is not None
            and previous_slow is not None
            and fast > previous_fast
            and slow > previous_slow
        )
        ema_filter_passed = bool(
            not require_ema_filter or (ema_trend_aligned and ema_both_rising)
        )
        signal = "LONG" if bullish_swing_bos and ema_filter_passed else None
        if require_ema_filter and not ready:
            regime = "WARMUP"
        elif ema_filter_passed:
            regime = "LONG_ALLOWED"
        else:
            regime = "FILTERED"
        points.append(
            SmcSwingBosLongPoint(
                candle=candle,
                fast_ema=fast,
                slow_ema=slow,
                swing_high=swing_high,
                swing_low=swing_low,
                structure_trend=structure_trend,
                bullish_swing_bos=bullish_swing_bos,
                ema_trend_aligned=ema_trend_aligned,
                ema_both_rising=ema_both_rising,
                regime=regime,
                signal=signal,
            )
        )
        previous_close = candle.close
        previous_swing_high = swing_high
        previous_swing_low = swing_low

    return points


def analyze_bollinger_cci_long(
    candles: list[Candle],
    *,
    bollinger_period: int,
    bollinger_stddev_multiple: Decimal,
    cci_period: int,
) -> list[BollingerCciLongPoint]:
    closes = [candle.close for candle in candles]
    basis_values, upper_values, lower_values = bollinger_bands(
        closes, bollinger_period, bollinger_stddev_multiple
    )
    cci_values = commodity_channel_index(candles, cci_period)
    points: list[BollingerCciLongPoint] = []
    for index, candle in enumerate(candles):
        basis = basis_values[index]
        upper = upper_values[index]
        lower = lower_values[index]
        cci = cci_values[index]
        previous_cci = cci_values[index - 1] if index > 0 else None
        lower_band_touched = bool(lower is not None and candle.low <= lower)
        cci_rising_to_zero = bool(
            cci is not None
            and previous_cci is not None
            and Decimal("-100") <= cci < Decimal("0")
            and cci > previous_cci
        )
        ready = lower is not None and cci is not None and previous_cci is not None
        signal = "LONG" if lower_band_touched and cci_rising_to_zero else None
        if not ready:
            regime = "WARMUP"
        elif cci_rising_to_zero:
            regime = "LONG_ALLOWED"
        else:
            regime = "FILTERED"
        points.append(
            BollingerCciLongPoint(
                candle=candle,
                basis=basis,
                upper_band=upper,
                lower_band=lower,
                cci=cci,
                lower_band_touched=lower_band_touched,
                cci_rising_to_zero=cci_rising_to_zero,
                regime=regime,
                signal=signal,
            )
        )
    return points
