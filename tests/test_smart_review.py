from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore
from backpack_tax.smart_review import apply_smart_review_rule, smart_review_inbox


def _insert_pending_trade(store: TaxStore, asset: str = "SOL") -> int:
    cursor = store.connection.execute(
        """INSERT INTO normalized_events (
            source_type, occurred_at_utc, event_type, asset, side, quantity,
            gross_amount, classification_status, classification_reason, created_at_utc
        ) VALUES ('BACKPACK', '2026-08-01T00:00:00+00:00', 'TRADE_FILL', ?,
                  'BUY', '1', '100', 'NEEDS_REVIEW', '원본 체결 거래',
                  '2026-08-01T00:00:00+00:00')""",
        (asset,),
    )
    store.connection.commit()
    return int(cursor.lastrowid)


def test_smart_inbox_groups_and_applies_trade_fills(tmp_path: Path) -> None:
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        ids = [_insert_pending_trade(store, "SOL"), _insert_pending_trade(store, "BTC")]
        inbox = smart_review_inbox(store)
        trade = next(group for group in inbox["groups"] if group["key"] == "TRADE_FILL")
        assert trade["count"] == 2
        assert trade["actionable"] is True
        assert apply_smart_review_rule(store, "TRADE_FILL") == 2
        rows = store.connection.execute(
            "SELECT id, classification_status FROM normalized_events WHERE id IN (?, ?) ORDER BY id",
            ids,
        ).fetchall()
        assert [tuple(row) for row in rows] == [(ids[0], "NORMALIZED"), (ids[1], "NORMALIZED")]
        decisions = store.connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE decision = 'CONFIRMED'"
        ).fetchone()[0]
        assert decisions == 2


def test_smart_review_workbox_renders_and_route_requires_confirmation(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_id = _insert_pending_trade(store)
    client = app.test_client()
    page = client.get("/reviews")
    assert page.status_code == 200
    assert "스마트 검토 작업함".encode() in page.data
    assert "체결 거래 자동 정규화".encode() in page.data
    with client.session_transaction() as session:
        csrf = session["review_csrf_token"]
    response = client.post(
        "/reviews/smart/TRADE_FILL/apply",
        data={"csrf_token": csrf},
    )
    assert response.status_code == 303
    with TaxStore(database) as store:
        status = store.connection.execute(
            "SELECT classification_status FROM normalized_events WHERE id = ?", (event_id,)
        ).fetchone()[0]
        assert status == "NORMALIZED"


def test_smart_inbox_separates_airdrops_that_still_need_basis_review(tmp_path: Path) -> None:
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        store.connection.execute(
            """INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, created_at_utc
            ) VALUES ('BACKPACK', '2026-08-01T00:00:00+00:00', 'AIRDROP',
                      'UNKNOWN', '3', 'NEEDS_REVIEW', '2026-08-01T00:00:00+00:00')"""
        )
        store.connection.commit()
        inbox = smart_review_inbox(store)
    group = next(group for group in inbox["groups"] if group["key"] == "AIRDROP_REVIEW")
    assert group["count"] == 1
    assert group["actionable"] is False
    assert group["event_type"] == "AIRDROP"
