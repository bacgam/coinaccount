from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore


def test_ledger_filters_and_detail_trace_original_row(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        event_id = int(store.connection.execute(
            """INSERT INTO normalized_events (
                   source_type, occurred_at_utc, event_type, asset, quantity,
                   txid, classification_status, created_at_utc
               ) VALUES ('BACKPACK', '2026-08-20T01:00:00Z', 'DEPOSIT',
                         'APT', '2', 'apt-tx-123', 'NEEDS_REVIEW', '2026-08-20')"""
        ).lastrowid)
        store.connection.execute(
            """INSERT INTO normalized_events (
                   source_type, occurred_at_utc, event_type, asset, quantity,
                   txid, classification_status, created_at_utc
               ) VALUES ('BACKPACK', '2026-07-01T01:00:00Z', 'DEPOSIT',
                         'BTC', '1', 'btc-tx', 'NORMALIZED', '2026-07-01')"""
        )
        store.connection.commit()

    client = app.test_client()
    result = client.get(
        "/ledger?exchange=BACKPACK&start=2026-08-01&end=2026-08-31"
        "&asset=APT&txid=tx-123&status=NEEDS_REVIEW"
    )
    assert result.status_code == 200
    assert b"APT" in result.data
    assert b"btc-tx" not in result.data
    detail = client.get(f"/ledger/{event_id}")
    assert detail.status_code == 200
    assert b"apt-tx-123" in detail.data
    assert "검토센터에서 처리".encode() in detail.data
