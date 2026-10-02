from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


@dataclass(frozen=True)
class PaperAccount:
    name: str
    initial_balance: Decimal
    cash_balance: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    funding_pnl: Decimal
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    max_leverage: Decimal
    daily_loss_limit_pct: Decimal
    risk_per_trade_pct: Decimal
    max_daily_entries: int


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    quantity: Decimal
    entry_price: Decimal


class PaperBroker:
    """Strategy-independent derivatives simulator with no exchange order access."""

    def __init__(self, connection: sqlite3.Connection, account_name: str = "default") -> None:
        self.connection = connection
        self.account_name = account_name
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_accounts (
                name TEXT PRIMARY KEY,
                initial_balance TEXT NOT NULL,
                cash_balance TEXT NOT NULL,
                realized_pnl TEXT NOT NULL DEFAULT '0',
                fees_paid TEXT NOT NULL DEFAULT '0',
                funding_pnl TEXT NOT NULL DEFAULT '0',
                maker_fee_bps TEXT NOT NULL,
                taker_fee_bps TEXT NOT NULL,
                max_leverage TEXT NOT NULL DEFAULT '1',
                daily_loss_limit_pct TEXT NOT NULL DEFAULT '2',
                risk_per_trade_pct TEXT NOT NULL DEFAULT '0.5',
                max_daily_entries INTEGER NOT NULL DEFAULT 10,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                account_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (account_name, symbol),
                FOREIGN KEY (account_name) REFERENCES paper_accounts(name)
            );

            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                filled_at_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity TEXT NOT NULL,
                price TEXT NOT NULL,
                liquidity TEXT NOT NULL,
                fee TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                FOREIGN KEY (account_name) REFERENCES paper_accounts(name)
            );

            CREATE TABLE IF NOT EXISTS paper_daily_risk (
                account_name TEXT NOT NULL,
                day_utc TEXT NOT NULL,
                start_equity TEXT NOT NULL,
                halted INTEGER NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (account_name, day_utc),
                FOREIGN KEY (account_name) REFERENCES paper_accounts(name)
            );
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(paper_accounts)")
        }
        if "max_leverage" not in columns:
            self.connection.execute(
                "ALTER TABLE paper_accounts "
                "ADD COLUMN max_leverage TEXT NOT NULL DEFAULT '1'"
            )
        if "daily_loss_limit_pct" not in columns:
            self.connection.execute(
                "ALTER TABLE paper_accounts "
                "ADD COLUMN daily_loss_limit_pct TEXT NOT NULL DEFAULT '2'"
            )
        if "risk_per_trade_pct" not in columns:
            self.connection.execute(
                "ALTER TABLE paper_accounts "
                "ADD COLUMN risk_per_trade_pct TEXT NOT NULL DEFAULT '0.5'"
            )
        if "max_daily_entries" not in columns:
            self.connection.execute(
                "ALTER TABLE paper_accounts "
                "ADD COLUMN max_daily_entries INTEGER NOT NULL DEFAULT 10"
            )
        daily_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(paper_daily_risk)")
        }
        if "entry_count" not in daily_columns:
            self.connection.execute(
                "ALTER TABLE paper_daily_risk "
                "ADD COLUMN entry_count INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()

    def initialize(
        self,
        initial_balance: Decimal,
        *,
        maker_fee_bps: Decimal,
        taker_fee_bps: Decimal,
        max_leverage: Decimal,
        daily_loss_limit_pct: Decimal,
        risk_per_trade_pct: Decimal,
        max_daily_entries: int,
    ) -> PaperAccount:
        if initial_balance <= ZERO:
            raise ValueError("모의매매 초기 자금은 0보다 커야 합니다.")
        if max_leverage <= ZERO:
            raise ValueError("최대 레버리지는 0보다 커야 합니다.")
        if daily_loss_limit_pct <= ZERO or daily_loss_limit_pct > Decimal("100"):
            raise ValueError("일일 손실 제한률은 0보다 크고 100 이하여야 합니다.")
        if risk_per_trade_pct <= ZERO or risk_per_trade_pct > Decimal("100"):
            raise ValueError("거래당 위험률은 0보다 크고 100 이하여야 합니다.")
        if max_daily_entries <= 0:
            raise ValueError("하루 신규 진입 횟수는 1 이상이어야 합니다.")
        now = int(time.time() * 1_000)
        self.connection.execute(
            """
            INSERT INTO paper_accounts (
                name, initial_balance, cash_balance, maker_fee_bps,
                taker_fee_bps, max_leverage, daily_loss_limit_pct,
                risk_per_trade_pct, max_daily_entries, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                maker_fee_bps = excluded.maker_fee_bps,
                taker_fee_bps = excluded.taker_fee_bps,
                max_leverage = excluded.max_leverage,
                daily_loss_limit_pct = excluded.daily_loss_limit_pct,
                risk_per_trade_pct = excluded.risk_per_trade_pct,
                max_daily_entries = excluded.max_daily_entries,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                self.account_name,
                str(initial_balance),
                str(initial_balance),
                str(maker_fee_bps),
                str(taker_fee_bps),
                str(max_leverage),
                str(daily_loss_limit_pct),
                str(risk_per_trade_pct),
                max_daily_entries,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.account()

    def account(self) -> PaperAccount:
        row = self.connection.execute(
            "SELECT * FROM paper_accounts WHERE name = ?", (self.account_name,)
        ).fetchone()
        if row is None:
            raise ValueError("모의 계좌가 아직 초기화되지 않았습니다.")
        return PaperAccount(
            name=row["name"],
            initial_balance=Decimal(row["initial_balance"]),
            cash_balance=Decimal(row["cash_balance"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            fees_paid=Decimal(row["fees_paid"]),
            funding_pnl=Decimal(row["funding_pnl"]),
            maker_fee_bps=Decimal(row["maker_fee_bps"]),
            taker_fee_bps=Decimal(row["taker_fee_bps"]),
            max_leverage=Decimal(row["max_leverage"]),
            daily_loss_limit_pct=Decimal(row["daily_loss_limit_pct"]),
            risk_per_trade_pct=Decimal(row["risk_per_trade_pct"]),
            max_daily_entries=int(row["max_daily_entries"]),
        )

    def position(self, symbol: str) -> PaperPosition:
        row = self.connection.execute(
            """
            SELECT symbol, quantity, entry_price
            FROM paper_positions
            WHERE account_name = ? AND symbol = ?
            """,
            (self.account_name, symbol),
        ).fetchone()
        if row is None:
            return PaperPosition(symbol=symbol, quantity=ZERO, entry_price=ZERO)
        return PaperPosition(
            symbol=row["symbol"],
            quantity=Decimal(row["quantity"]),
            entry_price=Decimal(row["entry_price"]),
        )

    @staticmethod
    def _is_risk_reducing(
        current_quantity: Decimal, projected_quantity: Decimal
    ) -> bool:
        if current_quantity == ZERO:
            return False
        if projected_quantity == ZERO:
            return True
        same_side = (projected_quantity > ZERO) == (current_quantity > ZERO)
        return same_side and abs(projected_quantity) < abs(current_quantity)

    def daily_risk(self, symbol: str, mark_price: Decimal) -> dict[str, object]:
        account = self.account()
        position = self.position(symbol)
        unrealized_pnl = position.quantity * (mark_price - position.entry_price)
        equity = account.cash_balance + unrealized_pnl
        day_utc = datetime.now(UTC).date().isoformat()
        now = int(time.time() * 1_000)
        row = self.connection.execute(
            """
            SELECT start_equity, halted, entry_count
            FROM paper_daily_risk
            WHERE account_name = ? AND day_utc = ?
            """,
            (self.account_name, day_utc),
        ).fetchone()

        if row is None:
            start_equity = equity
            halted_before = False
            entry_count = 0
            self.connection.execute(
                """
                INSERT INTO paper_daily_risk (
                    account_name, day_utc, start_equity, halted, updated_at_ms
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (self.account_name, day_utc, str(start_equity), now),
            )
        else:
            start_equity = Decimal(row["start_equity"])
            halted_before = bool(row["halted"])
            entry_count = int(row["entry_count"])

        loss = max(ZERO, start_equity - equity)
        loss_limit = start_equity * account.daily_loss_limit_pct / Decimal("100")
        halted = halted_before or loss >= loss_limit
        self.connection.execute(
            """
            UPDATE paper_daily_risk
            SET halted = ?, updated_at_ms = ?
            WHERE account_name = ? AND day_utc = ?
            """,
            (int(halted), now, self.account_name, day_utc),
        )
        self.connection.commit()
        return {
            "dayUtc": day_utc,
            "startEquity": start_equity,
            "currentEquity": equity,
            "dailyLoss": loss,
            "dailyLossLimit": loss_limit,
            "dailyLossLimitPct": account.daily_loss_limit_pct,
            "tradingHalted": halted,
            "entryCount": entry_count,
            "maxDailyEntries": account.max_daily_entries,
            "entriesRemaining": max(0, account.max_daily_entries - entry_count),
        }

    def apply_fill(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        liquidity: str = "Taker",
    ) -> PaperPosition:
        """Apply a simulated fill. This method cannot call Backpack orders."""
        if side not in {"Bid", "Ask"}:
            raise ValueError("side는 Bid 또는 Ask여야 합니다.")
        if liquidity not in {"Maker", "Taker"}:
            raise ValueError("liquidity는 Maker 또는 Taker여야 합니다.")
        if quantity <= ZERO or price <= ZERO:
            raise ValueError("수량과 가격은 0보다 커야 합니다.")

        account = self.account()
        position = self.position(symbol)
        signed_fill = quantity if side == "Bid" else -quantity
        new_quantity = position.quantity + signed_fill
        risk_reducing = self._is_risk_reducing(position.quantity, new_quantity)
        entering_new_position = position.quantity == ZERO and new_quantity != ZERO
        daily_risk = self.daily_risk(symbol, price)
        if daily_risk["tradingHalted"] and not risk_reducing:
            raise ValueError(
                "일일 손실 한도에 도달해 신규 진입과 증액이 중단되었습니다. "
                "포지션 축소 또는 청산만 가능합니다."
            )
        if (
            entering_new_position
            and int(daily_risk["entryCount"]) >= account.max_daily_entries
        ):
            raise ValueError(
                f"하루 신규 진입 한도 {account.max_daily_entries}회에 도달했습니다."
            )
        same_direction = (
            position.quantity == ZERO
            or (position.quantity > ZERO and signed_fill > ZERO)
            or (position.quantity < ZERO and signed_fill < ZERO)
        )

        realized_pnl = ZERO
        if same_direction:
            total_cost = (
                abs(position.quantity) * position.entry_price + quantity * price
            )
            new_entry_price = total_cost / abs(new_quantity)
        else:
            closing_quantity = min(abs(position.quantity), quantity)
            direction = Decimal("1") if position.quantity > ZERO else Decimal("-1")
            realized_pnl = closing_quantity * (price - position.entry_price) * direction
            if new_quantity == ZERO:
                new_entry_price = ZERO
            elif (new_quantity > ZERO) == (position.quantity > ZERO):
                new_entry_price = position.entry_price
            else:
                new_entry_price = price

        fee_bps = (
            account.maker_fee_bps if liquidity == "Maker" else account.taker_fee_bps
        )
        fee = quantity * price * fee_bps / TEN_THOUSAND
        equity_before_fee = account.cash_balance + (
            position.quantity * (price - position.entry_price)
        )
        projected_equity = equity_before_fee - fee
        projected_notional = abs(new_quantity) * price
        if projected_equity <= ZERO:
            raise ValueError("체결 후 모의 계좌 자산이 0 이하가 됩니다.")
        if projected_notional > projected_equity * account.max_leverage:
            projected_leverage = projected_notional / projected_equity
            raise ValueError(
                f"최대 레버리지 {account.max_leverage}배를 초과합니다 "
                f"(예상 {projected_leverage:.4f}배)."
            )
        projected_daily_loss = max(
            ZERO, Decimal(daily_risk["startEquity"]) - projected_equity
        )
        if (
            projected_daily_loss >= Decimal(daily_risk["dailyLossLimit"])
            and not risk_reducing
        ):
            raise ValueError(
                f"일일 손실 제한 {account.daily_loss_limit_pct}%를 초과하는 체결입니다."
            )
        now = int(time.time() * 1_000)

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO paper_positions (
                    account_name, symbol, quantity, entry_price, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_name, symbol) DO UPDATE SET
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    self.account_name,
                    symbol,
                    str(new_quantity),
                    str(new_entry_price),
                    now,
                ),
            )
            if entering_new_position:
                self.connection.execute(
                    """
                    UPDATE paper_daily_risk
                    SET entry_count = entry_count + 1, updated_at_ms = ?
                    WHERE account_name = ? AND day_utc = ?
                    """,
                    (now, self.account_name, daily_risk["dayUtc"]),
                )
            self.connection.execute(
                """
                UPDATE paper_accounts
                SET cash_balance = ?, realized_pnl = ?, fees_paid = ?, updated_at_ms = ?
                WHERE name = ?
                """,
                (
                    str(account.cash_balance + realized_pnl - fee),
                    str(account.realized_pnl + realized_pnl),
                    str(account.fees_paid + fee),
                    now,
                    self.account_name,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO paper_fills (
                    account_name, filled_at_ms, symbol, side, quantity,
                    price, liquidity, fee, realized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_name,
                    now,
                    symbol,
                    side,
                    str(quantity),
                    str(price),
                    liquidity,
                    str(fee),
                    str(realized_pnl),
                ),
            )

        self.daily_risk(symbol, price)
        return self.position(symbol)

    def status(self, symbol: str, mark_price: Decimal) -> dict[str, object]:
        account = self.account()
        position = self.position(symbol)
        unrealized_pnl = position.quantity * (mark_price - position.entry_price)
        equity = account.cash_balance + unrealized_pnl
        position_notional = abs(position.quantity) * mark_price
        current_leverage = (
            position_notional / equity if equity > ZERO else Decimal("Infinity")
        )
        status = {
            "symbol": symbol,
            "initialBalance": account.initial_balance,
            "cashBalance": account.cash_balance,
            "positionQuantity": position.quantity,
            "entryPrice": position.entry_price,
            "markPrice": mark_price,
            "unrealizedPnl": unrealized_pnl,
            "realizedPnl": account.realized_pnl,
            "feesPaid": account.fees_paid,
            "fundingPnl": account.funding_pnl,
            "equity": equity,
            "positionNotional": position_notional,
            "currentLeverage": current_leverage,
            "maxLeverage": account.max_leverage,
            "riskPerTradePct": account.risk_per_trade_pct,
            "makerFeeBps": account.maker_fee_bps,
            "takerFeeBps": account.taker_fee_bps,
        }
        status.update(self.daily_risk(symbol, mark_price))
        return status
