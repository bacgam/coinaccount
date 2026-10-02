from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore


def test_basis_gap_page_groups_unknown_sale_and_links_to_reviews(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        store.connection.execute(
            """
            INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, instrument_type,
                side, quantity, unit_price, gross_amount, classification_status,
                created_at_utc
            ) VALUES (
                'BACKPACK', '2026-08-01', 'TRADE_FILL', 'SOL', 'SPOT_CRYPTO',
                'SELL', '2', '100', '200', 'NORMALIZED', '2026-08-01'
            )
            """
        )
        store.connection.commit()

    page = app.test_client().get("/reviews/basis-gaps")
    assert page.status_code == 200
    assert b"SOL" in page.data
    assert "검토센터에서 해결".encode() in page.data
    assert b"source=BACKPACK" in page.data
