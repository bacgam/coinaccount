from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backpack_tax.db import TaxStore


def smart_review_inbox(store: TaxStore) -> dict[str, Any]:
    active = "(ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)"

    def count(where: str, parameters: tuple[Any, ...] = ()) -> int:
        return int(store.connection.execute(
            f"""SELECT COUNT(*) FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.classification_status = 'NEEDS_REVIEW'
              AND {active} AND ({where})""",
            parameters,
        ).fetchone()[0])

    trade_count = count("ne.event_type = 'TRADE_FILL'")
    airdrop_count = count(
        """ne.event_type = 'AIRDROP' AND ev.valuation_status = 'VALUED'
        AND ev.native_value IS NOT NULL
        AND ((ne.source_type = 'BACKPACK' AND ev.native_currency = 'USD')
          OR (ne.source_type IN ('UPBIT', 'BITHUMB') AND ev.native_currency = 'KRW'))"""
    )
    airdrop_review_count = count("ne.event_type = 'AIRDROP'") - airdrop_count
    transfer_count = count("ne.event_type IN ('DEPOSIT', 'WITHDRAWAL')")
    unpriced_count = count("ev.id IS NULL OR ev.valuation_status = 'UNPRICED'")
    actionable = trade_count + airdrop_count
    groups = [
        {
            "key": "TRADE_FILL",
            "title": "체결 거래 자동 정규화",
            "count": trade_count,
            "confidence": "높음",
            "description": "거래소 원본에 매수·매도 체결로 확정된 거래입니다.",
            "actionable": True,
            "pnl_impact": True,
            "event_type": "TRADE_FILL",
        },
        {
            "key": "VALUED_AIRDROP",
            "title": "가격 확인된 에어드랍 취득가 승인",
            "count": airdrop_count,
            "confidence": "사용자 확인",
            "description": "저장된 평가액을 취득가로 승인하면 이후 매도 PNL 원가에 반영됩니다.",
            "actionable": True,
            "pnl_impact": True,
            "event_type": "AIRDROP",
        },
        {
            "key": "TRANSFER_REVIEW",
            "title": "입출금·지갑 이동 검토",
            "count": transfer_count,
            "confidence": "묶음 확인",
            "description": "시간·수량이 맞는 이동 후보와 같은 주소의 거래를 모아서 확인합니다.",
            "actionable": False,
            "pnl_impact": True,
            "event_type": "DEPOSIT",
        },
        {
            "key": "AIRDROP_REVIEW",
            "title": "취득가 확인이 필요한 에어드랍",
            "count": max(0, airdrop_review_count),
            "confidence": "취득가 확인",
            "description": "가격 또는 기준통화를 확인한 뒤 에어드랍 취득가를 승인해야 합니다.",
            "actionable": False,
            "pnl_impact": True,
            "event_type": "AIRDROP",
        },
        {
            "key": "UNPRICED",
            "title": "가격 증빙 필요",
            "count": unpriced_count,
            "confidence": "자동 조회",
            "description": "거래 시점 가격을 먼저 채운 뒤 분류할 수 있습니다.",
            "actionable": False,
            "pnl_impact": False,
            "unpriced": True,
        },
    ]
    return {
        "groups": [group for group in groups if group["count"]],
        "actionable": actionable,
        "manual": transfer_count,
    }


def apply_smart_review_rule(store: TaxStore, rule: str) -> int:
    rule = rule.upper().strip()
    now = datetime.now(UTC).isoformat()
    if rule == "TRADE_FILL":
        rows = store.connection.execute(
            """SELECT ne.id FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.classification_status = 'NEEDS_REVIEW'
              AND ne.event_type = 'TRADE_FILL'
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            ORDER BY ne.id LIMIT 5000"""
        ).fetchall()
        for row in rows:
            store.record_review_decision(
                int(row["id"]), decision="CONFIRMED",
                rationale="스마트 검토: 거래소 원본의 체결 거래 일괄 정규화",
                decided_at_utc=now,
            )
        return len(rows)
    if rule == "VALUED_AIRDROP":
        rows = store.connection.execute(
            """SELECT ne.id FROM normalized_events ne
            JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.classification_status = 'NEEDS_REVIEW'
              AND ne.event_type = 'AIRDROP'
              AND ev.valuation_status = 'VALUED' AND ev.native_value IS NOT NULL
              AND ((ne.source_type = 'BACKPACK' AND ev.native_currency = 'USD')
                OR (ne.source_type IN ('UPBIT', 'BITHUMB') AND ev.native_currency = 'KRW'))
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            ORDER BY ne.id LIMIT 5000"""
        ).fetchall()
        completed = 0
        for row in rows:
            store.confirm_airdrop_basis(
                int(row["id"]),
                rationale="스마트 검토: 저장된 평가액을 에어드랍 취득가로 일괄 승인",
                decided_at_utc=now,
            )
            completed += 1
        return completed
    raise ValueError("지원하지 않는 스마트 검토 규칙입니다.")
