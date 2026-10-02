from __future__ import annotations

from decimal import Decimal
from typing import Any

from backpack_tax.analytics import asset_performance
from backpack_tax.db import TaxStore


def basis_gap_summary(store: TaxStore) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    for source_type in ("BACKPACK", "UPBIT", "BITHUMB"):
        for performance in asset_performance(store, source_type=source_type):
            if not performance["has_unknown_basis"]:
                continue
            asset = str(performance["asset"])
            candidate = store.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN ne.classification_status = 'NEEDS_REVIEW'
                                THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN ne.classification_status = 'NEEDS_REVIEW'
                                      AND (ev.id IS NULL OR ev.valuation_status = 'UNPRICED')
                                THEN 1 ELSE 0 END) AS unpriced,
                       MIN(ne.occurred_at_utc) AS first_at_utc,
                       MAX(ne.occurred_at_utc) AS last_at_utc
                FROM normalized_events ne
                LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE ne.source_type = ? AND ne.asset = ?
                  AND ne.event_type IN ('DEPOSIT', 'AIRDROP')
                  AND ne.classification_status != 'EXCLUDED'
                  AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                """,
                (source_type, asset),
            ).fetchone()
            gaps.append(
                {
                    "source_type": source_type,
                    "asset": asset,
                    "instrument_label": performance["instrument_label"],
                    "unknown_quantity": performance["unknown_sell_quantity"],
                    "unknown_quantity_display": performance["unknown_basis_display"],
                    "unknown_amount": performance["unknown_sell_amount"],
                    "unknown_amount_display": performance["unknown_basis_amount_display"],
                    "candidate_count": int(candidate["total"] or 0),
                    "pending_count": int(candidate["pending"] or 0),
                    "unpriced_count": int(candidate["unpriced"] or 0),
                    "first_candidate_at_utc": candidate["first_at_utc"],
                    "last_candidate_at_utc": candidate["last_at_utc"],
                }
            )
    gaps.sort(key=lambda item: item["unknown_amount"], reverse=True)
    return {
        "gaps": gaps,
        "asset_count": len(gaps),
        "unknown_quantity_total": sum(
            (item["unknown_quantity"] for item in gaps), Decimal("0")
        ),
        "candidate_count": sum(item["candidate_count"] for item in gaps),
        "pending_count": sum(item["pending_count"] for item in gaps),
    }
