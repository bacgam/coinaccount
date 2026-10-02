from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
from decimal import Decimal

from backpack_tax.analytics import asset_performance
from backpack_tax.db import TaxStore


def test_asset_performance_uses_moving_average_and_separate_fees(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        trades = (
            ("2026-01-01", "BUY", "2", "100", "200", "USDC", "0.1"),
            ("2026-01-02", "BUY", "1", "130", "130", "TEST", "0.01"),
            ("2026-01-03", "SELL", "1", "150", "150", "USDC", "0.2"),
        )
        for occurred_at, side, quantity, price, gross, fee_asset, fee in trades:
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset,
                    instrument_type, side, quantity, unit_price, gross_amount,
                    fee_asset, fee_quantity, classification_status,
                    created_at_utc
                ) VALUES (
                    'BACKPACK', ?, 'TRADE_FILL', 'TEST', 'SPOT_CRYPTO',
                    ?, ?, ?, ?, ?, ?, 'NORMALIZED', '2026-01-01'
                )
                """,
                (occurred_at, side, quantity, price, gross, fee_asset, fee),
            )
        store.connection.commit()

        row = asset_performance(store)[0]

    assert row["volume_display"] == "480"
    assert row["buy_amount_display"] == "330"
    assert row["sell_amount_display"] == "150"
    assert row["pnl_display"] == "40"
    assert row["fee_display"] == "1.6"
    assert row["has_unknown_basis"] is False


def test_approved_airdrop_basis_enters_moving_average_pnl(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset,
                    instrument_type, quantity, classification_status,
                    created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01T00:00:00Z', 'AIRDROP', 'TEST',
                    'SPOT_CRYPTO', '10', 'NORMALIZED', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.commit()
        store.upsert_event_valuation(
            event_id=event_id,
            valuation_status="VALUED",
            usd_value=Decimal("20"),
            unit_price_usd=Decimal("2"),
            native_value=Decimal("20"),
            native_currency="USD",
            valuation_method="USER_MANUAL_UNIT_PRICE",
            confidence="MEDIUM",
            reason="test",
            valued_at_utc="2026-01-01T00:00:00Z",
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-02T00:00:00Z', 'TRADE_FILL', 'TEST',
                'SPOT_CRYPTO', 'SELL', '5', '5', '25', 'USDC', '0',
                'NORMALIZED', '2026-01-02'
            )
            """
        )
        store.connection.commit()

        row = asset_performance(store, source_type="BACKPACK")[0]

    assert row["airdrop_quantity"] == Decimal("10")
    assert row["airdrop_cost"] == Decimal("20")
    assert row["airdrop_quantity_display"] == "10"
    assert row["realized_cost"] == Decimal("10")
    assert row["pnl"] == Decimal("15")
    assert row["has_unknown_basis"] is False


def test_personal_wallet_round_trip_carries_basis_without_linked_counterparty(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-01T00:00:00Z', 'TRADE_FILL', 'ROUND',
                'SPOT_CRYPTO', 'BUY', '10', '2', '20', 'USDC', '0',
                'NORMALIZED', '2026-01-01'
            )
            """
        )
        withdrawal_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-02T00:00:00Z', 'WITHDRAWAL', 'ROUND', '10',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', '2026-01-02'
                )
                """
            ).lastrowid
        )
        deposit_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-03T00:00:00Z', 'DEPOSIT', 'ROUND', '10',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', '2026-01-03'
                )
                """
            ).lastrowid
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-04T00:00:00Z', 'TRADE_FILL', 'ROUND',
                'SPOT_CRYPTO', 'SELL', '5', '4', '20', 'USDC', '0',
                'NORMALIZED', '2026-01-04'
            )
            """
        )
        store.connection.commit()
        for event_id in (withdrawal_id, deposit_id):
            store.record_personal_wallet_transfer(
                event_id,
                wallet_name="Ledger",
                wallet_address="0x123" if event_id == withdrawal_id else "",
                basis_mode="AUTO_CARRY",
                unit_cost="",
                fee_cost="0",
                note="test",
                recorded_at_utc="2026-01-05T00:00:00+00:00",
            )

        row = asset_performance(store, source_type="BACKPACK")[0]

    assert row["transfer_out_quantity"] == Decimal("10")
    assert row["transfer_in_quantity"] == Decimal("10")
    assert row["transfer_in_cost"] == Decimal("20")
    assert row["pending_transfer_basis_quantity"] == Decimal("0")
    assert row["realized_cost"] == Decimal("10")
    assert row["pnl"] == Decimal("10")
    assert row["has_unknown_basis"] is False


def test_personal_wallet_deposit_can_use_manual_basis_without_prior_withdrawal(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        deposit_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01T00:00:00Z', 'DEPOSIT', 'OLD', '4',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-02T00:00:00Z', 'TRADE_FILL', 'OLD',
                'SPOT_CRYPTO', 'SELL', '2', '5', '10', 'USDC', '0',
                'NORMALIZED', '2026-01-02'
            )
            """
        )
        store.connection.commit()
        store.record_personal_wallet_transfer(
            deposit_id,
            wallet_name="과거 개인 지갑",
            wallet_address="",
            basis_mode="MANUAL_BASIS",
            unit_cost="3",
            fee_cost="2",
            note="과거 매수내역 확인",
            recorded_at_utc="2026-01-03T00:00:00+00:00",
        )

        row = asset_performance(store, source_type="BACKPACK")[0]

    assert row["transfer_in_cost"] == Decimal("14")
    assert row["realized_cost"] == Decimal("7")
    assert row["pnl"] == Decimal("3")
    assert row["has_unknown_basis"] is False


def test_perpetual_summary_keeps_pnl_and_fees_separate(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, realized_pnl, funding_quantity,
                interest_quantity, trading_fee_quantity,
                liquidation_fee_quantity, classification_status,
                created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-01', 'FUTURES_POSITION_CLOSED', 'BTC',
                'PERPETUAL_FUTURE', '20', '-1', '0.5', '2', '0.1',
                'NORMALIZED', '2026-01-01'
            )
            """
        )
        store.connection.commit()

        row = asset_performance(store)[0]

    assert row["pnl_display"] == "18.5"
    assert row["fee_display"] == "2.1"


def test_period_pnl_uses_cost_basis_from_before_selected_period(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        for values in (
            ("2025-12-31T14:00:00+00:00", "BUY", "1", "100", "100", "0.1"),
            ("2025-12-31T16:00:00+00:00", "SELL", "1", "150", "150", "0.2"),
        ):
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset,
                    instrument_type, side, quantity, unit_price, gross_amount,
                    fee_asset, fee_quantity, classification_status,
                    created_at_utc
                ) VALUES (
                    'BACKPACK', ?, 'TRADE_FILL', 'TEST', 'SPOT_CRYPTO',
                    ?, ?, ?, ?, 'USDC', ?, 'NORMALIZED', '2026-01-01'
                )
                """,
                values,
            )
        store.connection.commit()
        rows = asset_performance(
            store,
            start_utc=datetime(2025, 12, 31, 15, tzinfo=UTC),
            end_utc=datetime(2026, 12, 31, 15, tzinfo=UTC),
        )

    assert len(rows) == 1
    assert rows[0]["volume_display"] == "150"
    assert rows[0]["buy_amount_display"] == "0"
    assert rows[0]["sell_amount_display"] == "150"
    assert rows[0]["pnl_display"] == "50"
    assert rows[0]["fee_display"] == "0.2"


def test_asset_performance_can_keep_exchange_currencies_separate(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        for source_type, gross_amount in (("BACKPACK", "100"), ("UPBIT", "140000")):
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset,
                    instrument_type, side, quantity, unit_price, gross_amount,
                    fee_asset, fee_quantity, classification_status, created_at_utc
                ) VALUES (
                    ?, '2026-01-01', 'TRADE_FILL', 'BTC', 'SPOT_CRYPTO',
                    'BUY', '1', ?, ?, 'KRW', '0', 'NORMALIZED', '2026-01-01'
                )
                """,
                (source_type, gross_amount, gross_amount),
            )
        store.connection.commit()

        backpack = asset_performance(store, source_type="BACKPACK")
        upbit = asset_performance(store, source_type="UPBIT")

    assert backpack[0]["buy_amount_display"] == "100"
    assert upbit[0]["buy_amount_display"] == "140,000"


def test_asset_performance_only_uses_normalized_events(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', ?, 'TRADE_FILL', 'BTC', 'SPOT_CRYPTO',
                ?, '1', ?, ?, 'USDC', '0', ?, '2026-01-01'
            )
            """,
            [
                ("2026-01-01", "BUY", "100", "100", "NORMALIZED"),
                ("2026-01-02", "SELL", "150", "150", "EXCLUDED"),
                ("2026-01-03", "SELL", "160", "160", "NEEDS_REVIEW"),
            ],
        )
        store.connection.commit()

        row = asset_performance(store, source_type="BACKPACK")[0]

    assert row["buy_amount_display"] == "100"
    assert row["sell_amount_display"] == "0"
    assert row["pnl_display"] == "0"


def test_same_currency_internal_transfer_carries_basis_and_network_fee(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'UPBIT', '2026-01-01T00:00:00Z', 'TRADE_FILL', 'ETH',
                'SPOT_CRYPTO', 'BUY', '2', '100000', '200000',
                'KRW', '0', 'NORMALIZED', '2026-01-01'
            )
            """
        )
        withdrawal = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                fee_asset, fee_quantity, classification_status, is_internal,
                created_at_utc
            ) VALUES (
                'UPBIT', '2026-01-02T00:00:00Z', 'WITHDRAWAL', 'ETH', '1.1',
                'ETH', '0.1', 'NORMALIZED', 1, '2026-01-02'
            )
            """
        ).lastrowid
        deposit = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, is_internal, created_at_utc
            ) VALUES (
                'BITHUMB', '2026-01-02T00:10:00Z', 'DEPOSIT', 'ETH', '1',
                'NORMALIZED', 1, '2026-01-02'
            )
            """
        ).lastrowid
        store.connection.execute(
            """
            INSERT INTO transfer_matches (
                withdrawal_event_id, deposit_event_id, asset,
                withdrawal_quantity, deposit_quantity, fee_quantity,
                status, rationale, created_at_utc
            ) VALUES (?, ?, 'ETH', '1.1', '1', '0.1', 'ACTIVE', 'test', '2026-01-02')
            """,
            (withdrawal, deposit),
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BITHUMB', '2026-01-03T00:00:00Z', 'TRADE_FILL', 'ETH',
                'SPOT_CRYPTO', 'SELL', '1', '150000', '150000',
                'KRW', '0', 'NORMALIZED', '2026-01-03'
            )
            """
        )
        store.connection.commit()

        upbit = asset_performance(store, source_type="UPBIT")[0]
        bithumb = asset_performance(store, source_type="BITHUMB")[0]

    assert upbit["transfer_out_display"] == "1.1"
    assert upbit["transfer_fee_display"] == "0.1"
    assert upbit["transfer_fee_cost_display"] == "10,000"
    assert upbit["fee_display"] == "10,000"
    assert bithumb["transfer_in_display"] == "1"
    assert bithumb["transfer_in_cost_display"] == "100,000"
    assert bithumb["realized_cost"] == Decimal("100000")
    assert bithumb["pnl_display"] == "50,000"
    assert bithumb["has_unknown_basis"] is False


def test_cross_currency_internal_transfer_waits_for_price_evidence(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-01T00:00:00Z', 'TRADE_FILL', 'BTC',
                'SPOT_CRYPTO', 'BUY', '1', '100', '100',
                'USDC', '0', 'NORMALIZED', '2026-01-01'
            )
            """
        )
        withdrawal = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, is_internal, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-02T00:00:00Z', 'WITHDRAWAL', 'BTC', '1',
                'NORMALIZED', 1, '2026-01-02'
            )
            """
        ).lastrowid
        deposit = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, is_internal, created_at_utc
            ) VALUES (
                'UPBIT', '2026-01-02T00:10:00Z', 'DEPOSIT', 'BTC', '0.99',
                'NORMALIZED', 1, '2026-01-02'
            )
            """
        ).lastrowid
        store.connection.execute(
            """
            INSERT INTO transfer_matches (
                withdrawal_event_id, deposit_event_id, asset,
                withdrawal_quantity, deposit_quantity, fee_quantity,
                status, rationale, created_at_utc
            ) VALUES (?, ?, 'BTC', '1', '0.99', '0.01', 'ACTIVE', 'test', '2026-01-02')
            """,
            (withdrawal, deposit),
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'UPBIT', '2026-01-03T00:00:00Z', 'TRADE_FILL', 'BTC',
                'SPOT_CRYPTO', 'SELL', '0.99', '150000000', '148500000',
                'KRW', '0', 'NORMALIZED', '2026-01-03'
            )
            """
        )
        store.connection.commit()

        upbit = asset_performance(store, source_type="UPBIT")[0]

    assert upbit["pending_transfer_basis_display"] == "0.99"
    assert upbit["unknown_basis_display"] == "0.99"
    assert upbit["unknown_basis_amount_display"] == "148,500,000"
    assert upbit["pnl_display"] == "0"
