from __future__ import annotations

import json
from pathlib import Path

import backpack_tax.imports as imports_module
import pytest
from backpack_tax.db import TaxStore
from backpack_tax.imports import import_upbit_pdf_statement
from backpack_tax.normalize import (
    normalize_trusted_krw_deposits,
    normalize_upbit_transactions,
)
from backpack_tax.upbit_pdf import parse_upbit_statement_text


SAMPLE_STATEMENT = """거래내역서
발급정보
발급번호 UT-20260824-sample-0001 발급일자 2026. 08. 24.
거래소 거래내역
번호
거래일자 거래유형 자산명 거래수량 수수료 본인/상대방정보
거래시간 적요 거래단가 거래금액 정산금액 지갑주소정보
1
2026-08-16 매수 KRW-USDC 155.47703180 USDC 10 KRW
11:32:52 1,415.0 KRW 220,000 KRW 220,010 KRW
1 / 2
번호
거래일자 거래유형 자산명 거래수량 수수료 본인/상대방정보
거래시간 적요 거래단가 거래금액 정산금액 지갑주소정보
2
2026-08-17 출금 USDC 154.77703100 USDC 0.70000000 USDC
09:06:47 디지털 자산 154.77703100 USDC 155.47703100 USDC wallet-part-one
-part-two
2 / 2
"""


def test_upbit_statement_text_parses_trade_and_transfer() -> None:
    records = parse_upbit_statement_text(SAMPLE_STATEMENT)

    assert len(records) == 2
    trade, withdrawal = records
    assert trade["transaction_type"] == "매수"
    assert trade["occurred_at_utc"] == "2026-08-16T02:32:52+00:00"
    assert trade["asset_name"] == "KRW-USDC"
    assert trade["quantity"] == "155.47703180"
    assert trade["unit_price"] == "1415.0"
    assert trade["gross_amount"] == "220000"
    assert trade["fee"] == "10"
    assert withdrawal["transaction_type"] == "출금"
    assert withdrawal["wallet_address"] == "wallet-part-one-part-two"


def test_upbit_event_fingerprint_is_stable_across_overlapping_statements() -> None:
    first = parse_upbit_statement_text(SAMPLE_STATEMENT)[0]
    second_text = SAMPLE_STATEMENT.replace("UT-20260824-sample-0001", "UT-OTHER")
    second_text = second_text.replace("\n1\n2026-08-16", "\n99\n2026-08-16", 1)
    second = parse_upbit_statement_text(second_text)[0]

    assert first["statement_number"] != second["statement_number"]
    assert first["transaction_number"] != second["transaction_number"]
    assert first["event_fingerprint"] == second["event_fingerprint"]


def test_upbit_pdf_import_preserves_original_and_normalizes_without_password(
    tmp_path: Path, monkeypatch
) -> None:
    records = parse_upbit_statement_text(SAMPLE_STATEMENT)
    monkeypatch.setattr(
        imports_module,
        "parse_upbit_pdf_statement",
        lambda content, password: records,
    )
    content = b"encrypted-upbit-pdf-content"
    database = tmp_path / "tax.db"
    evidence = tmp_path / "evidence"

    with TaxStore(database) as store:
        first = import_upbit_pdf_statement(
            store,
            evidence_directory=evidence,
            original_filename="upbit.pdf",
            content=content,
            password="document-password",
        )
        second = import_upbit_pdf_statement(
            store,
            evidence_directory=evidence,
            original_filename="renamed.pdf",
            content=content,
            password="different-password-is-not-used-for-duplicate",
        )
        result = normalize_upbit_transactions(store)
        events = store.list_normalized_events(source_type="UPBIT")
        source = store.source_file(first.file_id)
        raw = json.loads(
            store.connection.execute(
                "SELECT raw_json FROM raw_records WHERE source_file_id = ? ORDER BY id LIMIT 1",
                (first.file_id,),
            ).fetchone()["raw_json"]
        )

    assert first.created is True
    assert second.created is False
    assert first.file_id == second.file_id
    assert first.row_count == 2
    assert result.created == 2
    assert len(events) == 2
    assert events[0]["event_type"] == "WITHDRAWAL"
    assert events[0]["classification_status"] == "NEEDS_REVIEW"
    assert events[1]["event_type"] == "TRADE_FILL"
    assert events[1]["asset"] == "USDC"
    assert events[1]["quote_asset"] == "KRW"
    assert events[1]["side"] == "BUY"
    assert Path(source["stored_path"]).read_bytes() == content
    assert "document-password" not in json.dumps(raw)
    assert b"document-password" not in database.read_bytes()


def test_upbit_krw_deposit_from_kbank_is_automatically_normalized(
    tmp_path: Path, monkeypatch
) -> None:
    record = {
        "statement_format": "UPBIT_PDF_V1",
        "transaction_type": "입금",
        "occurred_at_utc": "2026-08-20T01:00:00+00:00",
        "asset_name": "KRW",
        "quantity_asset": "KRW",
        "quantity": "1000000",
        "fee_asset": "KRW",
        "fee": "0",
        "event_fingerprint": "upbit-krw-deposit",
    }
    monkeypatch.setattr(
        imports_module,
        "parse_upbit_pdf_statement",
        lambda content, password: [record],
    )
    with TaxStore(tmp_path / "tax.db") as store:
        import_upbit_pdf_statement(
            store,
            evidence_directory=tmp_path / "evidence",
            original_filename="upbit-krw.pdf",
            content=b"upbit-krw-pdf",
            password="password",
        )
        normalize_upbit_transactions(store)
        event = store.list_normalized_events(source_type="UPBIT")[0]

    assert event["event_type"] == "DEPOSIT"
    assert event["asset"] == "KRW"
    assert event["classification_status"] == "NORMALIZED"
    assert "케이뱅크" in event["classification_reason"]


def test_existing_krw_deposits_are_always_normalized_even_after_user_review(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, classification_reason, created_at_utc
            ) VALUES (?, '2026-08-01', ?, 'KRW', '100000',
                'NEEDS_REVIEW', 'legacy', '2026-08-01')
            """,
            [
                ("UPBIT", "DEPOSIT"),
                ("BITHUMB", "DEPOSIT"),
                ("UPBIT", "DEPOSIT"),
                ("UPBIT", "WITHDRAWAL"),
                ("BITHUMB", "WITHDRAWAL"),
            ],
        )
        reviewed_id = int(
            store.connection.execute(
                """
                SELECT id FROM normalized_events
                WHERE source_type = 'UPBIT' AND event_type = 'DEPOSIT'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()["id"]
        )
        store.connection.execute(
            """
            INSERT INTO review_decisions (
                normalized_event_id, decision, rationale, decided_at_utc
            ) VALUES (?, 'REOPENED', '직접 재검토', '2026-08-02')
            """,
            (reviewed_id,),
        )
        store.connection.commit()

        updated = normalize_trusted_krw_deposits(store)
        rows = store.connection.execute(
            """
            SELECT id, source_type, event_type, classification_status, classification_reason
            FROM normalized_events ORDER BY id
            """
        ).fetchall()

    assert updated == 5
    assert rows[0]["classification_status"] == "NORMALIZED"
    assert "케이뱅크" in rows[0]["classification_reason"]
    assert rows[1]["classification_status"] == "NORMALIZED"
    assert "국민은행" in rows[1]["classification_reason"]
    assert rows[2]["classification_status"] == "NORMALIZED"
    assert "케이뱅크" in rows[2]["classification_reason"]
    assert rows[3]["classification_status"] == "NORMALIZED"
    assert "KRW 출금" in rows[3]["classification_reason"]
    assert "케이뱅크" in rows[3]["classification_reason"]
    assert rows[4]["classification_status"] == "NORMALIZED"
    assert "KRW 출금" in rows[4]["classification_reason"]
    assert "국민은행" in rows[4]["classification_reason"]

    with TaxStore(tmp_path / "tax.db") as store:
        with pytest.raises(ValueError, match="항상 자동 정규화"):
            store.record_review_decision(
                int(rows[4]["id"]),
                decision="REOPENED",
                rationale="다시 검토",
                decided_at_utc="2026-08-03T00:00:00+00:00",
            )
