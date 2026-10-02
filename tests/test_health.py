from __future__ import annotations

from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore
from backpack_tax.health import run_data_health_checks


def test_data_health_finds_ledger_anomalies_and_links_to_event(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        conflict_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01', 'DEPOSIT', 'CONFLICT', '1',
                    'NEEDS_REVIEW', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.execute(
            """
            INSERT INTO review_decisions (
                normalized_event_id, decision, rationale, decided_at_utc
            ) VALUES (?, 'CONFIRMED', 'test', '2026-01-02')
            """,
            (conflict_id,),
        )
        airdrop_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-02', 'AIRDROP', 'GIFT', '2',
                    'SPOT_CRYPTO', 'NORMALIZED', '2026-01-02'
                )
                """
            ).lastrowid
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, created_at_utc
            ) VALUES (
                'UPBIT', '2026-01-03', 'DEPOSIT', 'KRW', '10000',
                'NEEDS_REVIEW', '2026-01-03'
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                instrument_type, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-03T12:00:00', 'DEPOSIT', 'MISSING', '3',
                'SPOT_CRYPTO', 'NEEDS_REVIEW', '2026-01-03'
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-01-04', 'TRADE_FILL', 'MISSING',
                'SPOT_CRYPTO', 'SELL', '3', '5', '15', 'USDC', '0',
                'NORMALIZED', '2026-01-04'
            )
            """
        )
        store.connection.commit()
        health = run_data_health_checks(store)

    checks = {check["code"]: check for check in health["checks"]}
    assert checks["REVIEW_STATE_CONFLICT"]["count"] == 1
    assert checks["AIRDROP_BASIS_MISSING"]["count"] == 1
    assert checks["KRW_REVIEW_PENDING"]["count"] == 1
    assert checks["UNKNOWN_SALE_BASIS"]["count"] == 1
    assert "SOURCE_FILE_INTEGRITY" not in checks
    assert health["summary"]["critical"] >= 3

    page = app.test_client().get("/reviews/health")
    assert page.status_code == 200
    assert "데이터 건강검진".encode() in page.data
    assert "판정 이력과 현재 상태 불일치".encode() in page.data
    assert "원본 증빙 파일 무결성".encode() not in page.data
    assert "외부입금 취득가 입력".encode() in page.data

    event_page = app.test_client().get(f"/reviews?status=ALL&event_id={airdrop_id}")
    assert event_page.status_code == 200
    assert b"GIFT" in event_page.data
    assert b"CONFLICT" not in event_page.data

    deposit_page = app.test_client().get(
        "/reviews?status=PENDING&source=BACKPACK&event_type=DEPOSIT&asset=MISSING"
    )
    assert deposit_page.status_code == 200
    assert b"MISSING" in deposit_page.data
    assert b"CONFLICT" not in deposit_page.data
