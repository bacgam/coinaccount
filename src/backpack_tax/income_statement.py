from __future__ import annotations

from decimal import Decimal
from typing import Any


ZERO = Decimal("0")


def _compact(value: Decimal, places: int = 2) -> str:
    rendered = f"{abs(value):,.{places}f}".rstrip("0").rstrip(".")
    return rendered if rendered else "0"


def _line(
    roman: str,
    label: str,
    amount: Decimal,
    *,
    subtotal: bool = False,
    final: bool = False,
) -> dict[str, Any]:
    negative = amount < ZERO
    amount_text = _compact(amount)
    return {
        "roman": roman,
        "label": label,
        "amount": amount,
        "amount_display": f"({amount_text})" if negative else amount_text,
        "negative": negative,
        "subtotal": subtotal,
        "final": final,
    }


def build_income_statement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    spot_rows = [
        row for row in rows if row.get("instrument_type") != "PERPETUAL_FUTURE"
    ]
    future_rows = [
        row for row in rows if row.get("instrument_type") == "PERPETUAL_FUTURE"
    ]

    recognized_sales = sum(
        (Decimal(str(row.get("matched_sell_amount") or 0)) for row in spot_rows),
        ZERO,
    )
    cost_of_sales = sum(
        (Decimal(str(row.get("realized_cost") or 0)) for row in spot_rows), ZERO
    )
    spot_profit = sum((Decimal(str(row.get("pnl") or 0)) for row in spot_rows), ZERO)
    fees = sum((Decimal(str(row.get("fees") or 0)) for row in rows), ZERO)
    future_income = sum(
        (
            max(Decimal(str(row.get("pnl") or 0)), ZERO)
            for row in future_rows
        ),
        ZERO,
    )
    future_expense = sum(
        (
            abs(min(Decimal(str(row.get("pnl") or 0)), ZERO))
            for row in future_rows
        ),
        ZERO,
    )
    unknown_basis_amount = sum(
        (Decimal(str(row.get("unknown_sell_amount") or 0)) for row in spot_rows),
        ZERO,
    )
    pending_transfer_basis = [
        {
            "asset": str(row.get("asset") or "-"),
            "quantity": Decimal(str(row.get("pending_transfer_basis_quantity") or 0)),
        }
        for row in spot_rows
        if Decimal(str(row.get("pending_transfer_basis_quantity") or 0)) > ZERO
    ]

    operating_profit = spot_profit - fees
    pre_tax_profit = operating_profit + future_income - future_expense
    tax_amount = ZERO
    net_profit = pre_tax_profit - tax_amount
    lines = [
        _line("I.", "현물 매도금액(취득가 확인분)", recognized_sales),
        _line("II.", "처분자산 취득원가", -cost_of_sales),
        _line("III.", "현물 실현손익", spot_profit, subtotal=True),
        _line("IV.", "거래·청산·네트워크 수수료", -fees),
        _line("V.", "수수료 반영 현물손익", operating_profit, subtotal=True),
        _line("VI.", "선물·기타 수익", future_income),
        _line("VII.", "선물·기타 비용", -future_expense),
        _line("VIII.", "세전 기간손익", pre_tax_profit, subtotal=True),
        _line("IX.", "세금(미반영)", -tax_amount),
        _line("X.", "기간 순손익", net_profit, subtotal=True, final=True),
    ]
    return {
        "lines": lines,
        "recognized_sales": recognized_sales,
        "cost_of_sales": cost_of_sales,
        "spot_profit": spot_profit,
        "fees": fees,
        "future_income": future_income,
        "future_expense": future_expense,
        "pre_tax_profit": pre_tax_profit,
        "tax_amount": tax_amount,
        "net_profit": net_profit,
        "unknown_basis_amount": unknown_basis_amount,
        "unknown_basis_display": _compact(unknown_basis_amount),
        "has_unknown_basis": unknown_basis_amount > ZERO,
        "pending_transfer_basis": pending_transfer_basis,
        "has_pending_transfer_basis": bool(pending_transfer_basis),
    }
