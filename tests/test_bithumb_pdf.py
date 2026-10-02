from __future__ import annotations

import hashlib
import json
from pathlib import Path

import backpack_tax.imports as imports_module
from backpack_tax.bithumb_pdf import parse_bithumb_statement_pages
from backpack_tax.db import TaxStore
from backpack_tax.imports import import_bithumb_pdf_statement
from backpack_tax.normalize import normalize_bithumb_transactions


SAMPLE_PAGE = """거래내역 확인서
거래일시 자산명 거래유형 거래수량 체결가격 거래금액 정산금액 보유자산잔고 원화환산잔고 내용
2026-08-21
10:54:12 USDT 매수 247.97961491
USDT
1,376
KRW
341,220
KRW
-341,356
KRW
965.66159455
USDT
718
KRW 지정가 매수 - taker
2026-08-21
10:53:59 KRW 입금 340,000
KRW
340,000
KRW
340,000
KRW
342,074
KRW 민감한고객명 123-456-789012
"""


def test_bithumb_statement_parses_transactions_without_free_text() -> None:
    records = parse_bithumb_statement_pages([SAMPLE_PAGE])

    assert len(records) == 2
    trade, deposit = records
    assert trade["transaction_type"] == "매수"
    assert trade["occurred_at_utc"] == "2026-08-21T01:54:12+00:00"
    assert trade["asset"] == "USDT"
    assert trade["quantity"] == "247.97961491"
    assert trade["unit_price"] == "1376"
    assert trade["gross_amount"] == "341220"
    assert trade["fee"] == "136"
    assert trade["is_maker"] is False
    assert trade["order_type"] == "LIMIT"
    assert deposit["transaction_type"] == "입금"
    assert deposit["fee"] == "0"
    serialized = json.dumps(records, ensure_ascii=False)
    assert "민감한고객명" not in serialized
    assert "123-456-789012" not in serialized
    assert "detail" not in serialized
    assert "memo" not in serialized
    assert "account" not in serialized


def test_bithumb_pdf_import_discards_original_and_stores_sanitized_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    records = parse_bithumb_statement_pages([SAMPLE_PAGE])
    monkeypatch.setattr(
        imports_module,
        "parse_bithumb_pdf_statement",
        lambda content: records,
    )
    original = "fake-pdf 민감한고객명 123-456-789012".encode()
    database = tmp_path / "tax.db"
    evidence = tmp_path / "evidence"

    with TaxStore(database) as store:
        first = import_bithumb_pdf_statement(
            store,
            evidence_directory=evidence,
            original_filename="민감한고객명-거래내역.pdf",
            content=original,
        )
        second = import_bithumb_pdf_statement(
            store,
            evidence_directory=evidence,
            original_filename="renamed.pdf",
            content=original,
        )
        normalized = normalize_bithumb_transactions(store)
        source = store.source_file(first.file_id)
        events = store.list_normalized_events(source_type="BITHUMB")

    stored_path = Path(source["stored_path"])
    stored = stored_path.read_bytes()
    evidence_json = json.loads(stored)
    assert first.created is True
    assert first.retention_mode == "SANITIZED_ONLY"
    assert second.created is False
    assert first.file_id == second.file_id
    assert first.sha256 == hashlib.sha256(original).hexdigest()
    assert first.row_count == 2
    assert source["status"] == "SANITIZED_IMPORTED"
    assert source["original_filename"].endswith(".sanitized.json")
    assert stored != original
    assert evidence_json["privacy"]["original_pdf_retained"] is False
    assert evidence_json["privacy"]["personal_information_retained"] is False
    assert evidence_json["source_document_sha256"] == first.sha256
    assert "민감한고객명".encode() not in stored
    assert b"123-456-789012" not in stored
    assert "민감한고객명".encode() not in database.read_bytes()
    assert b"123-456-789012" not in database.read_bytes()
    assert normalized.created == 2
    by_type = {event["event_type"]: event for event in events}
    assert by_type["DEPOSIT"]["classification_status"] == "NORMALIZED"
    assert "국민은행" in by_type["DEPOSIT"]["classification_reason"]
    assert by_type["TRADE_FILL"]["side"] == "BUY"
    assert by_type["TRADE_FILL"]["fee_quantity"] == "136"
