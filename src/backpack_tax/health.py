from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backpack_tax.analytics import asset_performance
from backpack_tax.db import TaxStore


FINAL_DECISIONS = {
    "CONFIRMED",
    "AIRDROP_BASIS",
    "INTERNAL_TRANSFER",
    "PERSONAL_WALLET_TRANSFER",
    "EXTERNAL_PURCHASE",
    "OPENING_BALANCE",
    "EXCLUDED",
}


def _event_items(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": int(row["id"]),
            "source_type": str(row["source_type"]),
            "asset": str(row["asset"] or "자산 미확인"),
            "event_type": str(row["event_type"] or "이벤트 미확인"),
            "occurred_at_utc": str(row["occurred_at_utc"] or "시각 미확인"),
            "detail": str(row["detail"] or ""),
        }
        for row in rows[:50]
    ]


def _check(
    *,
    code: str,
    title: str,
    severity: str,
    description: str,
    action: str,
    count: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "status": "PASS" if count == 0 else severity,
        "description": description,
        "action": action,
        "count": count,
        "items": items,
        "truncated": count > len(items),
    }


def run_data_health_checks(store: TaxStore) -> dict[str, Any]:
    active_source = "(ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)"
    checks: list[dict[str, Any]] = []

    rows = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, ne.asset, ne.event_type, ne.occurred_at_utc,
               ne.classification_status || ' / ' || COALESCE(rd.decision, '판정 없음') AS detail
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN review_decisions rd ON rd.id = (
            SELECT latest.id FROM review_decisions latest
            WHERE latest.normalized_event_id = ne.id
            ORDER BY latest.id DESC LIMIT 1
        )
        WHERE {active_source}
          AND (
              (ne.classification_status = 'NEEDS_REVIEW' AND rd.decision IN ({','.join('?' for _ in FINAL_DECISIONS)}))
              OR (ne.classification_status != 'NEEDS_REVIEW' AND rd.decision = 'REOPENED')
          )
        ORDER BY ne.occurred_at_utc DESC, ne.id DESC
        """,
        tuple(sorted(FINAL_DECISIONS)),
    ).fetchall()
    checks.append(
        _check(
            code="REVIEW_STATE_CONFLICT",
            title="판정 이력과 현재 상태 불일치",
            severity="CRITICAL",
            description="승인 이력은 있지만 검토 대기이거나, 다시 검토 이력 뒤에도 완료 상태인 이벤트입니다.",
            action="관련 이벤트를 열어 현재 판정을 다시 저장하세요.",
            count=len(rows),
            items=_event_items(rows),
        )
    )

    rows = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, ne.asset, ne.event_type, ne.occurred_at_utc,
               COALESCE(ev.valuation_status, '평가 없음') || ' / ' ||
               COALESCE(ev.native_currency, '기준통화 없음') AS detail
        FROM normalized_events ne
        LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE {active_source}
          AND ne.event_type = 'AIRDROP'
          AND ne.classification_status = 'NORMALIZED'
          AND NOT (
              ev.valuation_status = 'VALUED'
              AND ev.native_value IS NOT NULL
              AND (
                  (ne.source_type = 'BACKPACK' AND ev.native_currency = 'USD')
                  OR (ne.source_type IN ('UPBIT', 'BITHUMB') AND ev.native_currency = 'KRW')
              )
          )
        ORDER BY ne.occurred_at_utc DESC, ne.id DESC
        """
    ).fetchall()
    checks.append(
        _check(
            code="AIRDROP_BASIS_MISSING",
            title="정규화됐지만 취득가가 없는 에어드랍",
            severity="CRITICAL",
            description="정규화 완료된 에어드랍에 거래소 기준통화 취득가가 없어 이후 매도 PNL이 틀릴 수 있습니다.",
            action="다시 검토로 돌린 뒤 조회 가격 또는 직접 입력한 취득가를 승인하세요.",
            count=len(rows),
            items=_event_items(rows),
        )
    )

    rows = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, ne.asset, ne.event_type, ne.occurred_at_utc,
               ne.classification_reason AS detail
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE {active_source}
          AND ne.source_type IN ('UPBIT', 'BITHUMB')
          AND ne.asset = 'KRW'
          AND ne.event_type IN ('DEPOSIT', 'WITHDRAWAL')
          AND ne.classification_status = 'NEEDS_REVIEW'
        ORDER BY ne.occurred_at_utc DESC, ne.id DESC
        """
    ).fetchall()
    checks.append(
        _check(
            code="KRW_REVIEW_PENDING",
            title="국내 거래소 KRW 입출금 검토 대기",
            severity="CRITICAL",
            description="업비트·빗썸 KRW 입출금은 인증계좌 거래이므로 자동 정규화되어야 합니다.",
            action="자동 정규화를 다시 실행하고 원본 자산·이벤트 유형을 확인하세요.",
            count=len(rows),
            items=_event_items(rows),
        )
    )

    rows = store.connection.execute(
        f"""
        WITH latest AS (
            SELECT rd.normalized_event_id, rd.decision
            FROM review_decisions rd
            WHERE rd.id = (
                SELECT newest.id FROM review_decisions newest
                WHERE newest.normalized_event_id = rd.normalized_event_id
                ORDER BY newest.id DESC LIMIT 1
            )
        )
        SELECT ne.id, ne.source_type, ne.asset, ne.event_type, ne.occurred_at_utc,
               '활성 분류 ' || (
                   CASE WHEN tm.id IS NOT NULL THEN 1 ELSE 0 END
                   + CASE WHEN da.id IS NOT NULL AND latest.decision IN ('EXTERNAL_PURCHASE', 'OPENING_BALANCE') THEN 1 ELSE 0 END
                   + CASE WHEN pwt.id IS NOT NULL AND latest.decision = 'PERSONAL_WALLET_TRANSFER' THEN 1 ELSE 0 END
                   + CASE WHEN ne.event_type = 'AIRDROP' AND latest.decision = 'AIRDROP_BASIS' THEN 1 ELSE 0 END
               ) || '개' AS detail
        FROM normalized_events ne
        LEFT JOIN latest ON latest.normalized_event_id = ne.id
        LEFT JOIN transfer_matches tm ON tm.status = 'ACTIVE'
          AND (tm.withdrawal_event_id = ne.id OR tm.deposit_event_id = ne.id)
        LEFT JOIN deposit_acquisitions da ON da.normalized_event_id = ne.id
        LEFT JOIN personal_wallet_transfers pwt ON pwt.normalized_event_id = ne.id
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE {active_source}
          AND (
              CASE WHEN tm.id IS NOT NULL THEN 1 ELSE 0 END
              + CASE WHEN da.id IS NOT NULL AND latest.decision IN ('EXTERNAL_PURCHASE', 'OPENING_BALANCE') THEN 1 ELSE 0 END
              + CASE WHEN pwt.id IS NOT NULL AND latest.decision = 'PERSONAL_WALLET_TRANSFER' THEN 1 ELSE 0 END
              + CASE WHEN ne.event_type = 'AIRDROP' AND latest.decision = 'AIRDROP_BASIS' THEN 1 ELSE 0 END
          ) > 1
        ORDER BY ne.occurred_at_utc DESC, ne.id DESC
        """
    ).fetchall()
    checks.append(
        _check(
            code="MULTIPLE_BASIS_CLASSIFICATIONS",
            title="취득가 원천 중복 분류",
            severity="CRITICAL",
            description="하나의 입출금이 내부 이동·외부 취득·개인 지갑·에어드랍 중 두 가지 이상으로 활성화됐습니다.",
            action="관련 이벤트를 다시 검토해 하나의 취득가 원천만 남기세요.",
            count=len(rows),
            items=_event_items(rows),
        )
    )

    duplicate_rows = store.connection.execute(
        f"""
        SELECT MIN(ne.id) AS id, ne.source_type, ne.asset, ne.event_type,
               MAX(ne.occurred_at_utc) AS occurred_at_utc,
               'TXID ' || ne.txid || ' · ' || COUNT(*) || '건' AS detail,
               COUNT(*) AS duplicate_count
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE {active_source}
          AND ne.txid IS NOT NULL AND TRIM(ne.txid) != ''
        GROUP BY ne.source_type, ne.event_type, ne.asset, ne.quantity, ne.txid
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, MAX(ne.occurred_at_utc) DESC
        """
    ).fetchall()
    duplicate_count = sum(int(row["duplicate_count"]) - 1 for row in duplicate_rows)
    checks.append(
        _check(
            code="DUPLICATE_TXID",
            title="중복 TXID 거래 후보",
            severity="WARNING",
            description="같은 거래소·유형·자산·수량·TXID가 반복된 이벤트입니다. 정상적인 분할 기록일 수도 있습니다.",
            action="원본 행을 비교해 실제 중복이면 하나를 계산 제외하세요.",
            count=duplicate_count,
            items=_event_items(duplicate_rows),
        )
    )

    unknown_items: list[dict[str, Any]] = []
    for source_type in ("BACKPACK", "UPBIT", "BITHUMB"):
        for row in asset_performance(store, source_type=source_type):
            if not row["has_unknown_basis"]:
                continue
            candidate_count = int(
                store.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM normalized_events ne
                    LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                    LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                    WHERE ne.source_type = ?
                      AND ne.asset = ?
                      AND ne.event_type = 'DEPOSIT'
                      AND ne.classification_status = 'NEEDS_REVIEW'
                      AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                    """,
                    (source_type, row["asset"]),
                ).fetchone()[0]
            )
            unknown_items.append(
                {
                    "source_type": source_type,
                    "asset": row["asset"],
                    "event_type": "SELL",
                    "occurred_at_utc": "전체 기간",
                    "detail": (
                        f"미확인 매도 {row['unknown_basis_display']} · "
                        f"금액 {row['unknown_basis_amount_display']} "
                        f"{'USD' if source_type == 'BACKPACK' else 'KRW'} · "
                        f"취득가 입력 가능한 검토 대기 입금 {candidate_count}건"
                    ),
                    "review_asset": row["asset"] if candidate_count else None,
                    "candidate_count": candidate_count,
                    "asset_link": candidate_count == 0,
                }
            )
    checks.append(
        _check(
            code="UNKNOWN_SALE_BASIS",
            title="취득가 미확인 매도",
            severity="WARNING",
            description="매도 시점에 원장에서 찾을 수 있는 취득 물량이 부족해 PNL 일부가 계산에서 제외됐습니다.",
            action="매도 이전의 외부 입금·개인 지갑·기초잔고 취득가를 등록하세요.",
            count=len(unknown_items),
            items=unknown_items[:50],
        )
    )

    pending_rows = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, ne.asset, ne.event_type, ne.occurred_at_utc,
               ne.classification_reason AS detail
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE {active_source} AND ne.classification_status = 'NEEDS_REVIEW'
        ORDER BY ne.occurred_at_utc DESC, ne.id DESC
        """
    ).fetchall()
    checks.append(
        _check(
            code="PENDING_REVIEW",
            title="아직 판정하지 않은 검토 이벤트",
            severity="WARNING",
            description="자동 분류가 확정되지 않아 사용자의 판정이 필요한 이벤트입니다.",
            action="검토센터에서 원본과 취득가 근거를 확인해 처리하세요.",
            count=len(pending_rows),
            items=_event_items(pending_rows),
        )
    )

    summary = {
        "checks": len(checks),
        "passed": sum(check["status"] == "PASS" for check in checks),
        "warnings": sum(check["status"] == "WARNING" for check in checks),
        "critical": sum(check["status"] == "CRITICAL" for check in checks),
        "issues": sum(check["count"] for check in checks),
    }
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "checks": checks,
    }
