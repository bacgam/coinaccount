import io
import re
from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore
from backpack_tax.import_preview import analyze_records
from backpack_tax.imports import import_exchange_csv


def _preview_and_confirm(client, content: bytes, filename: str):
    preview = client.post(
        "/imports",
        data={
            "source_type": "BACKPACK",
            "document_type": "BALANCES",
            "source_file": (io.BytesIO(content), filename),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    path = re.search(rb'action="(/imports/preview/[^\"]+/confirm)"', preview.data)
    assert path
    with client.session_transaction() as session:
        csrf = session["delete_csrf_token"]
    confirmed = client.post(
        path.group(1).decode(), data={"csrf_token": csrf}, follow_redirects=True
    )
    assert confirmed.status_code == 200
    return preview, confirmed


def test_repeated_full_period_upload_imports_only_new_rows(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()
    first = b"time,market,side\n2026-01-01,BTC,bid\n"
    second = b"time,market,side\n2026-01-01,BTC,bid\n2026-01-02,ETH,bid\n"

    _preview_and_confirm(client, first, "first.csv")
    preview, _confirmed = _preview_and_confirm(client, second, "second.csv")
    assert "신규".encode() in preview.data
    assert "중복".encode() in preview.data
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        assert store.connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 2
        counts = store.connection.execute(
            "SELECT row_count FROM source_files ORDER BY id"
        ).fetchall()
        assert [row[0] for row in counts] == [1, 1]


def test_preview_marks_same_trade_id_with_changed_content_as_conflict(tmp_path: Path) -> None:
    database = tmp_path / "data" / "tax_ledger.db"
    evidence = tmp_path / "data" / "evidence"
    with TaxStore(database) as store:
        import_exchange_csv(
            store,
            evidence_directory=evidence,
            source_type="BACKPACK",
            document_type="TRADES",
            original_filename="first.csv",
            content=b"orderId,price,quantity\nabc,10,1\n",
        )
        analysis = analyze_records(
            store,
            source_type="BACKPACK",
            records=[{"orderId": "abc", "price": "11", "quantity": "1"}],
            first_row_number=2,
        )
    assert analysis.new == 0
    assert analysis.conflict == 1
    assert analysis.conflict_rows == (2,)


def test_confirmed_conflict_is_saved_and_can_be_kept(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()
    _preview_and_confirm(client, b"orderId,price,quantity\nabc,10,1\n", "first.csv")
    preview = client.post(
        "/imports",
        data={
            "source_type": "BACKPACK",
            "document_type": "TRADES",
            "source_file": (io.BytesIO(b"orderId,price,quantity\nabc,11,1\n"), "changed.csv"),
        },
        content_type="multipart/form-data",
    )
    path = re.search(rb'action="(/imports/preview/[^\"]+/confirm)"', preview.data)
    assert path
    with client.session_transaction() as session:
        csrf = session["delete_csrf_token"]
    client.post(path.group(1).decode(), data={"csrf_token": csrf})
    inbox = client.get("/imports/conflicts")
    assert inbox.status_code == 200
    assert "abc".encode() in inbox.data
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        conflict_id = store.connection.execute("SELECT id FROM import_conflicts").fetchone()[0]
        assert store.connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1
    client.post(
        f"/imports/conflicts/{conflict_id}/resolve",
        data={"csrf_token": csrf, "action": "KEEP_EXISTING", "note": "기존 자료 확인"},
    )
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        row = store.connection.execute("SELECT status, resolution_note FROM import_conflicts").fetchone()
        assert tuple(row) == ("KEEP_EXISTING", "기존 자료 확인")
        assert store.connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1


def test_conflict_can_be_added_as_separate_transaction(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()
    _preview_and_confirm(client, b"orderId,price,quantity\nabc,10,1\n", "first.csv")
    preview = client.post(
        "/imports",
        data={
            "source_type": "BACKPACK", "document_type": "TRADES",
            "source_file": (io.BytesIO(b"orderId,price,quantity\nabc,11,1\n"), "changed.csv"),
        }, content_type="multipart/form-data",
    )
    path = re.search(rb'action="(/imports/preview/[^\"]+/confirm)"', preview.data)
    with client.session_transaction() as session:
        csrf = session["delete_csrf_token"]
    client.post(path.group(1).decode(), data={"csrf_token": csrf})
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        conflict_id = store.connection.execute("SELECT id FROM import_conflicts").fetchone()[0]
    response = client.post(
        f"/imports/conflicts/{conflict_id}/resolve",
        data={"csrf_token": csrf, "action": "ADD_SEPARATE"},
    )
    assert response.status_code == 302
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        assert store.connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 2
        assert store.connection.execute("SELECT status FROM import_conflicts").fetchone()[0] == "ADD_SEPARATE"
