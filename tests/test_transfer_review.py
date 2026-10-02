from pathlib import Path

import pytest

from backpack_tax.app import create_app
from backpack_tax.db import TaxStore
from backpack_tax.imports import import_exchange_csv
from backpack_tax.transfer_review import apply_transfer_batch, transfer_review_queue


def event(store, *, kind="WITHDRAWAL", source="BACKPACK", quantity="10", address="wallet-A",
          timestamp="2026-08-01T00:00:00Z", network="Solana", txid=None, status="NEEDS_REVIEW", asset="USDC"):
    row = store.connection.execute(
        """INSERT INTO normalized_events (source_type, event_type, asset, quantity,
        occurred_at_utc, network, txid, wallet_address, classification_status, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, kind, asset, quantity, timestamp, network, txid, address, status, timestamp),
    )
    store.connection.commit()
    return row.lastrowid


def test_unique_pair_links_both_events_and_preserves_audit(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        withdrawal = event(store)
        deposit = event(store, kind="DEPOSIT", source="UPBIT", timestamp="2026-08-01T00:05:00Z")
        queue = transfer_review_queue(store)
        assert len(queue["pairs"]) == 1
        assert queue["groups"] == []
        assert queue["pairs"][0]["cross_currency"] is True
        assert apply_transfer_batch(store, mode="PAIRS", snapshot=queue["pair_key"], selected=[f"{withdrawal}:{deposit}"]) == 2
        assert store.transfer_match_for_event(withdrawal)["deposit_event_id"] == deposit
        assert store.connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 2
        assert transfer_review_queue(store)["total"] == 0


@pytest.mark.parametrize("overrides", [
    {"network": "Ethereum"}, {"txid": "different"}, {"quantity": "9"},
    {"quantity": "NaN"}, {"quantity": "Infinity"},
    {"timestamp": "2026-08-01T01:00:00Z"},
    {"timestamp": "2026-07-31T23:59:00Z"}, {"source": "BACKPACK"},
])
def test_conflicting_or_weak_pairs_are_not_suggested(tmp_path, overrides):
    with TaxStore(tmp_path / "test.db") as store:
        event(store, txid="withdrawal-tx")
        args = dict(kind="DEPOSIT", source="UPBIT", timestamp="2026-08-01T00:05:00Z")
        args.update(overrides)
        event(store, **args)
        queue = transfer_review_queue(store)
        assert queue["pairs"] == []
        assert queue["grouped_count"] == 2


def test_ambiguous_pairs_stay_in_wallet_groups(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        event(store)
        event(store)
        event(store, kind="DEPOSIT", source="UPBIT", timestamp="2026-08-01T00:05:00Z")
        queue = transfer_review_queue(store)
        assert queue["pairs"] == []
        assert sum(g["count"] for g in queue["groups"]) == 3


def test_wallet_batch_processes_only_selected_rows_and_keeps_basis_unknown(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        first = event(store, kind="DEPOSIT")
        second = event(store, kind="DEPOSIT")
        event(store, kind="AIRDROP")
        event(store, status="NORMALIZED")
        queue = transfer_review_queue(store)
        assert queue["total"] == 2
        group = queue["groups"][0]
        assert apply_transfer_batch(store, mode="WALLET", snapshot=group["key"], selected=[str(first)],
                                    wallet_name="My wallet", wallet_address="wallet-A") == 1
        stored = store.connection.execute("SELECT basis_mode, total_cost FROM personal_wallet_transfers").fetchone()
        assert tuple(stored) == ("AUTO_CARRY", None)
        assert store.connection.execute("SELECT classification_status FROM normalized_events WHERE id=?", (second,)).fetchone()[0] == "NEEDS_REVIEW"
        assert store.connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 1


def test_stale_group_or_foreign_selection_changes_nothing(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        first = event(store)
        foreign = event(store, address="different-wallet")
        group = transfer_review_queue(store)["groups"][0]
        with pytest.raises(ValueError):
            apply_transfer_batch(store, mode="WALLET", snapshot=group["key"], selected=[str(first), str(foreign)], wallet_name="Mine", wallet_address="wallet-A")
        event(store)
        with pytest.raises(ValueError):
            apply_transfer_batch(store, mode="WALLET", snapshot=group["key"], selected=[str(first)], wallet_name="Mine", wallet_address="wallet-A")
        assert store.connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0


def test_wallet_batch_rolls_back_all_prior_decisions_on_error(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        first = event(store)
        invalid = event(store, quantity="0")
        group = transfer_review_queue(store)["groups"][0]
        with pytest.raises(ValueError):
            apply_transfer_batch(store, mode="WALLET", snapshot=group["key"], selected=[str(first), str(invalid)], wallet_name="Mine", wallet_address="wallet-A")
        assert store.connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM personal_wallet_transfers").fetchone()[0] == 0
        assert transfer_review_queue(store)["total"] == 2


def test_pair_batch_rolls_back_prior_links_on_error(tmp_path, monkeypatch):
    with TaxStore(tmp_path / "test.db") as store:
        for asset in ("USDC", "SOL"):
            event(store, asset=asset)
            event(store, asset=asset, kind="DEPOSIT", source="UPBIT", timestamp="2026-08-01T00:05:00Z")
        queue = transfer_review_queue(store)
        original = store.record_review_decision
        calls = 0
        def failing(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise ValueError("simulated failure")
            return result
        monkeypatch.setattr(store, "record_review_decision", failing)
        with pytest.raises(ValueError):
            apply_transfer_batch(store, mode="PAIRS", snapshot=queue["pair_key"], selected=[p["id"] for p in queue["pairs"]])
        assert store.connection.execute("SELECT COUNT(*) FROM transfer_matches").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0
        assert transfer_review_queue(store)["total"] == 4


def test_non_evm_addresses_and_networks_are_separate(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        event(store, address="Abc")
        event(store, address="abc")
        event(store, address="abc", network="Ethereum")
        assert len(transfer_review_queue(store)["groups"]) == 3


def test_deleted_source_is_not_in_batch_preview(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        result = import_exchange_csv(
            store, evidence_directory=tmp_path / "evidence", source_type="BACKPACK",
            original_filename="fixture.csv", content=b"field\nvalue\n", document_type="DEPOSITS",
        )
        raw_id = store.connection.execute("SELECT id FROM raw_records WHERE source_file_id=?", (result.file_id,)).fetchone()[0]
        selected = event(store)
        store.connection.execute("UPDATE normalized_events SET raw_record_id=? WHERE id=?", (raw_id, selected))
        store.connection.execute("UPDATE source_files SET deleted_at_utc='2026-08-03T00:00:00Z' WHERE id=?", (result.file_id,))
        store.connection.commit()
        assert transfer_review_queue(store)["total"] == 0


def test_pair_preview_rejects_changed_quantity(tmp_path):
    with TaxStore(tmp_path / "test.db") as store:
        withdrawal = event(store)
        deposit = event(store, kind="DEPOSIT", source="UPBIT", timestamp="2026-08-01T00:05:00Z")
        queue = transfer_review_queue(store)
        store.connection.execute("UPDATE normalized_events SET quantity='9.999' WHERE id=?", (deposit,))
        store.connection.commit()
        with pytest.raises(ValueError):
            apply_transfer_batch(store, mode="PAIRS", snapshot=queue["pair_key"], selected=[f"{withdrawal}:{deposit}"])
        assert store.connection.execute("SELECT COUNT(*) FROM transfer_matches").fetchone()[0] == 0


def test_route_requires_csrf_ownership_and_current_preview(tmp_path: Path):
    app = create_app(project_root=tmp_path, testing=True)
    with TaxStore(tmp_path / "data/tax_ledger.db") as store:
        selected = event(store)
        snapshot = transfer_review_queue(store)["groups"][0]["key"]
    client = app.test_client()
    assert client.get("/reviews/transfers").status_code == 200
    payload = dict(mode="WALLET", snapshot=snapshot, selected=str(selected), wallet_name="Mine", wallet_address="wallet-A")
    assert client.post("/reviews/transfers/apply", data=payload).status_code == 400
    with client.session_transaction() as session:
        payload["csrf_token"] = session["review_csrf_token"]
    client.post("/reviews/transfers/apply", data=payload)
    with TaxStore(tmp_path / "data/tax_ledger.db") as store:
        assert transfer_review_queue(store)["total"] == 1
    payload["ownership_confirmed"] = "1"
    assert client.post("/reviews/transfers/apply", data=payload).status_code == 303
    # Replaying the old preview must not create duplicate history.
    client.post("/reviews/transfers/apply", data=payload)
    with TaxStore(tmp_path / "data/tax_ledger.db") as store:
        assert transfer_review_queue(store)["total"] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 1
