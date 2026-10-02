from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore
from backpack_tax.imports import import_exchange_csv
from backpack_tax.normalize import normalize_all_backpack, normalize_backpack_transfers


def test_upbit_csv_is_preserved_and_duplicate_safe(tmp_path: Path) -> None:
    content = "거래일시,마켓,종류,수량\n2026-01-01 00:00:00,KRW-BTC,매수,0.1\n".encode(
        "utf-8-sig"
    )
    database = tmp_path / "tax.db"
    evidence = tmp_path / "evidence"

    with TaxStore(database) as store:
        first = import_exchange_csv(
            store,
            evidence_directory=evidence,
            source_type="UPBIT",
            original_filename="업비트_거래내역.csv",
            content=content,
        )
        second = import_exchange_csv(
            store,
            evidence_directory=evidence,
            source_type="UPBIT",
            original_filename="다른이름.csv",
            content=content,
        )
        statistics = store.dashboard_statistics()
        source = store.source_file(first.file_id)

    assert first.created is True
    assert second.created is False
    assert first.file_id == second.file_id
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert first.row_count == 1
    assert statistics["source_files"] == 1
    assert statistics["raw_records"] == 1
    assert Path(source["stored_path"]).read_bytes() == content


def test_bithumb_cp949_csv_is_supported(tmp_path: Path) -> None:
    content = "거래일시,자산,구분\n2026-02-01 12:00:00,ETH,입금\n".encode("cp949")

    with TaxStore(tmp_path / "tax.db") as store:
        result = import_exchange_csv(
            store,
            evidence_directory=tmp_path / "evidence",
            source_type="BITHUMB",
            original_filename="bithumb.csv",
            content=content,
        )

    assert result.created is True
    assert result.row_count == 1


def test_local_web_app_uploads_csv_and_renders_pages(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()

    assert client.get("/").status_code == 200
    assert client.get("/ledger").status_code == 200
    assert client.get("/reports").status_code == 200

    response = client.post(
        "/imports",
        data={
            "source_type": "BACKPACK",
            "document_type": "BALANCES",
            "csv_file": (
                io.BytesIO(b"time,market,side\n2026-01-01,BTC,bid\n"),
                "backpack.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "신규".encode() in response.data
    confirm_path = re.search(
        rb'action="(/imports/preview/[^\"]+/confirm)"', response.data
    ).group(1).decode()
    with client.session_transaction() as session:
        csrf = session["delete_csrf_token"]
    confirmed = client.post(
        confirm_path,
        data={"csrf_token": csrf},
        follow_redirects=True,
    )
    assert confirmed.status_code == 200
    assert "신규 1건을 가져왔습니다".encode() in confirmed.data
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        assert store.dashboard_statistics()["source_files"] == 1
        assert store.dashboard_statistics()["raw_records"] == 1


def test_upbit_import_screen_requires_encrypted_statement_pdf(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()

    page = client.get("/imports")
    assert page.status_code == 200
    assert "이메일로 받은 업비트 거래내역서 PDF".encode() in page.data
    assert b'.pdf,application/pdf' in page.data
    assert "업비트 PDF 비밀번호".encode() in page.data
    assert b"const usesStatementPdf = isUpbit || isBithumb" in page.data
    assert b"pdfPasswordWrap.hidden = !isUpbit" in page.data
    assert "빗썸 거래내역확인서 PDF".encode() in page.data
    assert "빗썸 파일 비밀번호".encode() not in page.data

    css = client.get("/static/backpack.css")
    assert css.status_code == 200
    assert b".upload-form [hidden] { display: none !important; }" in css.data
    assert b".usdt-market-controls" in css.data
    assert b"gap: 0" in css.data

    csv_response = client.post(
        "/imports",
        data={
            "source_type": "UPBIT",
            "document_type": "TRANSACTIONS",
            "pdf_password": "temporary",
            "source_file": (io.BytesIO(b"header\nvalue\n"), "upbit.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "업비트는 거래내역서 PDF 파일을 선택하세요.".encode() in csv_response.data

    no_password_response = client.post(
        "/imports",
        data={
            "source_type": "UPBIT",
            "document_type": "TRANSACTIONS",
            "source_file": (io.BytesIO(b"not-a-real-pdf"), "upbit.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "업비트 거래내역서 PDF 비밀번호를 입력하세요.".encode() in no_password_response.data

    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        assert store.dashboard_statistics()["source_files"] == 0


def test_ledger_exchange_tabs_filter_events_by_source(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                classification_status, created_at_utc
            ) VALUES (?, ?, 'TRADE_FILL', ?, 'NORMALIZED', ?)
            """,
            [
                ("BACKPACK", "2026-08-01T00:00:00Z", "BACKPACK_ONLY", "2026-08-01T00:00:00Z"),
                ("UPBIT", "2026-08-02T00:00:00Z", "UPBIT_ONLY", "2026-08-02T00:00:00Z"),
                ("BITHUMB", "2026-08-03T00:00:00Z", "BITHUMB_ONLY", "2026-08-03T00:00:00Z"),
            ],
        )
        store.connection.commit()

    client = app.test_client()
    backpack = client.get("/ledger")
    upbit = client.get("/ledger?exchange=UPBIT")
    bithumb = client.get("/ledger?exchange=BITHUMB")
    invalid = client.get("/ledger?exchange=UNKNOWN")

    assert "거래소별 취득가액 원장".encode() in backpack.data
    assert b'class="ledger-table"' in backpack.data
    assert b"BACKPACK_ONLY" in backpack.data
    assert b"UPBIT_ONLY" not in backpack.data
    assert b"UPBIT_ONLY" in upbit.data
    assert b"BACKPACK_ONLY" not in upbit.data
    assert b"BITHUMB_ONLY" in bithumb.data
    assert b"UPBIT_ONLY" not in bithumb.data
    assert b"BACKPACK_ONLY" in invalid.data


def test_asset_performance_tabs_filter_by_exchange(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (?, '2026-08-01T00:00:00Z', 'TRADE_FILL', ?,
                'SPOT_CRYPTO', 'BUY', '1', ?, ?, ?, '1', 'NORMALIZED',
                '2026-08-01T00:00:00Z')
            """,
            [
                ("BACKPACK", "BACKPACK_ONLY", "100", "100", "USDC"),
                ("UPBIT", "UPBIT_ONLY", "200000", "200000", "KRW"),
                ("BITHUMB", "BITHUMB_ONLY", "300000", "300000", "KRW"),
            ],
        )
        store.connection.commit()

    client = app.test_client()
    backpack = client.get("/assets?exchange=BACKPACK")
    upbit = client.get("/assets?exchange=UPBIT")
    bithumb = client.get("/assets?exchange=BITHUMB")
    invalid = client.get("/assets?exchange=UNKNOWN")

    assert b"BACKPACK_ONLY" in backpack.data
    assert b"UPBIT_ONLY" not in backpack.data
    assert "누적 매수액 (USD)".encode() in backpack.data
    assert "실제 로고 업로드".encode() not in backpack.data
    assert b"UPBIT_ONLY" in upbit.data
    assert b"BACKPACK_ONLY" not in upbit.data
    assert "업비트 실현손익 기준".encode() in upbit.data
    assert "누적 매수액 (KRW)".encode() in upbit.data
    assert b"BITHUMB_ONLY" in bithumb.data
    assert b"UPBIT_ONLY" not in bithumb.data
    assert "빗썸 실현손익 기준".encode() in bithumb.data
    assert b"BACKPACK_ONLY" in invalid.data


def test_source_archive_can_trash_restore_and_permanently_delete_source(
    tmp_path: Path,
) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    evidence = tmp_path / "data" / "tax_evidence" / "raw"
    content = (
        "created_at,from_address,id,quantity,source,status,symbol,to_address,transaction_hash\n"
        "2026-08-23T14:11:29.792,from-wallet,1,12.5,Arbitrum,Confirmed,USDC,to-wallet,tx-in\n"
    ).encode()
    with TaxStore(database) as store:
        result = import_exchange_csv(
            store,
            evidence_directory=evidence,
            source_type="BACKPACK",
            document_type="DEPOSITS",
            original_filename="deposit.csv",
            content=content,
        )
        normalize_backpack_transfers(store)
        event_id = store.connection.execute(
            "SELECT id FROM normalized_events WHERE raw_record_id IS NOT NULL"
        ).fetchone()["id"]
        store.connection.execute(
            """
            INSERT INTO review_decisions (
                normalized_event_id, decision, rationale, decided_at_utc
            ) VALUES (?, 'TRANSFER', 'test', '2026-08-24T00:00:00+00:00')
            """,
            (event_id,),
        )
        store.connection.commit()
        original_path = Path(store.source_file(result.file_id)["stored_path"])

    client = app.test_client()
    assert client.post(f"/imports/{result.file_id}/delete").status_code == 400
    with client.session_transaction() as browser_session:
        browser_session["delete_csrf_token"] = "test-delete-token"
    response = client.post(
        f"/imports/{result.file_id}/delete",
        data={"csrf_token": "test-delete-token"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "원장을 휴지통으로 이동했습니다.".encode() in response.data
    assert not original_path.exists()
    trash_files = list((tmp_path / "data" / "tax_evidence" / "trash").iterdir())
    assert len(trash_files) == 1
    assert trash_files[0].read_bytes() == content
    with TaxStore(database) as store:
        assert store.source_file(result.file_id) is None
        assert store.source_file(result.file_id, include_deleted=True) is not None
        assert store.connection.execute("SELECT COUNT(1) FROM raw_records").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(1) FROM normalized_events").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(1) FROM review_decisions").fetchone()[0] == 0
        assert store.dashboard_statistics()["source_files"] == 0
        assert store.dashboard_statistics()["normalized_events"] == 0

    trash_page = client.get("/imports?view=trash")
    assert trash_page.status_code == 200
    assert b"deposit.csv" in trash_page.data
    assert "복구".encode() in trash_page.data
    assert "영구삭제".encode() in trash_page.data

    restored_response = client.post(
        f"/imports/{result.file_id}/restore",
        data={"csrf_token": "test-delete-token"},
        follow_redirects=True,
    )
    assert restored_response.status_code == 200
    assert "원장을 복구했습니다.".encode() in restored_response.data
    with TaxStore(database) as store:
        restored_source = store.source_file(result.file_id)
        assert restored_source is not None
        assert Path(restored_source["stored_path"]).read_bytes() == content
        assert store.dashboard_statistics()["source_files"] == 1
        assert store.dashboard_statistics()["normalized_events"] == 1
        assert store.connection.execute("SELECT COUNT(1) FROM review_decisions").fetchone()[0] == 0

    client.post(
        f"/imports/{result.file_id}/delete",
        data={"csrf_token": "test-delete-token"},
    )
    permanent_response = client.post(
        f"/imports/{result.file_id}/permanent-delete",
        data={"csrf_token": "test-delete-token"},
        follow_redirects=True,
    )
    assert permanent_response.status_code == 200
    assert "원장을 영구삭제했습니다.".encode() in permanent_response.data
    assert list((tmp_path / "data" / "tax_evidence" / "trash").iterdir()) == []
    with TaxStore(database) as store:
        assert store.source_file(result.file_id, include_deleted=True) is None
        assert store.connection.execute("SELECT COUNT(1) FROM raw_records").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(1) FROM normalized_events").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(1) FROM review_decisions").fetchone()[0] == 0


def test_backpack_deposit_csv_records_document_type(tmp_path: Path) -> None:
    content = (
        "Asset,Quantity,Source,Source Address,Initiated,Transaction,Status\n"
        "USDC,259.181374,Arbitrum,masked,2026-08-23,masked,Confirmed\n"
    ).encode("utf-8")

    with TaxStore(tmp_path / "tax.db") as store:
        result = import_exchange_csv(
            store,
            evidence_directory=tmp_path / "evidence",
            source_type="BACKPACK",
            document_type="DEPOSITS",
            original_filename="backpack-deposits.csv",
            content=content,
        )
        source = store.source_file(result.file_id)

    assert result.created is True
    assert result.document_type == "DEPOSITS"
    assert source["source_type"] == "BACKPACK"
    assert source["document_type"] == "DEPOSITS"


def test_backpack_transfers_are_normalized_once(tmp_path: Path) -> None:
    deposits = (
        "created_at,from_address,id,quantity,source,status,symbol,to_address,transaction_hash\n"
        "2026-08-23T14:11:29.792,from-wallet,1,12.5,Arbitrum,Confirmed,USDC,to-wallet,tx-in\n"
    ).encode()
    withdrawals = (
        "blockchain,created_at,fee,id,is_internal,quantity,status,subaccount_id,symbol,to_address,transaction_hash\n"
        "Tron,2026-08-22T01:02:03.004,0.2,2,false,5,confirmed,9,USDT,out-wallet,tx-out\n"
    ).encode()

    with TaxStore(tmp_path / "tax.db") as store:
        for document_type, content in (
            ("DEPOSITS", deposits),
            ("WITHDRAWALS", withdrawals),
        ):
            import_exchange_csv(
                store,
                evidence_directory=tmp_path / "evidence",
                source_type="BACKPACK",
                document_type=document_type,
                original_filename=f"{document_type.lower()}.csv",
                content=content,
            )
        first = normalize_backpack_transfers(store)
        second = normalize_backpack_transfers(store)
        events = store.list_normalized_events()

    assert first.created == 2
    assert second.created == 0
    assert events[0]["event_type"] == "DEPOSIT"
    assert events[0]["occurred_at_utc"] == "2026-08-23T14:11:29.792000+00:00"
    assert events[0]["network"] == "Arbitrum"
    assert events[1]["event_type"] == "WITHDRAWAL"
    assert events[1]["fee_quantity"] == "0.2"
    assert events[1]["is_internal"] == 0
    assert events[1]["to_address"] == "out-wallet"


def test_backpack_internal_deposit_is_classified_as_airdrop(tmp_path: Path) -> None:
    deposits = (
        "created_at,from_address,id,quantity,source,status,symbol,to_address,transaction_hash\n"
        "2026-03-23T05:31:06.601,,,12255,Internal,Confirmed,BP,,\n"
    ).encode()
    with TaxStore(tmp_path / "tax.db") as store:
        import_exchange_csv(
            store,
            evidence_directory=tmp_path / "evidence",
            source_type="BACKPACK",
            document_type="DEPOSITS",
            original_filename="internal-deposits.csv",
            content=deposits,
        )
        normalize_backpack_transfers(store)
        event = store.connection.execute(
            "SELECT * FROM normalized_events"
        ).fetchone()

    assert event["event_type"] == "AIRDROP"
    assert event["network"] == "Internal"
    assert event["instrument_type"] == "SPOT_CRYPTO"
    assert event["classification_status"] == "NEEDS_REVIEW"
    assert "에어드랍" in event["classification_reason"]


def test_existing_tax_database_is_migrated_for_backpack(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE source_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL CHECK(source_type IN ('UPBIT', 'BITHUMB')),
            original_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            imported_at_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'RAW_IMPORTED',
            notes TEXT,
            UNIQUE(source_type, sha256)
        );
        INSERT INTO source_files (
            source_type, original_filename, stored_path, sha256, byte_size,
            row_count, imported_at_utc
        ) VALUES ('UPBIT', 'old.csv', 'old.csv', 'abc', 1, 0, '2026-01-01');
        """
    )
    connection.close()

    with TaxStore(database) as store:
        row = store.source_file(1)
        violations = store.connection.execute("PRAGMA foreign_key_check").fetchall()

    assert row["document_type"] == "TRANSACTIONS"
    assert violations == []


def test_trades_and_closed_futures_are_separated(tmp_path: Path) -> None:
    trade = (
        "fee,fee_asset,is_bid,is_maker,order_id,price,quantity,subaccount_id,symbol,system_order_type,timestamp,trade_id\n"
        "0.01,USDC,true,false,o1,100,2,s1,CRCL.US_USDC_RFQ,USER,2026-08-01T00:00:00+00:00,t1\n"
    ).encode()
    future = (
        "account_leverage,closed_at,closing_price,cumulative_pnl_realized,entry_price,last_event_type,market_symbol,max_net_quantity,max_net_quantity_direction,opened_at,position_id,total_funding_quantity,total_interest,total_liquidation_fees,total_trading_fees\n"
        "2,2026-08-02T00:00:00,110,20,100,CLOSED,BTC_USDC_PERP,1,-1,2026-08-01T00:00:00,p1,-1,0,0,2\n"
    ).encode()

    with TaxStore(tmp_path / "tax.db") as store:
        for document_type, content in (("TRADES", trade), ("FUTURES", future)):
            import_exchange_csv(
                store,
                evidence_directory=tmp_path / "evidence",
                source_type="BACKPACK",
                document_type=document_type,
                original_filename=f"{document_type}.csv",
                content=content,
            )
        result = normalize_all_backpack(store)
        events = store.list_normalized_events()

    assert result.created == 2
    assert events[0]["event_type"] == "FUTURES_POSITION_CLOSED"
    assert events[0]["side"] == "SHORT"
    assert events[0]["realized_pnl"] == "20"
    assert events[1]["event_type"] == "TRADE_FILL"
    assert events[1]["instrument_type"] == "TOKENIZED_STOCK"
    assert events[1]["gross_amount"] == "200"
