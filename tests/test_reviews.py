from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from backpack_tax.app import create_app, format_grouped_decimal
from backpack_tax.analytics import asset_performance
from backpack_tax.db import TaxStore
from backpack_tax.imports import import_exchange_csv
from backpack_tax.normalize import normalize_backpack_transfers


def _insert_review_events(database: Path) -> tuple[int, int, int, int]:
    with TaxStore(database) as store:
        cursor = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                network, txid, from_address, to_address,
                classification_status, classification_reason, created_at_utc
            ) VALUES (
                'BACKPACK', '2026-08-20T01:02:03Z', 'DEPOSIT', 'USDC', '12.5',
                'Arbitrum', 'tx-review', 'sender', 'receiver',
                'NEEDS_REVIEW', '본인 소유 계정 간 이동인지 확인해야 합니다.',
                '2026-08-20T01:02:03Z'
            )
            """
        )
        deposit_id = int(cursor.lastrowid)
        cursor = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                network, txid, classification_status, classification_reason,
                created_at_utc
            ) VALUES (
                'UPBIT', '2026-08-20T00:52:03Z', 'WITHDRAWAL', 'USDC', '12.7',
                'Arbitrum', 'tx-review', 'NEEDS_REVIEW',
                '상대 거래소 입금 여부를 확인해야 합니다.',
                '2026-08-20T00:52:03Z'
            )
            """
        )
        withdrawal_id = int(cursor.lastrowid)
        cursor = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                market_symbol, side, classification_status,
                classification_reason, created_at_utc
            ) VALUES (
                'UPBIT', '2026-08-21T01:02:03Z', 'TRADE_FILL', 'BTC', '0.1',
                'KRW-BTC', 'BUY', 'NEEDS_REVIEW', '거래 유형을 확인해야 합니다.',
                '2026-08-21T01:02:03Z'
            )
            """
        )
        trade_id = int(cursor.lastrowid)
        cursor = store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, classification_reason, created_at_utc
            ) VALUES (
                'BITHUMB', '2026-08-22T01:02:03Z', 'WITHDRAWAL', 'ETH', '1',
                'NORMALIZED', '자동 분류 완료', '2026-08-22T01:02:03Z'
            )
            """
        )
        normalized_id = int(cursor.lastrowid)
        store.connection.commit()
    return deposit_id, withdrawal_id, trade_id, normalized_id


def test_review_quantity_number_format_preserves_useful_decimals() -> None:
    assert format_grouped_decimal("12255") == "12,255"
    assert format_grouped_decimal("12345.67000000") == "12,345.67"
    assert format_grouped_decimal("0.00000100") == "0.000001"


def test_review_center_filters_records_decisions_and_reopens(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    deposit_id, withdrawal_id, trade_id, _ = _insert_review_events(database)
    client = app.test_client()

    page = client.get("/reviews")
    assert page.status_code == 200
    assert "전체 검토 일괄 승인".encode() not in page.data
    assert "스마트 검토 작업함".encode() in page.data
    assert b'name="collection" value="ALL"' not in page.data
    assert b'id="review-help-open"' in page.data
    assert b'id="review-help-dialog"' in page.data
    assert "미지원 거래소 이동과 취득가 주의".encode() in page.data
    assert "바이낸스 등 다른 해외거래소".encode() in page.data
    assert b"dialog.showModal()" in page.data
    assert "검토 센터".encode() in page.data
    assert "건너뛰고 다음".encode() in page.data
    assert b"coinaccount-review-advance" in page.data
    assert b"USDC" in page.data
    assert b"BTC" in page.data
    assert b"ETH" not in page.data
    assert "선택한 입출금 연결".encode() in page.data
    assert client.get("/reviews?source=UPBIT").data.count(
        b'class="panel review-card'
    ) == 2
    assert b"BTC" in client.get("/reviews?event_type=TRADE_FILL").data

    assert client.post(
        f"/reviews/{deposit_id}/decision",
        data={"decision": "INTERNAL_TRANSFER"},
    ).status_code == 400
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "review-test-token"

    response = client.post(
        f"/reviews/{deposit_id}/decision",
        data={
            "csrf_token": "review-test-token",
            "decision": "INTERNAL_TRANSFER",
            "rationale": "Backpack에서 업비트로 보낸 내 자산",
            "counterparty_event_id": str(withdrawal_id),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "내부 이동 처리했습니다.".encode() in response.data
    assert b"<strong>USDC</strong>" not in response.data
    with TaxStore(database) as store:
        event = store.connection.execute(
            "SELECT classification_status, is_internal FROM normalized_events WHERE id IN (?, ?) ORDER BY id",
            (deposit_id, withdrawal_id),
        ).fetchall()
        decision = store.connection.execute(
            "SELECT decision, rationale FROM review_decisions WHERE normalized_event_id = ?",
            (deposit_id,),
        ).fetchone()
        transfer_match = store.connection.execute(
            "SELECT * FROM transfer_matches WHERE status = 'ACTIVE'"
        ).fetchone()
        assert all(item["classification_status"] == "NORMALIZED" for item in event)
        assert all(item["is_internal"] == 1 for item in event)
        assert decision["decision"] == "INTERNAL_TRANSFER"
        assert "Backpack에서 업비트로 보낸 내 자산" in decision["rationale"]
        assert transfer_match["withdrawal_event_id"] == withdrawal_id
        assert transfer_match["deposit_event_id"] == deposit_id
        assert transfer_match["fee_quantity"] == "0.2"

    resolved = client.get("/reviews?status=RESOLVED")
    assert b"USDC" in resolved.data
    assert "판정 이력 1건".encode() in resolved.data

    reopened = client.post(
        f"/reviews/{deposit_id}/decision",
        data={
            "csrf_token": "review-test-token",
            "decision": "REOPENED",
            "return_status": "PENDING",
        },
        follow_redirects=True,
    )
    assert b"USDC" in reopened.data
    assert "판정 이력 2건".encode() in reopened.data
    with TaxStore(database) as store:
        events = store.connection.execute(
            "SELECT classification_status, is_internal FROM normalized_events WHERE id IN (?, ?)",
            (deposit_id, withdrawal_id),
        ).fetchall()
        active_matches = store.connection.execute(
            "SELECT COUNT(*) FROM transfer_matches WHERE status = 'ACTIVE'"
        ).fetchone()[0]
        assert all(item["classification_status"] == "NEEDS_REVIEW" for item in events)
        assert all(item["is_internal"] is None for item in events)
        assert active_matches == 0

    invalid = client.post(
        f"/reviews/{trade_id}/decision",
        data={
            "csrf_token": "review-test-token",
            "decision": "INTERNAL_TRANSFER",
        },
        follow_redirects=True,
    )
    assert "입금과 출금만 내부 이동으로 판정할 수 있습니다.".encode() in invalid.data


def test_review_center_excludes_event_and_preserves_history(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    _, _, trade_id, _ = _insert_review_events(database)
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "review-test-token"

    response = client.post(
        f"/reviews/{trade_id}/decision",
        data={
            "csrf_token": "review-test-token",
            "decision": "EXCLUDED",
            "rationale": "테스트 주문으로 확인",
            "return_status": "RESOLVED",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "계산 제외 처리했습니다.".encode() in response.data
    assert b"BTC" in response.data
    with TaxStore(database) as store:
        event = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?",
            (trade_id,),
        ).fetchone()
        decision_count = store.connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE normalized_event_id = ?",
            (trade_id,),
        ).fetchone()[0]
        assert event["classification_status"] == "EXCLUDED"
        assert decision_count == 1


def test_trashing_matched_source_reopens_counterpart(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    evidence = tmp_path / "data" / "tax_evidence" / "raw"
    content = (
        "created_at,from_address,id,quantity,source,status,symbol,to_address,transaction_hash\n"
        "2026-08-23T14:11:29.792,from-wallet,1,12.5,Arbitrum,Confirmed,USDC,to-wallet,tx-match\n"
    ).encode()
    with TaxStore(database) as store:
        imported = import_exchange_csv(
            store,
            evidence_directory=evidence,
            source_type="BACKPACK",
            document_type="DEPOSITS",
            original_filename="matched-deposit.csv",
            content=content,
        )
        normalize_backpack_transfers(store)
        deposit_id = int(
            store.connection.execute(
                "SELECT id FROM normalized_events WHERE event_type = 'DEPOSIT'"
            ).fetchone()["id"]
        )
        withdrawal_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    classification_status, classification_reason, created_at_utc
                ) VALUES (
                    'UPBIT', '2026-08-23T14:01:29Z', 'WITHDRAWAL', 'USDC', '12.7',
                    'NEEDS_REVIEW', '상대 입금을 확인해야 합니다.', '2026-08-23'
                )
                """
            ).lastrowid
        )
        store.connection.commit()
        store.record_review_decision(
            deposit_id,
            decision="INTERNAL_TRANSFER",
            rationale="내 거래소 사이의 이동",
            decided_at_utc="2026-08-24T00:00:00+00:00",
            counterparty_event_id=withdrawal_id,
        )

    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["delete_csrf_token"] = "delete-test-token"
    response = client.post(
        f"/imports/{imported.file_id}/delete",
        data={"csrf_token": "delete-test-token"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with TaxStore(database) as store:
        counterpart = store.connection.execute(
            "SELECT classification_status, is_internal FROM normalized_events WHERE id = ?",
            (withdrawal_id,),
        ).fetchone()
        latest_decision = store.connection.execute(
            """
            SELECT decision FROM review_decisions
            WHERE normalized_event_id = ? ORDER BY id DESC LIMIT 1
            """,
            (withdrawal_id,),
        ).fetchone()["decision"]
        assert counterpart["classification_status"] == "NEEDS_REVIEW"
        assert counterpart["is_internal"] is None
        assert latest_decision == "REOPENED"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM transfer_matches"
        ).fetchone()[0] == 0


def test_dust_review_collection_requires_stored_usd_valuation(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                quote_asset, gross_amount, classification_status,
                classification_reason, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'NEEDS_REVIEW', 'test', '2026-08-01')
            """,
            [
                ("BACKPACK", "2026-08-01", "TRADE_FILL", "DUSTUSD", "1", "USDC", "0.75"),
                ("BACKPACK", "2026-08-02", "DEPOSIT", "USDC", "0.5", None, None),
                ("UPBIT", "2026-08-03", "TRADE_FILL", "DUSTKRW", "1", "KRW", "1000"),
                ("BITHUMB", "2026-08-04", "TRADE_FILL", "NOTDUST", "1", "KRW", "2000"),
                ("BACKPACK", "2026-08-05", "TRADE_FILL", "ZERO", "1", "USDC", "0"),
                ("BITHUMB", "2026-08-06", "DEPOSIT", "USDT", "565.8", None, "565.8"),
                ("UPBIT", "2026-08-07", "DEPOSIT", "BTC", "0.00001", None, None),
            ],
        )
        store.connection.commit()
        store.seed_intrinsic_event_valuations()
        statistics = store.review_statistics()

    assert statistics["dust_pending"] == 2
    assert statistics["unpriced_pending"] == 1
    client = app.test_client()
    page = client.get("/reviews?dust=1")
    assert page.status_code == 200
    assert "더스트 트렌젝션".encode() in page.data
    assert "KRW는 원화 금액이 확정된 법정화폐".encode() in page.data
    assert b"DUSTUSD" in page.data
    assert b"DUSTKRW" not in page.data
    assert b"NOTDUST" not in page.data
    assert b">ZERO<" not in page.data
    assert b"565.8" not in page.data
    assert "더스트 트렌젝션 <span>2</span>".encode() in page.data
    assert "가격 미확인 <span>1</span>".encode() in page.data
    assert "검토 대기 일괄 승인".encode() in page.data

    unpriced_page = client.get("/reviews?unpriced=1")
    assert unpriced_page.status_code == 200
    assert "거래 시점 가격 채우기".encode() in unpriced_page.data
    assert "가격 미확인 일괄 승인".encode() in unpriced_page.data
    assert b'name="collection" value="UNPRICED"' in unpriced_page.data
    assert "거래시점 가격을 채우고 있습니다".encode() in unpriced_page.data
    assert "요청 제한 발생 시 자동으로 재시도합니다".encode() in unpriced_page.data
    assert b'id="valuation-loading"' in unpriced_page.data
    assert b"requestAnimationFrame" in unpriced_page.data
    assert b"BTC" in unpriced_page.data
    assert b"DUSTKRW" not in unpriced_page.data
    assert b"NOTDUST" not in unpriced_page.data
    assert b"DUSTUSD" not in unpriced_page.data

    assert client.post(
        "/reviews/dust/approve",
        data={"source": "BACKPACK"},
    ).status_code == 400
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "dust-bulk-token"
    approved = client.post(
        "/reviews/dust/approve",
        data={
            "csrf_token": "dust-bulk-token",
            "source": "BACKPACK",
        },
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert "더스트 트렌젝션 2건을 일괄 승인했습니다.".encode() in approved.data
    assert "더스트 트렌젝션 <span>0</span>".encode() in approved.data
    with TaxStore(database) as store:
        statuses = {
            row["asset"]: row["classification_status"]
            for row in store.connection.execute(
                "SELECT asset, classification_status FROM normalized_events"
            ).fetchall()
        }
        decisions = store.connection.execute(
            "SELECT decision, rationale FROM review_decisions ORDER BY id"
        ).fetchall()
    assert statuses["DUSTUSD"] == "NORMALIZED"
    assert statuses["USDC"] == "NORMALIZED"
    assert statuses["DUSTKRW"] == "NEEDS_REVIEW"
    assert statuses["NOTDUST"] == "NEEDS_REVIEW"
    assert len(decisions) == 2
    assert all(item["decision"] == "CONFIRMED" for item in decisions)
    assert all("일괄 정규화 승인" in item["rationale"] for item in decisions)

    css = client.get("/static/backpack.css")
    assert b".review-actions button" in css.data
    assert b"border: 1px solid var(--green)" in css.data
    assert b"background: #fff; color: var(--ink)" in css.data
    assert b".valuation-loading-box" in css.data
    assert b"border: 2px solid var(--green)" in css.data
    assert b"@keyframes valuation-spin" in css.data
    assert b".review-help-trigger" in css.data
    assert b".review-help-dialog::backdrop" in css.data


def test_all_and_unpriced_bulk_approval_respect_collection_scope(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        store.connection.executemany(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, classification_reason, created_at_utc
            ) VALUES (
                'UPBIT', '2026-08-01T00:00:00+00:00', 'DEPOSIT', ?, ?,
                'NEEDS_REVIEW', 'test', '2026-08-01'
            )
            """,
            [("BTC", "0.001"), ("USDC", "12")],
        )
        store.connection.commit()
        store.seed_intrinsic_event_valuations()

    client = app.test_client()
    assert client.post(
        "/reviews/bulk/approve", data={"collection": "UNPRICED"}
    ).status_code == 400
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "bulk-review-token"

    unpriced = client.post(
        "/reviews/bulk/approve",
        data={"csrf_token": "bulk-review-token", "collection": "UNPRICED"},
        follow_redirects=True,
    )
    assert unpriced.status_code == 200
    assert "가격 미확인의 검토 대기 1건을 일괄 승인했습니다.".encode() in unpriced.data
    with TaxStore(database) as store:
        statuses = {
            row["asset"]: row["classification_status"]
            for row in store.connection.execute(
                "SELECT asset, classification_status FROM normalized_events"
            )
        }
    assert statuses == {"BTC": "NORMALIZED", "USDC": "NEEDS_REVIEW"}

    approved_all = client.post(
        "/reviews/bulk/approve",
        data={"csrf_token": "bulk-review-token", "collection": "ALL"},
        follow_redirects=True,
    )
    assert approved_all.status_code == 200
    assert "전체 검토의 검토 대기 1건을 일괄 승인했습니다.".encode() in approved_all.data
    with TaxStore(database) as store:
        statuses = {
            row["asset"]: row["classification_status"]
            for row in store.connection.execute(
                "SELECT asset, classification_status FROM normalized_events"
            )
        }
        decisions = store.connection.execute(
            "SELECT decision, rationale FROM review_decisions ORDER BY id"
        ).fetchall()
    assert statuses == {"BTC": "NORMALIZED", "USDC": "NORMALIZED"}
    assert [row["decision"] for row in decisions] == ["CONFIRMED", "CONFIRMED"]
    assert "가격 미확인 모음" in decisions[0]["rationale"]
    assert "전체 검토 모음" in decisions[1]["rationale"]


def test_unpriced_resolved_summary_matches_the_filtered_event_list(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_ids: dict[str, int] = {}
        for asset in ("UNPRICED_DONE", "KNOWN_DONE"):
            event_ids[asset] = int(
                store.connection.execute(
                    """
                    INSERT INTO normalized_events (
                        source_type, occurred_at_utc, event_type, asset, quantity,
                        classification_status, classification_reason, created_at_utc
                    ) VALUES (
                        'BACKPACK', '2026-08-01T00:00:00+00:00', 'DEPOSIT', ?, '1',
                        'NEEDS_REVIEW', 'test', '2026-08-01T00:00:00+00:00'
                    )
                    """,
                    (asset,),
                ).lastrowid
            )
        store.connection.commit()
        store.upsert_event_valuation(
            event_id=event_ids["UNPRICED_DONE"],
            valuation_status="UNPRICED",
            usd_value=None,
            unit_price_usd=None,
            valuation_method="PRICE_UNAVAILABLE",
            confidence="NONE",
            reason="test",
            valued_at_utc="2026-08-01T00:00:00+00:00",
        )
        store.upsert_event_valuation(
            event_id=event_ids["KNOWN_DONE"],
            valuation_status="VALUED",
            usd_value=Decimal("10"),
            unit_price_usd=Decimal("10"),
            valuation_method="TEST",
            confidence="HIGH",
            reason="test",
            valued_at_utc="2026-08-01T00:00:00+00:00",
        )
        for event_id in event_ids.values():
            store.record_review_decision(
                event_id,
                decision="CONFIRMED",
                rationale="test",
                decided_at_utc="2026-08-01T01:00:00+00:00",
            )
        statistics = store.review_statistics()
        events, total = store.list_review_events(
            status_filter="RESOLVED",
            unpriced_only=True,
        )

    assert statistics["resolved"] == 2
    assert statistics["unpriced_resolved"] == 1
    assert total == 1
    assert [event["asset"] for event in events] == ["UNPRICED_DONE"]

    page = app.test_client().get("/reviews?status=RESOLVED&unpriced=1")
    assert page.status_code == 200
    assert b"UNPRICED_DONE" in page.data
    assert b"KNOWN_DONE" not in page.data
    assert "1개 이벤트".encode() in page.data


def test_external_purchase_deposit_creates_audited_basis_and_recalculates_pnl(
    tmp_path: Path,
) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        deposit_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-02T00:00:00Z', 'DEPOSIT', 'EXT', '10',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', 'test', '2026-01-02'
                )
                """
            ).lastrowid
        )
        withdrawal_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'UPBIT', '2026-01-01T23:00:00Z', 'WITHDRAWAL', 'EXT', '10',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', 'test', '2026-01-01'
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
                'BACKPACK', '2026-01-03T00:00:00Z', 'TRADE_FILL', 'EXT',
                'SPOT_CRYPTO', 'SELL', '5', '4', '20', 'USDC', '0',
                'NORMALIZED', '2026-01-03'
            )
            """
        )
        store.connection.commit()

    client = app.test_client()
    page = client.get("/reviews?status=PENDING&unpriced=1")
    assert page.status_code == 200
    for label in (
        "외부 거래소에서 매수 후 입금",
        "내 지갑·거래소 간 이동",
        "에어드랍 분류·보상",
        "기초잔고 또는 거래자료 누락",
    ):
        assert label.encode() in page.data

    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "deposit-origin-token"
    response = client.post(
        f"/reviews/{deposit_id}/deposit-origin",
        data={
            "csrf_token": "deposit-origin-token",
            "origin_type": "EXTERNAL_PURCHASE",
            "origin_name": "Binance",
            "acquired_at_utc": "2026-01-01T12:00",
            "unit_cost": "2",
            "fee_cost": "1",
            "origin_note": "거래내역 확인",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "총 취득가 21 USD를 PNL 원가에 반영".encode() in response.data

    with TaxStore(database) as store:
        event = store.connection.execute(
            "SELECT classification_status, is_internal FROM normalized_events WHERE id = ?",
            (deposit_id,),
        ).fetchone()
        acquisition = store.connection.execute(
            """
            SELECT origin_type, origin_name, acquired_at_utc, quantity,
                   unit_cost, fee_cost, total_cost, currency
            FROM deposit_acquisitions WHERE normalized_event_id = ?
            """,
            (deposit_id,),
        ).fetchone()
        decision = store.connection.execute(
            "SELECT decision FROM review_decisions WHERE normalized_event_id = ?",
            (deposit_id,),
        ).fetchone()["decision"]
        row = asset_performance(store, source_type="BACKPACK")[0]

    assert event["classification_status"] == "NORMALIZED"
    assert event["is_internal"] == 0
    assert dict(acquisition) == {
        "origin_type": "EXTERNAL_PURCHASE",
        "origin_name": "Binance",
        "acquired_at_utc": "2026-01-01T12:00:00+00:00",
        "quantity": "10",
        "unit_cost": "2",
        "fee_cost": "1",
        "total_cost": "21",
        "currency": "USD",
    }
    assert decision == "EXTERNAL_PURCHASE"
    assert row["external_acquisition_quantity"] == Decimal("10")
    assert row["external_acquisition_cost"] == Decimal("21")
    assert row["realized_cost"] == Decimal("10.5")
    assert row["pnl"] == Decimal("9.5")
    assert row["has_unknown_basis"] is False

    with TaxStore(database) as store:
        store.record_review_decision(
            deposit_id,
            decision="REOPENED",
            rationale="내부 이동으로 수정",
            decided_at_utc="2026-01-04T00:00:00+00:00",
        )
        store.record_review_decision(
            deposit_id,
            decision="INTERNAL_TRANSFER",
            rationale="내 출금 확인",
            decided_at_utc="2026-01-04T00:01:00+00:00",
            counterparty_event_id=withdrawal_id,
        )
        revised = asset_performance(store, source_type="BACKPACK")[0]

    assert revised["external_acquisition_quantity"] == Decimal("0")
    assert revised["unknown_sell_quantity"] == Decimal("5")


def test_personal_wallet_deposit_can_be_approved_without_counterparty(
    tmp_path: Path,
) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-02-01T00:00:00Z', 'DEPOSIT', 'WALLET', '3',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', 'test', '2026-02-01'
                )
                """
            ).lastrowid
        )
        store.connection.commit()

    client = app.test_client()
    page = client.get("/reviews?status=PENDING")
    assert page.status_code == 200
    assert "연결할 입출금 기록이 있음".encode() in page.data
    assert "개인 지갑 이동 · 상대 기록 없음".encode() in page.data
    assert "취득가 미확인으로 승인".encode() in page.data

    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "personal-wallet-token"
    response = client.post(
        f"/reviews/{event_id}/deposit-origin",
        data={
            "csrf_token": "personal-wallet-token",
            "origin_type": "INTERNAL_TRANSFER",
            "internal_mode": "PERSONAL_WALLET",
            "wallet_name": "MetaMask 메인",
            "wallet_address": "0xAbC",
            "wallet_basis_mode": "UNKNOWN",
            "origin_note": "개인 지갑에서 입금",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "상대 기록 없는 개인 지갑 이동으로 승인".encode() in response.data

    with TaxStore(database) as store:
        event = store.connection.execute(
            "SELECT classification_status, is_internal FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        wallet_transfer = store.connection.execute(
            """
            SELECT wallet_key, wallet_name, wallet_address, direction, basis_mode
            FROM personal_wallet_transfers WHERE normalized_event_id = ?
            """,
            (event_id,),
        ).fetchone()
        decision = store.connection.execute(
            "SELECT decision FROM review_decisions WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()["decision"]

    assert event["classification_status"] == "NORMALIZED"
    assert event["is_internal"] == 1
    assert dict(wallet_transfer) == {
        "wallet_key": "ADDRESS:0xabc",
        "wallet_name": "MetaMask 메인",
        "wallet_address": "0xAbC",
        "direction": "IN",
        "basis_mode": "UNKNOWN",
    }
    assert decision == "PERSONAL_WALLET_TRANSFER"


def test_manual_airdrop_price_is_audited_and_approved_as_basis(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    network, instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-03-23T05:31:06+00:00', 'AIRDROP',
                    'BP', '12255', 'Internal', 'SPOT_CRYPTO', 'NEEDS_REVIEW',
                    '에어드랍 취득가를 확인해야 합니다.', '2026-03-23'
                )
                """
            ).lastrowid
        )
        store.connection.commit()
        store.upsert_event_valuation(
            event_id=event_id,
            valuation_status="UNPRICED",
            usd_value=None,
            unit_price_usd=None,
            valuation_method="PRICE_UNAVAILABLE",
            confidence="NONE",
            reason="자동 가격 없음",
            valued_at_utc="2026-03-23T05:31:06+00:00",
        )
        assert store.approve_review_events(
            collection="UNPRICED",
            decided_at_utc="2026-03-23T06:00:00+00:00",
        ) == 0

    client = app.test_client()
    page = client.get("/reviews?status=PENDING&unpriced=1")
    assert "에어드랍 취득가 직접 입력".encode() in page.data
    assert b"12,255" in page.data
    assert "취득가 저장 및 승인".encode() in page.data
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "manual-price-token"
    response = client.post(
        f"/reviews/{event_id}/manual-price",
        data={
            "csrf_token": "manual-price-token",
            "unit_price": "0.25",
            "price_note": "프로젝트 배포 자료 확인",
            "return_unpriced": "1",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "PNL 원가에 반영했습니다".encode() in response.data
    with TaxStore(database) as store:
        event = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        manual = store.connection.execute(
            "SELECT * FROM manual_price_entries WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        decision = store.connection.execute(
            "SELECT decision FROM review_decisions WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()

    assert event["classification_status"] == "NORMALIZED"
    assert valuation["valuation_method"] == "USER_MANUAL_UNIT_PRICE"
    assert valuation["native_value"] == "3063.75"
    assert valuation["native_currency"] == "USD"
    assert valuation["usd_value"] == "3063.75"
    assert manual["unit_price"] == "0.25"
    assert manual["currency"] == "USD"
    assert "프로젝트 배포" in manual["note"]
    assert decision["decision"] == "AIRDROP_BASIS"


def test_manual_domestic_price_uses_krw_without_approving_event(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    classification_status, classification_reason, created_at_utc
                ) VALUES (
                    'UPBIT', '2026-01-01', 'DEPOSIT', 'TEST', '2',
                    'NEEDS_REVIEW', 'test', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.commit()
        event = store.set_manual_event_price(
            event_id,
            unit_price="1,250",
            note="국내 거래소 원화 가격",
            entered_at_utc="2026-01-02T00:00:00+00:00",
        )
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        status = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()["classification_status"]

    assert event["manual_currency"] == "KRW"
    assert event["airdrop_confirmed"] is False
    assert valuation["native_value"] == "2500"
    assert valuation["native_currency"] == "KRW"
    assert valuation["usd_value"] is None
    assert status == "NEEDS_REVIEW"


def test_zero_cost_is_allowed_only_for_airdrop(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        airdrop_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01', 'AIRDROP', 'SPCX.US', '0.12',
                    'TOKENIZED_STOCK', 'NEEDS_REVIEW', '2026-01-01'
                )
                """
            ).lastrowid
        )
        deposit_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01', 'DEPOSIT', 'BTC', '1',
                    'NEEDS_REVIEW', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.commit()
        result = store.set_manual_event_price(
            airdrop_id,
            unit_price="0",
            note="무상 취득",
            entered_at_utc="2026-01-02T00:00:00+00:00",
        )
        valuation = store.connection.execute(
            "SELECT * FROM event_valuations WHERE normalized_event_id = ?",
            (airdrop_id,),
        ).fetchone()
        try:
            store.set_manual_event_price(
                deposit_id,
                unit_price="0",
                note="",
                entered_at_utc="2026-01-02T00:00:00+00:00",
            )
        except ValueError as exc:
            regular_error = str(exc)
        else:
            regular_error = ""

    assert result["airdrop_confirmed"] is True
    assert valuation["native_value"] == "0.00"
    assert valuation["usd_value"] == "0.00"
    assert valuation["unit_price_usd"] == "0"
    assert "일반 이벤트 가격은 0보다 커야" in regular_error


def test_looked_up_airdrop_price_can_be_confirmed_as_basis(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    network, instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01', 'AIRDROP', 'TEST', '10',
                    'Internal', 'SPOT_CRYPTO', 'NEEDS_REVIEW', 'test', '2026-01-01'
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
            valuation_method="COINGECKO_USD_HOURLY_FALLBACK",
            confidence="MEDIUM",
            reason="CoinGecko 대체 가격",
            valued_at_utc="2026-01-01",
        )

    client = app.test_client()
    assert "정규화 승인 · 취득가 반영".encode() in client.get("/reviews").data
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "airdrop-basis-token"
    response = client.post(
        f"/reviews/{event_id}/airdrop-basis",
        data={"csrf_token": "airdrop-basis-token"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "PNL 원가에 반영했습니다".encode() in response.data
    with TaxStore(database) as store:
        status = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()["classification_status"]
        decision = store.connection.execute(
            "SELECT decision FROM review_decisions WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()["decision"]
    assert status == "NORMALIZED"
    assert decision == "AIRDROP_BASIS"


def test_stablecoin_airdrop_repairs_legacy_bulk_approval_and_native_usd_basis(
    tmp_path: Path,
) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    network, instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-08-10T10:53:09Z', 'AIRDROP', 'USDC', '0.115664',
                    'Internal', 'SPOT_CRYPTO', 'NEEDS_REVIEW',
                    '취득가 확인 필요', '2026-08-10'
                )
                """
            ).lastrowid
        )
        store.connection.execute(
            """
            INSERT INTO event_valuations (
                normalized_event_id, valuation_status, usd_value,
                unit_price_usd, native_value, native_currency,
                valuation_method, confidence, reason, valued_at_utc,
                pricing_version, calculated_at_utc
            ) VALUES (
                ?, 'VALUED', '0.115664', '1', NULL, NULL,
                'STABLECOIN_PARITY', 'HIGH', 'legacy', '2026-08-10',
                'HISTORICAL_V1', '2026-08-10'
            )
            """,
            (event_id,),
        )
        store.connection.execute(
            """
            INSERT INTO review_decisions (
                normalized_event_id, decision, rationale, decided_at_utc
            ) VALUES (?, 'CONFIRMED', '더스트 일괄 승인', '2026-08-26')
            """,
            (event_id,),
        )
        store.connection.commit()

        assert store.seed_intrinsic_event_valuations() == 1
        event = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        valuation = store.connection.execute(
            """
            SELECT native_value, native_currency
            FROM event_valuations WHERE normalized_event_id = ?
            """,
            (event_id,),
        ).fetchone()
        latest = store.connection.execute(
            """
            SELECT decision FROM review_decisions
            WHERE normalized_event_id = ? ORDER BY id DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()["decision"]

    assert event["classification_status"] == "NORMALIZED"
    assert valuation["native_value"] == "0.115664"
    assert valuation["native_currency"] == "USD"
    assert latest == "AIRDROP_BASIS"


def test_bulk_review_approves_valued_airdrop_basis(tmp_path: Path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status, created_at_utc
                ) VALUES (
                    'BACKPACK', '2026-01-01', 'AIRDROP', 'GIFT', '2',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.commit()
        store.upsert_event_valuation(
            event_id=event_id,
            valuation_status="VALUED",
            usd_value=Decimal("6"),
            unit_price_usd=Decimal("3"),
            native_value=Decimal("6"),
            native_currency="USD",
            valuation_method="TEST",
            confidence="HIGH",
            reason="test",
            valued_at_utc="2026-01-01",
        )

        assert store.approve_review_events(
            collection="ALL",
            decided_at_utc="2026-01-02T00:00:00+00:00",
        ) == 1
        status = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()["classification_status"]
        decision = store.connection.execute(
            "SELECT decision FROM review_decisions WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()["decision"]

    assert status == "NORMALIZED"
    assert decision == "AIRDROP_BASIS"


def test_unpriced_upbit_deposit_can_be_reclassified_as_zero_cost_airdrop(
    tmp_path: Path,
) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_id = int(
            store.connection.execute(
                """
                INSERT INTO normalized_events (
                    source_type, occurred_at_utc, event_type, asset, quantity,
                    instrument_type, classification_status,
                    classification_reason, created_at_utc
                ) VALUES (
                    'UPBIT', '2026-01-01T00:00:00Z', 'DEPOSIT', 'GIFT', '1',
                    'SPOT_CRYPTO', 'NEEDS_REVIEW', '가격 미확인 입금', '2026-01-01'
                )
                """
            ).lastrowid
        )
        store.connection.commit()

    client = app.test_client()
    page = client.get("/reviews?status=PENDING&unpriced=1")
    assert "에어드랍 분류".encode() in page.data
    with client.session_transaction() as browser_session:
        browser_session["review_csrf_token"] = "upbit-airdrop-token"
    classified = client.post(
        f"/reviews/{event_id}/airdrop-classification",
        data={
            "csrf_token": "upbit-airdrop-token",
            "rationale": "업비트 이벤트 무상 지급",
            "return_unpriced": "1",
        },
        follow_redirects=True,
    )
    assert classified.status_code == 200
    assert "이제 KRW 취득가를 입력".encode() in classified.data
    assert "에어드랍 취득가 직접 입력".encode() in classified.data

    approved = client.post(
        f"/reviews/{event_id}/manual-price",
        data={
            "csrf_token": "upbit-airdrop-token",
            "unit_price": "0",
            "price_note": "무상 취득",
        },
        follow_redirects=True,
    )
    assert approved.status_code == 200
    with TaxStore(database) as store:
        event = store.connection.execute(
            "SELECT event_type, classification_status FROM normalized_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        valuation = store.connection.execute(
            "SELECT native_value, native_currency FROM event_valuations WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        decisions = store.connection.execute(
            "SELECT decision FROM review_decisions WHERE normalized_event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset,
                instrument_type, side, quantity, unit_price, gross_amount,
                fee_asset, fee_quantity, classification_status, created_at_utc
            ) VALUES (
                'UPBIT', '2026-01-02T00:00:00Z', 'TRADE_FILL', 'GIFT',
                'SPOT_CRYPTO', 'SELL', '1', '1000', '1000', 'KRW', '0',
                'NORMALIZED', '2026-01-02'
            )
            """
        )
        store.connection.commit()
        performance = asset_performance(store, source_type="UPBIT")[0]

    assert event["event_type"] == "AIRDROP"
    assert event["classification_status"] == "NORMALIZED"
    assert valuation["native_value"] == "0"
    assert valuation["native_currency"] == "KRW"
    assert [row["decision"] for row in decisions] == [
        "AIRDROP_CLASSIFIED",
        "AIRDROP_BASIS",
    ]
    assert performance["pnl"] == Decimal("1000")
    assert performance["has_unknown_basis"] is False
