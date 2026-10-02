from pathlib import Path

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore


def test_personal_wallet_account_create_list_and_delete(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()
    page = client.get("/accounts")
    assert page.status_code == 200
    assert "개인 지갑 등록".encode() in page.data
    with client.session_transaction() as session:
        csrf = session["delete_csrf_token"]
    response = client.post(
        "/accounts",
        data={
            "csrf_token": csrf,
            "wallet_name": "MetaMask 메인",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "network_name": "EVM",
            "note": "개인 보관",
        },
    )
    assert response.status_code == 303
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        account = store.connection.execute("SELECT * FROM personal_wallet_accounts").fetchone()
        assert account["network_family"] == "EVM"
        assert account["network_name"] == "EVM"
        account_id = int(account["id"])
    page = client.get("/accounts")
    assert "MetaMask 메인".encode() in page.data
    assert b"0x1111111111111111111111111111111111111111" in page.data
    assert b'data-copy-address="0x1111111111111111111111111111111111111111"' in page.data
    response = client.post(
        f"/accounts/{account_id}/delete", data={"csrf_token": csrf}
    )
    assert response.status_code == 303
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        assert store.connection.execute("SELECT COUNT(*) FROM personal_wallet_accounts").fetchone()[0] == 0


def test_exchange_wallet_account_create_list_and_delete(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()
    page = client.get("/accounts")
    assert page.status_code == 200
    assert "거래소 지갑 등록".encode() in page.data
    assert "Binance".encode() in page.data
    with client.session_transaction() as session:
        csrf = session["delete_csrf_token"]

    response = client.post(
        "/accounts/exchange",
        data={
            "csrf_token": csrf,
            "exchange_code": "UPBIT",
            "network_name": "EVM",
            "wallet_address": "0x2222222222222222222222222222222222222222",
            "note": "ETH 입금 주소",
        },
    )
    assert response.status_code == 303
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        account = store.connection.execute(
            "SELECT * FROM exchange_wallet_accounts"
        ).fetchone()
        assert account["exchange_code"] == "UPBIT"
        assert account["exchange_name"] == "업비트"
        assert account["network_family"] == "EVM"
        assert account["network_name"] == "EVM"
        account_id = int(account["id"])

    page = client.get("/accounts")
    assert "등록된 거래소 지갑".encode() in page.data
    assert "업비트".encode() in page.data
    assert b"0x2222222222222222222222222222222222222222" in page.data
    assert b'data-copy-address="0x2222222222222222222222222222222222222222"' in page.data

    duplicate = client.post(
        "/accounts/exchange",
        data={
            "csrf_token": csrf,
            "exchange_code": "UPBIT",
            "network_name": "EVM",
            "wallet_address": "0x2222222222222222222222222222222222222222",
            "note": "중복",
        },
        follow_redirects=True,
    )
    assert "이미 등록된 지갑 주소".encode() in duplicate.data

    response = client.post(
        f"/accounts/exchange/{account_id}/delete", data={"csrf_token": csrf}
    )
    assert response.status_code == 303
    with TaxStore(tmp_path / "data" / "tax_ledger.db") as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM exchange_wallet_accounts"
        ).fetchone()[0] == 0


def test_review_wallet_dropdown_uses_registered_address(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    database = tmp_path / "data" / "tax_ledger.db"
    with TaxStore(database) as store:
        account_id = store.create_personal_wallet_account(
            wallet_name="Solana 개인지갑",
            wallet_address="So11111111111111111111111111111111111111112",
            network_family="SVM",
            network_name="SOLANA",
            note="",
            recorded_at_utc="2026-08-01T00:00:00+00:00",
        )
        event_id = int(store.connection.execute(
            """INSERT INTO normalized_events (
                source_type, occurred_at_utc, event_type, asset, quantity,
                classification_status, created_at_utc
            ) VALUES ('BACKPACK', '2026-08-02T00:00:00+00:00', 'WITHDRAWAL',
                      'SOL', '1', 'NEEDS_REVIEW', '2026-08-02T00:00:00+00:00')"""
        ).lastrowid)
        store.connection.commit()
    client = app.test_client()
    page = client.get("/reviews?event_type=WITHDRAWAL")
    assert f'value="{account_id}"'.encode() in page.data
    assert "Solana 개인지갑".encode() in page.data
    with client.session_transaction() as session:
        session["review_csrf_token"] = "wallet-account-token"
    response = client.post(
        f"/reviews/{event_id}/personal-wallet-transfer",
        data={
            "csrf_token": "wallet-account-token",
            "wallet_account_id": str(account_id),
            "wallet_basis_mode": "AUTO_CARRY",
            "origin_note": "등록 지갑 선택",
        },
    )
    assert response.status_code == 303
    with TaxStore(database) as store:
        transfer = store.connection.execute(
            "SELECT wallet_name, wallet_address FROM personal_wallet_transfers WHERE normalized_event_id = ?",
            (event_id,),
        ).fetchone()
        assert tuple(transfer) == (
            "Solana 개인지갑", "So11111111111111111111111111111111111111112"
        )
