from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR


@dataclass(frozen=True)
class PositionSize:
    quantity: Decimal
    risk_budget: Decimal
    position_notional: Decimal
    limited_by: str


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("수량 단위는 0보다 커야 합니다.")
    steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


def calculate_position_size(
    *,
    equity: Decimal,
    entry_price: Decimal,
    stop_distance: Decimal,
    risk_pct: Decimal,
    max_leverage: Decimal,
    quantity_step: Decimal,
    entry_fee_bps: Decimal,
) -> PositionSize:
    """Size a futures position by stop risk, capped by fee-aware leverage."""
    if min(equity, entry_price, stop_distance, risk_pct, max_leverage) <= 0:
        raise ValueError("포지션 크기 입력값은 모두 0보다 커야 합니다.")
    if entry_fee_bps < 0:
        raise ValueError("수수료는 0 이상이어야 합니다.")

    risk_budget = equity * risk_pct / Decimal("100")
    risk_quantity = risk_budget / stop_distance
    fee_rate = entry_fee_bps / Decimal("10000")
    max_notional = (
        equity * max_leverage / (Decimal("1") + fee_rate * max_leverage)
    )
    leverage_quantity = max_notional / entry_price

    if risk_quantity <= leverage_quantity:
        raw_quantity = risk_quantity
        limited_by = "RISK"
    else:
        raw_quantity = leverage_quantity
        limited_by = "LEVERAGE"
    quantity = floor_to_step(raw_quantity, quantity_step)

    return PositionSize(
        quantity=quantity,
        risk_budget=risk_budget,
        position_notional=quantity * entry_price,
        limited_by=limited_by,
    )

