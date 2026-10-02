from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from backpack_tax.db import TaxStore


def _number(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _address(value: object) -> str:
    address = str(value or "").strip()
    # EVM addresses are case insensitive; Solana and other addresses are not.
    return address.lower() if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) else address


def _snapshot(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def transfer_review_queue(store: TaxStore) -> dict[str, Any]:
    rows = [dict(row) for row in store.connection.execute(
        """SELECT ne.* FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE ne.classification_status = 'NEEDS_REVIEW'
          AND ne.event_type IN ('DEPOSIT', 'WITHDRAWAL') AND ne.asset != 'KRW'
          AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
          AND NOT EXISTS (SELECT 1 FROM transfer_matches tm WHERE tm.status = 'ACTIVE'
              AND (tm.withdrawal_event_id = ne.id OR tm.deposit_event_id = ne.id))
        ORDER BY ne.occurred_at_utc, ne.id"""
    )]
    for row in rows:
        timestamp = _time(row["occurred_at_utc"])
        row["time_display"] = timestamp.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M") if timestamp else "시각 미확인"

    deposits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["event_type"] == "DEPOSIT":
            deposits[row["asset"]].append(row)
    candidates: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for withdrawal in rows:
        if withdrawal["event_type"] != "WITHDRAWAL":
            continue
        quantity = _number(withdrawal["quantity"])
        timestamp = _time(withdrawal["occurred_at_utc"])
        if not quantity or not timestamp:
            continue
        for deposit in deposits[withdrawal["asset"]]:
            if withdrawal["source_type"] == deposit["source_type"]:
                continue
            received = _number(deposit["quantity"])
            arrived = _time(deposit["occurred_at_utc"])
            if not received or not arrived or received > quantity:
                continue
            seconds = (arrived - timestamp).total_seconds()
            if not 0 <= seconds <= 30 * 60:
                continue
            networks = [str(r["network"] or "").strip().lower() for r in (withdrawal, deposit)]
            if all(networks) and networks[0] != networks[1]:
                continue
            txids = [str(r["txid"] or "").strip() for r in (withdrawal, deposit)]
            if all(txids) and txids[0] != txids[1]:
                continue
            difference = quantity - received
            fee = _number(withdrawal["fee_quantity"]) or Decimal(0)
            fee_matches = withdrawal["fee_asset"] == withdrawal["asset"] and difference == fee
            if difference and not fee_matches and difference / quantity > Decimal("0.001"):
                continue
            pair = {
                "id": f"{withdrawal['id']}:{deposit['id']}",
                "withdrawal": withdrawal, "deposit": deposit,
                "difference": difference, "minutes": int(seconds // 60),
                "evidence": "TXID 일치" if all(txids) else "시간·수량 유사 · TXID 대조 불가",
                "cross_currency": (withdrawal["source_type"] == "BACKPACK") != (deposit["source_type"] == "BACKPACK"),
            }
            candidates[withdrawal["id"]].append(pair)
            candidates[deposit["id"]].append(pair)

    pairs = [items[0] for event_id, items in candidates.items()
             if len(items) == 1 and items[0]["withdrawal"]["id"] == event_id
             and len(candidates[items[0]["deposit"]["id"]]) == 1]
    paired = {row["id"] for pair in pairs for row in (pair["withdrawal"], pair["deposit"])}
    grouped: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["id"] in paired:
            continue
        address = _address((row["from_address"] if row["event_type"] == "DEPOSIT" else row["to_address"]) or row["wallet_address"])
        network = str(row["network"] or "").strip()
        key = (address, network) if address else ("", network, row["source_type"], row["event_type"], row["asset"])
        grouped[key].append(row)
    groups = []
    for key, events in grouped.items():
        groups.append({
            "key": _snapshot(events), "address": key[0], "network": key[1],
            "events": events, "count": len(events),
            "deposits": sum(row["event_type"] == "DEPOSIT" for row in events),
            "withdrawals": sum(row["event_type"] == "WITHDRAWAL" for row in events),
            "assets": ", ".join(sorted({row["asset"] for row in events})),
            "sources": list(dict.fromkeys(row["source_type"] for row in events)),
        })
    groups.sort(key=lambda group: -group["count"])
    return {"total": len(rows), "pairs": pairs, "pair_key": _snapshot(pairs),
            "groups": groups, "grouped_count": len(rows) - len(paired)}


def apply_transfer_batch(
    store: TaxStore, *, mode: str, snapshot: str, selected: list[str],
    wallet_name: str = "", wallet_address: str = "",
) -> int:
    if mode not in {"PAIRS", "WALLET"} or not selected or len(selected) > 5000:
        raise ValueError("처리할 거래를 선택하세요.")
    selected_ids = set(selected)
    now = datetime.now(UTC).isoformat()
    # Lock and revalidate the exact preview before changing anything. Nested
    # decisions use savepoints so the whole selection succeeds or rolls back.
    with store.connection:
        store.connection.execute("BEGIN IMMEDIATE")
        queue = transfer_review_queue(store)
        if mode == "PAIRS":
            available = {pair["id"]: pair for pair in queue["pairs"]}
            if snapshot != queue["pair_key"] or not selected_ids <= available.keys():
                raise ValueError("거래 상태가 바뀌었습니다. 새로 표시된 후보를 다시 확인하세요.")
            for pair_id in sorted(selected_ids):
                pair = available[pair_id]
                store.record_review_decision(
                    pair["withdrawal"]["id"], decision="INTERNAL_TRANSFER",
                    counterparty_event_id=pair["deposit"]["id"],
                    rationale="묶음 검토: 사용자가 시간·수량과 거래소를 확인한 본인 계정 간 이동",
                    decided_at_utc=now,
                )
            return len(selected_ids) * 2
        group = next((group for group in queue["groups"] if group["key"] == snapshot), None)
        if group is None or not selected_ids <= {str(row["id"]) for row in group["events"]}:
            raise ValueError("거래 상태가 바뀌었습니다. 새로 표시된 묶음을 다시 확인하세요.")
        if not wallet_name.strip() or not wallet_address.strip():
            raise ValueError("본인 개인 지갑의 이름과 주소를 입력하세요.")
        for row in group["events"]:
            if str(row["id"]) not in selected_ids:
                continue
            if not _number(row["quantity"]):
                raise ValueError("수량이 올바르지 않은 거래가 있습니다. 개별 검토에서 확인하세요.")
            store.record_personal_wallet_transfer(
                row["id"], wallet_name=wallet_name, wallet_address=wallet_address,
                basis_mode="AUTO_CARRY", unit_cost="", fee_cost="0",
                note="묶음 검토: 사용자가 선택한 거래를 본인 개인 지갑 이동으로 확인",
                recorded_at_utc=now,
            )
        return len(selected_ids)
