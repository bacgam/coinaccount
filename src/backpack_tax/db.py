from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


_DUST_CONDITION_SQL = (
    "(ev.valuation_status = 'VALUED' "
    "AND CAST(ev.usd_value AS REAL) > 0 "
    "AND CAST(ev.usd_value AS REAL) < 1)"
)


class TaxStore:
    @contextmanager
    def review_transaction(self):
        """Keep individual decisions atomic inside a larger review batch."""
        if not self.connection.in_transaction:
            with self.connection:
                yield
            return
        self.connection.execute("SAVEPOINT review_operation")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK TO review_operation")
            self.connection.execute("RELEASE review_operation")
            raise
        else:
            self.connection.execute("RELEASE review_operation")

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL CHECK(source_type IN ('UPBIT', 'BITHUMB', 'BACKPACK')),
                document_type TEXT NOT NULL DEFAULT 'TRANSACTIONS',
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                imported_at_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RAW_IMPORTED',
                notes TEXT,
                deleted_at_utc TEXT,
                UNIQUE(source_type, sha256)
            );

            CREATE TABLE IF NOT EXISTS raw_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file_id INTEGER NOT NULL,
                source_row_number INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                row_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(source_file_id) REFERENCES source_files(id),
                UNIQUE(source_file_id, source_row_number)
            );

            CREATE TABLE IF NOT EXISTS normalized_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_record_id INTEGER,
                source_type TEXT NOT NULL,
                occurred_at_utc TEXT,
                event_type TEXT,
                asset TEXT,
                quantity TEXT,
                quote_asset TEXT,
                unit_price TEXT,
                gross_amount TEXT,
                fee_asset TEXT,
                fee_quantity TEXT,
                external_id TEXT,
                txid TEXT,
                order_id TEXT,
                trade_id TEXT,
                wallet_address TEXT,
                classification_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
                classification_reason TEXT,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(raw_record_id) REFERENCES raw_records(id)
            );

            CREATE TABLE IF NOT EXISTS price_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                quote_asset TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                price TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(asset, quote_asset, observed_at_utc, source_name)
            );

            CREATE TABLE IF NOT EXISTS event_valuations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_event_id INTEGER NOT NULL UNIQUE,
                valuation_status TEXT NOT NULL CHECK(valuation_status IN ('VALUED', 'UNPRICED')),
                usd_value TEXT,
                unit_price_usd TEXT,
                native_value TEXT,
                native_currency TEXT,
                valuation_method TEXT NOT NULL,
                confidence TEXT NOT NULL CHECK(confidence IN ('EXACT', 'HIGH', 'MEDIUM', 'NONE')),
                reason TEXT NOT NULL,
                valued_at_utc TEXT,
                pricing_version TEXT NOT NULL,
                calculated_at_utc TEXT NOT NULL,
                FOREIGN KEY(normalized_event_id) REFERENCES normalized_events(id)
            );

            CREATE TABLE IF NOT EXISTS event_valuation_evidence (
                event_valuation_id INTEGER NOT NULL,
                price_evidence_id INTEGER NOT NULL,
                evidence_role TEXT NOT NULL,
                PRIMARY KEY(event_valuation_id, price_evidence_id, evidence_role),
                FOREIGN KEY(event_valuation_id) REFERENCES event_valuations(id),
                FOREIGN KEY(price_evidence_id) REFERENCES price_evidence(id)
            );

            CREATE TABLE IF NOT EXISTS manual_price_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_event_id INTEGER NOT NULL,
                unit_price TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(currency IN ('KRW', 'USD')),
                note TEXT NOT NULL,
                entered_at_utc TEXT NOT NULL,
                FOREIGN KEY(normalized_event_id) REFERENCES normalized_events(id)
            );

            CREATE INDEX IF NOT EXISTS idx_manual_price_entries_event
            ON manual_price_entries(normalized_event_id, id);

            CREATE TABLE IF NOT EXISTS deposit_acquisitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_event_id INTEGER NOT NULL UNIQUE,
                origin_type TEXT NOT NULL CHECK(origin_type IN ('EXTERNAL_PURCHASE', 'OPENING_BALANCE')),
                origin_name TEXT NOT NULL,
                acquired_at_utc TEXT NOT NULL,
                quantity TEXT NOT NULL,
                unit_cost TEXT NOT NULL,
                fee_cost TEXT NOT NULL,
                total_cost TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(currency IN ('KRW', 'USD')),
                note TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                FOREIGN KEY(normalized_event_id) REFERENCES normalized_events(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deposit_acquisitions_origin
            ON deposit_acquisitions(origin_type, normalized_event_id);

            CREATE TABLE IF NOT EXISTS personal_wallet_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_event_id INTEGER NOT NULL UNIQUE,
                wallet_key TEXT NOT NULL,
                wallet_name TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('IN', 'OUT')),
                basis_mode TEXT NOT NULL CHECK(basis_mode IN ('AUTO_CARRY', 'MANUAL_BASIS', 'UNKNOWN')),
                quantity TEXT NOT NULL,
                unit_cost TEXT,
                fee_cost TEXT NOT NULL DEFAULT '0',
                total_cost TEXT,
                currency TEXT NOT NULL CHECK(currency IN ('KRW', 'USD')),
                note TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                FOREIGN KEY(normalized_event_id) REFERENCES normalized_events(id)
            );

            CREATE INDEX IF NOT EXISTS idx_personal_wallet_transfers_wallet
            ON personal_wallet_transfers(wallet_key, normalized_event_id);

            CREATE TABLE IF NOT EXISTS personal_wallet_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_name TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                network_family TEXT NOT NULL CHECK(network_family IN ('EVM', 'SVM', 'BITCOIN', 'OTHER')),
                network_name TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE(network_name, wallet_address)
            );

            CREATE INDEX IF NOT EXISTS idx_personal_wallet_accounts_name
            ON personal_wallet_accounts(LOWER(wallet_name), network_name);

            CREATE TABLE IF NOT EXISTS exchange_wallet_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_code TEXT NOT NULL,
                exchange_name TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                network_family TEXT NOT NULL CHECK(network_family IN ('EVM', 'SVM', 'BITCOIN', 'OTHER')),
                network_name TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE(exchange_code, network_name, wallet_address)
            );

            CREATE INDEX IF NOT EXISTS idx_exchange_wallet_accounts_exchange
            ON exchange_wallet_accounts(exchange_code, network_name, id);

            CREATE INDEX IF NOT EXISTS idx_event_valuations_status
            ON event_valuations(valuation_status, normalized_event_id);

            CREATE TABLE IF NOT EXISTS review_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_event_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                rationale TEXT NOT NULL,
                decided_at_utc TEXT NOT NULL,
                FOREIGN KEY(normalized_event_id) REFERENCES normalized_events(id)
            );

            CREATE TABLE IF NOT EXISTS transfer_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                withdrawal_event_id INTEGER NOT NULL,
                deposit_event_id INTEGER NOT NULL,
                asset TEXT NOT NULL,
                withdrawal_quantity TEXT NOT NULL,
                deposit_quantity TEXT NOT NULL,
                fee_quantity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                rationale TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                cancelled_at_utc TEXT,
                FOREIGN KEY(withdrawal_event_id) REFERENCES normalized_events(id),
                FOREIGN KEY(deposit_event_id) REFERENCES normalized_events(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_matches_active_withdrawal
            ON transfer_matches(withdrawal_event_id)
            WHERE status = 'ACTIVE';

            CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_matches_active_deposit
            ON transfer_matches(deposit_event_id)
            WHERE status = 'ACTIVE';

            CREATE TABLE IF NOT EXISTS report_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_month TEXT NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT,
                output_sha256 TEXT,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                source_row_number INTEGER NOT NULL,
                identity_kind TEXT NOT NULL,
                identity_value TEXT NOT NULL,
                existing_raw_record_id INTEGER,
                existing_raw_json TEXT,
                incoming_raw_json TEXT NOT NULL,
                incoming_row_sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(status IN ('PENDING', 'KEEP_EXISTING', 'ADD_SEPARATE')),
                resolution_note TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                resolved_at_utc TEXT,
                FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE,
                FOREIGN KEY(existing_raw_record_id) REFERENCES raw_records(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_import_conflicts_status
            ON import_conflicts(status, id);

            """
        )
        self.connection.commit()
        self._migrate_source_files()
        self._migrate_source_trash()
        self._migrate_normalized_events()
        self._migrate_event_valuations()

    def _migrate_event_valuations(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(event_valuations)")
        }
        for name in ("native_value", "native_currency"):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE event_valuations ADD COLUMN {name} TEXT"
                )
        self.connection.commit()

    def _migrate_source_trash(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(source_files)")
        }
        if "deleted_at_utc" not in columns:
            self.connection.execute(
                "ALTER TABLE source_files ADD COLUMN deleted_at_utc TEXT"
            )
            self.connection.commit()

    def _migrate_source_files(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(source_files)")
        }
        schema_row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'source_files'"
        ).fetchone()
        schema = schema_row["sql"] if schema_row else ""
        if "document_type" in columns and "'BACKPACK'" in schema:
            return

        document_expression = (
            "document_type" if "document_type" in columns else "'TRANSACTIONS'"
        )
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                f"""
                BEGIN;
                CREATE TABLE source_files_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL CHECK(source_type IN ('UPBIT', 'BITHUMB', 'BACKPACK')),
                    document_type TEXT NOT NULL DEFAULT 'TRANSACTIONS',
                    original_filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    imported_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'RAW_IMPORTED',
                    notes TEXT,
                    UNIQUE(source_type, sha256)
                );
                INSERT INTO source_files_new (
                    id, source_type, document_type, original_filename, stored_path,
                    sha256, byte_size, row_count, imported_at_utc, status, notes
                )
                SELECT
                    id, source_type, {document_expression}, original_filename,
                    stored_path, sha256, byte_size, row_count, imported_at_utc,
                    status, notes
                FROM source_files;
                DROP TABLE source_files;
                ALTER TABLE source_files_new RENAME TO source_files;
                COMMIT;
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError("세무 DB 마이그레이션 후 참조 무결성 오류가 있습니다.")

    def _migrate_normalized_events(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(normalized_events)")
        }
        additions = {
            "network": "TEXT",
            "source_status": "TEXT",
            "is_internal": "INTEGER",
            "subaccount_id": "TEXT",
            "from_address": "TEXT",
            "to_address": "TEXT",
            "market_symbol": "TEXT",
            "instrument_type": "TEXT",
            "side": "TEXT",
            "is_maker": "INTEGER",
            "system_order_type": "TEXT",
            "opened_at_utc": "TEXT",
            "closed_at_utc": "TEXT",
            "realized_pnl": "TEXT",
            "funding_quantity": "TEXT",
            "interest_quantity": "TEXT",
            "trading_fee_quantity": "TEXT",
            "liquidation_fee_quantity": "TEXT",
            "entry_price": "TEXT",
            "closing_price": "TEXT",
            "direction": "TEXT",
            "leverage": "TEXT",
            "position_id": "TEXT",
        }
        for name, data_type in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE normalized_events ADD COLUMN {name} {data_type}"
                )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_normalized_events_raw_record
            ON normalized_events(raw_record_id)
            WHERE raw_record_id IS NOT NULL
            """
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_normalized_events_external
            ON normalized_events(source_type, event_type, external_id)
            WHERE external_id IS NOT NULL
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> TaxStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def dashboard_statistics(self) -> dict[str, int]:
        source_files = self.connection.execute(
            "SELECT COUNT(*) FROM source_files WHERE deleted_at_utc IS NULL"
        ).fetchone()[0]
        raw_records = self.connection.execute(
            """
            SELECT COUNT(*) FROM raw_records rr
            JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE sf.deleted_at_utc IS NULL
            """
        ).fetchone()[0]
        normalized_events = self.connection.execute(
            """
            SELECT COUNT(*) FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL
            """
        ).fetchone()[0]
        needs_review = self.connection.execute(
            """
            SELECT COUNT(*) FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.classification_status = 'NEEDS_REVIEW'
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """
        ).fetchone()[0]
        return {
            "source_files": int(source_files),
            "raw_records": int(raw_records),
            "normalized_events": int(normalized_events),
            "needs_review": int(needs_review),
        }

    def exchange_data_coverage(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT ne.source_type,
                   MIN(ne.occurred_at_utc) AS first_event_at_utc,
                   MAX(ne.occurred_at_utc) AS last_event_at_utc,
                   COUNT(*) AS total_events,
                   SUM(CASE WHEN ne.event_type = 'TRADE_FILL' THEN 1 ELSE 0 END) AS trades,
                   SUM(CASE WHEN ne.event_type IN ('DEPOSIT', 'AIRDROP') THEN 1 ELSE 0 END) AS deposits,
                   SUM(CASE WHEN ne.event_type = 'WITHDRAWAL' THEN 1 ELSE 0 END) AS withdrawals,
                   SUM(CASE WHEN ne.classification_status = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) AS needs_review,
                   SUM(CASE WHEN ne.classification_status = 'NEEDS_REVIEW'
                                  AND (ev.id IS NULL OR ev.valuation_status = 'UNPRICED')
                            THEN 1 ELSE 0 END) AS unpriced,
                   SUM(CASE WHEN ne.classification_status = 'NEEDS_REVIEW'
                                  AND ne.event_type = 'DEPOSIT'
                                  AND ne.asset != 'KRW'
                            THEN 1 ELSE 0 END) AS deposit_basis_pending
            FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL
            GROUP BY ne.source_type
            """
        ).fetchall()
        by_source = {str(row["source_type"]): dict(row) for row in rows}
        return [
            {
                "source_type": source_type,
                "first_event_at_utc": None,
                "last_event_at_utc": None,
                "total_events": 0,
                "trades": 0,
                "deposits": 0,
                "withdrawals": 0,
                "needs_review": 0,
                "unpriced": 0,
                "deposit_basis_pending": 0,
                **by_source.get(source_type, {}),
            }
            for source_type in ("BACKPACK", "UPBIT", "BITHUMB")
        ]

    def review_statistics(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT ne.classification_status, COUNT(*) AS event_count
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL
            GROUP BY ne.classification_status
            """
        ).fetchall()
        counts = {str(row["classification_status"]): int(row["event_count"]) for row in rows}
        pending = counts.get("NEEDS_REVIEW", 0)
        normalized = counts.get("NORMALIZED", 0)
        excluded = counts.get("EXCLUDED", 0)
        resolved = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM normalized_events ne
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE ne.classification_status != 'NEEDS_REVIEW'
                  AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                  AND EXISTS (
                      SELECT 1 FROM review_decisions rd
                      WHERE rd.normalized_event_id = ne.id
                  )
                """
            ).fetchone()[0]
        )
        dust_rows = self.connection.execute(
            f"""
            SELECT ne.classification_status, COUNT(*) AS event_count
            FROM normalized_events ne
            JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
              AND {_DUST_CONDITION_SQL}
            GROUP BY ne.classification_status
            """
        ).fetchall()
        dust_counts = {
            str(row["classification_status"]): int(row["event_count"])
            for row in dust_rows
        }
        dust_resolved = int(
            self.connection.execute(
                f"""
                SELECT COUNT(*)
                FROM normalized_events ne
                JOIN event_valuations ev ON ev.normalized_event_id = ne.id
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE ne.classification_status != 'NEEDS_REVIEW'
                  AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                  AND {_DUST_CONDITION_SQL}
                  AND EXISTS (
                      SELECT 1 FROM review_decisions rd
                      WHERE rd.normalized_event_id = ne.id
                  )
                """
            ).fetchone()[0]
        )
        unpriced_pending = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM normalized_events ne
                LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE ne.classification_status = 'NEEDS_REVIEW'
                  AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                  AND (ev.id IS NULL OR ev.valuation_status = 'UNPRICED')
                """
            ).fetchone()[0]
        )
        unpriced_resolved = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM normalized_events ne
                LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE ne.classification_status != 'NEEDS_REVIEW'
                  AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                  AND (ev.id IS NULL OR ev.valuation_status = 'UNPRICED')
                  AND EXISTS (
                      SELECT 1 FROM review_decisions rd
                      WHERE rd.normalized_event_id = ne.id
                  )
                """
            ).fetchone()[0]
        )
        return {
            "pending": pending,
            "normalized": normalized,
            "excluded": excluded,
            "resolved": resolved,
            "total": sum(counts.values()),
            "dust_pending": dust_counts.get("NEEDS_REVIEW", 0),
            "dust_normalized": dust_counts.get("NORMALIZED", 0),
            "dust_excluded": dust_counts.get("EXCLUDED", 0),
            "dust_resolved": dust_resolved,
            "dust_total": sum(dust_counts.values()),
            "unpriced_pending": unpriced_pending,
            "unpriced_resolved": unpriced_resolved,
        }

    def review_filter_options(self) -> dict[str, list[str]]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT ne.source_type, ne.event_type, ne.asset
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL
            ORDER BY ne.source_type, ne.event_type
            """
        ).fetchall()
        return {
            "sources": sorted({str(row["source_type"]) for row in rows if row["source_type"]}),
            "event_types": sorted({str(row["event_type"]) for row in rows if row["event_type"]}),
            "assets": sorted({str(row["asset"]) for row in rows if row["asset"]}),
        }

    def list_review_events(
        self,
        *,
        event_id: int | None = None,
        status_filter: str = "PENDING",
        source_type: str | None = None,
        event_type: str | None = None,
        asset: str | None = None,
        dust_only: bool = False,
        unpriced_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = ["(ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)"]
        parameters: list[Any] = []
        if event_id is not None:
            conditions.append("ne.id = ?")
            parameters.append(event_id)
        if status_filter == "PENDING":
            conditions.append("ne.classification_status = 'NEEDS_REVIEW'")
        elif status_filter == "RESOLVED":
            conditions.append("ne.classification_status != 'NEEDS_REVIEW'")
            conditions.append(
                "EXISTS (SELECT 1 FROM review_decisions reviewed "
                "WHERE reviewed.normalized_event_id = ne.id)"
            )
        if source_type:
            conditions.append("ne.source_type = ?")
            parameters.append(source_type.upper())
        if event_type:
            conditions.append("ne.event_type = ?")
            parameters.append(event_type.upper())
        if asset:
            conditions.append("ne.asset = ?")
            parameters.append(asset.upper())
        if dust_only:
            conditions.append(_DUST_CONDITION_SQL)
        if unpriced_only:
            conditions.append("(ev.id IS NULL OR ev.valuation_status = 'UNPRICED')")
        where_clause = " AND ".join(conditions)
        total = int(
            self.connection.execute(
                f"""
                SELECT COUNT(*)
                FROM normalized_events ne
                LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE {where_clause}
                """,
                parameters,
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            f"""
            SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.event_type,
                   ne.asset, ne.quote_asset, ne.quantity, ne.unit_price,
                   ne.gross_amount, ne.fee_asset, ne.fee_quantity, ne.network,
                   ne.source_status, ne.is_internal, ne.txid, ne.from_address,
                   ne.to_address, ne.market_symbol, ne.side,
                   ne.classification_status, ne.classification_reason,
                   sf.id AS source_file_id, sf.original_filename,
                   rr.source_row_number, rd.decision AS latest_decision,
                   rd.rationale AS latest_rationale,
                   rd.decided_at_utc AS latest_decided_at_utc,
                   CAST(ev.usd_value AS REAL) AS estimated_usd_value,
                   ev.valuation_status, ev.valuation_method,
                   ev.confidence AS valuation_confidence,
                   ev.reason AS valuation_reason,
                   ev.valued_at_utc, ev.native_value, ev.native_currency,
                   (
                       SELECT pe.source_url
                       FROM event_valuation_evidence eve
                       JOIN price_evidence pe ON pe.id = eve.price_evidence_id
                       WHERE eve.event_valuation_id = ev.id
                       ORDER BY eve.evidence_role, pe.id
                       LIMIT 1
                   ) AS valuation_source_url
            FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            LEFT JOIN review_decisions rd ON rd.id = (
                SELECT latest.id
                FROM review_decisions latest
                WHERE latest.normalized_event_id = ne.id
                ORDER BY latest.id DESC
                LIMIT 1
            )
            WHERE {where_clause}
            ORDER BY
                CASE WHEN ne.classification_status = 'NEEDS_REVIEW' THEN 0 ELSE 1 END,
                ne.occurred_at_utc DESC,
                ne.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, max(1, min(limit, 200)), max(0, offset)],
        ).fetchall()
        return [dict(row) for row in rows], total

    def list_review_decisions(
        self, event_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        history = {event_id: [] for event_id in event_ids}
        if not event_ids:
            return history
        placeholders = ",".join("?" for _ in event_ids)
        rows = self.connection.execute(
            f"""
            SELECT normalized_event_id, decision, rationale, decided_at_utc
            FROM review_decisions
            WHERE normalized_event_id IN ({placeholders})
            ORDER BY id DESC
            """,
            event_ids,
        ).fetchall()
        for row in rows:
            history[int(row["normalized_event_id"])].append(dict(row))
        return history

    def seed_intrinsic_event_valuations(self) -> int:
        """Store valuations that are provable without an external market price."""
        rows = self.connection.execute(
            """
            SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.event_type, ne.asset,
                   ne.quantity, ne.quote_asset, ne.gross_amount, ne.realized_pnl
            FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            WHERE ev.id IS NULL
               OR ev.valuation_status = 'UNPRICED'
               OR (
                    (ne.asset = 'KRW' OR ne.quote_asset = 'KRW')
                    AND COALESCE(ev.valuation_method, '') != 'NATIVE_KRW_AMOUNT'
               )
               OR (
                    ne.source_type = 'BACKPACK'
                    AND ne.event_type = 'AIRDROP'
                    AND ne.asset IN ('USD', 'USDC', 'USDT')
                    AND (ev.native_value IS NULL OR ev.native_currency != 'USD')
               )
            ORDER BY ne.id
            """
        ).fetchall()
        valuations: list[tuple[Any, ...]] = []
        stable_assets = {"USD", "USDC", "USDT"}
        for row in rows:
            amount_text: str | None = None
            value_asset: str | None = None
            method = ""
            if row["gross_amount"] is not None and str(row["quote_asset"] or "").upper() == "KRW":
                amount_text = str(row["gross_amount"])
                value_asset = "KRW"
                method = "NATIVE_KRW_AMOUNT"
            elif row["quantity"] is not None and str(row["asset"] or "").upper() == "KRW":
                amount_text = str(row["quantity"])
                value_asset = "KRW"
                method = "NATIVE_KRW_AMOUNT"
            elif row["gross_amount"] is not None and str(row["quote_asset"] or "").upper() in stable_assets:
                amount_text = str(row["gross_amount"])
                value_asset = str(row["quote_asset"]).upper()
                method = "SOURCE_GROSS_AMOUNT"
            elif row["realized_pnl"] is not None and str(row["quote_asset"] or "").upper() in stable_assets:
                amount_text = str(row["realized_pnl"])
                value_asset = str(row["quote_asset"]).upper()
                method = "SOURCE_REALIZED_PNL"
            elif row["quantity"] is not None and str(row["asset"] or "").upper() in stable_assets:
                amount_text = str(row["quantity"])
                value_asset = str(row["asset"]).upper()
                method = "STABLECOIN_PARITY"
            if amount_text is None or value_asset is None:
                continue
            try:
                usd_value = abs(Decimal(amount_text))
            except InvalidOperation:
                continue
            if value_asset == "KRW":
                usd_value = None
                unit_price_usd = None
                native_value = abs(Decimal(amount_text))
                native_currency = "KRW"
                confidence = "EXACT"
                reason = "원화 금액이 원본에서 확정되어 있습니다. 코인 가격 조회 및 더스트 판정 대상이 아닙니다."
            else:
                unit_price_usd = Decimal("1")
                native_value = abs(Decimal(amount_text))
                native_currency = (
                    "USD"
                    if str(row["source_type"]).upper() == "BACKPACK"
                    else value_asset
                )
                confidence = "EXACT" if value_asset == "USD" else "HIGH"
                reason = (
                    "원본 USD 금액을 사용했습니다."
                    if value_asset == "USD"
                    else f"원본 {value_asset} 금액에 USD 등가 기준을 적용했습니다."
                )
            valuations.append(
                (
                    int(row["id"]),
                    str(usd_value) if usd_value is not None else None,
                    str(unit_price_usd) if unit_price_usd is not None else None,
                    str(native_value), native_currency,
                    method, confidence, reason,
                    row["occurred_at_utc"], datetime.now(UTC).isoformat(),
                )
            )
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO event_valuations (
                    normalized_event_id, valuation_status, usd_value,
                    unit_price_usd, native_value, native_currency,
                    valuation_method, confidence, reason,
                    valued_at_utc, pricing_version, calculated_at_utc
                ) VALUES (?, 'VALUED', ?, ?, ?, ?, ?, ?, ?, ?, 'HISTORICAL_V1', ?)
                ON CONFLICT(normalized_event_id) DO UPDATE SET
                    valuation_status = excluded.valuation_status,
                    usd_value = excluded.usd_value,
                    unit_price_usd = excluded.unit_price_usd,
                    native_value = excluded.native_value,
                    native_currency = excluded.native_currency,
                    valuation_method = excluded.valuation_method,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    valued_at_utc = excluded.valued_at_utc,
                    pricing_version = excluded.pricing_version,
                    calculated_at_utc = excluded.calculated_at_utc
                """,
                valuations,
            )
        self.restore_preapproved_airdrop_basis()
        return len(valuations)

    def restore_preapproved_airdrop_basis(self) -> int:
        """Honor a prior bulk confirmation after an Internal deposit became an airdrop."""
        rows = self.connection.execute(
            """
            SELECT ne.id, ne.asset, ev.native_value, ev.native_currency
            FROM normalized_events ne
            JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            WHERE ne.event_type = 'AIRDROP'
              AND ne.classification_status = 'NEEDS_REVIEW'
              AND ev.valuation_status = 'VALUED'
              AND ev.native_value IS NOT NULL
              AND (
                  (ne.source_type = 'BACKPACK' AND ev.native_currency = 'USD')
                  OR (ne.source_type IN ('UPBIT', 'BITHUMB') AND ev.native_currency = 'KRW')
              )
              AND (
                  SELECT latest.decision
                  FROM review_decisions latest
                  WHERE latest.normalized_event_id = ne.id
                  ORDER BY latest.id DESC LIMIT 1
              ) = 'CONFIRMED'
            ORDER BY ne.id
            """
        ).fetchall()
        if not rows:
            return 0
        decided_at_utc = datetime.now(UTC).isoformat()
        with self.connection:
            for row in rows:
                rationale = (
                    "기존 일괄 정규화 승인 이력을 복구했습니다. "
                    f"저장된 평가액 {row['native_value']} {row['native_currency']}를 "
                    "에어드랍 취득가로 반영했습니다."
                )
                self.connection.execute(
                    """
                    UPDATE normalized_events
                    SET classification_status = 'NORMALIZED',
                        classification_reason = ?
                    WHERE id = ?
                    """,
                    (f"사용자 판정 복구: {rationale}", row["id"]),
                )
                self.connection.execute(
                    """
                    INSERT INTO review_decisions (
                        normalized_event_id, decision, rationale, decided_at_utc
                    ) VALUES (?, 'AIRDROP_BASIS', ?, ?)
                    """,
                    (row["id"], rationale, decided_at_utc),
                )
        return len(rows)

    def list_events_needing_valuation(
        self,
        *,
        source_type: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = [
            "ne.classification_status = 'NEEDS_REVIEW'",
            "(ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)",
            "(ev.id IS NULL OR ev.valuation_status = 'UNPRICED')",
        ]
        parameters: list[Any] = []
        if source_type:
            conditions.append("ne.source_type = ?")
            parameters.append(source_type.upper())
        if event_type:
            conditions.append("ne.event_type = ?")
            parameters.append(event_type.upper())
        rows = self.connection.execute(
            f"""
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE {' AND '.join(conditions)}
            ORDER BY CASE WHEN ev.id IS NULL THEN 0 ELSE 1 END,
                     ne.occurred_at_utc, ne.id
            LIMIT ?
            """,
            [*parameters, max(1, min(limit, 500))],
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_price_evidence(
        self,
        *,
        asset: str,
        quote_asset: str,
        observed_at_utc: str,
        price: Decimal,
        source_name: str,
        source_url: str,
        raw_sha256: str,
        raw_json: str,
    ) -> int:
        self.connection.execute(
            """
            INSERT INTO price_evidence (
                asset, quote_asset, observed_at_utc, price, source_name,
                source_url, raw_sha256, raw_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset, quote_asset, observed_at_utc, source_name)
            DO UPDATE SET price = excluded.price,
                          source_url = excluded.source_url,
                          raw_sha256 = excluded.raw_sha256,
                          raw_json = excluded.raw_json
            """,
            (
                asset.upper(), quote_asset.upper(), observed_at_utc,
                str(price), source_name, source_url, raw_sha256, raw_json,
                datetime.now(UTC).isoformat(),
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM price_evidence
            WHERE asset = ? AND quote_asset = ?
              AND observed_at_utc = ? AND source_name = ?
            """,
            (asset.upper(), quote_asset.upper(), observed_at_utc, source_name),
        ).fetchone()
        assert row is not None
        self.connection.commit()
        return int(row["id"])

    def upsert_event_valuation(
        self,
        *,
        event_id: int,
        valuation_status: str,
        usd_value: Decimal | None,
        unit_price_usd: Decimal | None,
        valuation_method: str,
        confidence: str,
        reason: str,
        valued_at_utc: str | None,
        evidence: list[tuple[int, str]] | None = None,
        native_value: Decimal | None = None,
        native_currency: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO event_valuations (
                    normalized_event_id, valuation_status, usd_value,
                    unit_price_usd, native_value, native_currency,
                    valuation_method, confidence, reason,
                    valued_at_utc, pricing_version, calculated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'HISTORICAL_V1', ?)
                ON CONFLICT(normalized_event_id) DO UPDATE SET
                    valuation_status = excluded.valuation_status,
                    usd_value = excluded.usd_value,
                    unit_price_usd = excluded.unit_price_usd,
                    native_value = excluded.native_value,
                    native_currency = excluded.native_currency,
                    valuation_method = excluded.valuation_method,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    valued_at_utc = excluded.valued_at_utc,
                    pricing_version = excluded.pricing_version,
                    calculated_at_utc = excluded.calculated_at_utc
                """,
                (
                    event_id, valuation_status,
                    str(usd_value) if usd_value is not None else None,
                    str(unit_price_usd) if unit_price_usd is not None else None,
                    str(native_value) if native_value is not None else None,
                    native_currency.upper() if native_currency else None,
                    valuation_method, confidence, reason, valued_at_utc, now,
                ),
            )
            valuation_id = int(
                self.connection.execute(
                    "SELECT id FROM event_valuations WHERE normalized_event_id = ?",
                    (event_id,),
                ).fetchone()["id"]
            )
            self.connection.execute(
                "DELETE FROM event_valuation_evidence WHERE event_valuation_id = ?",
                (valuation_id,),
            )
            self.connection.executemany(
                """
                INSERT INTO event_valuation_evidence (
                    event_valuation_id, price_evidence_id, evidence_role
                ) VALUES (?, ?, ?)
                """,
                [
                    (valuation_id, evidence_id, role)
                    for evidence_id, role in (evidence or [])
                ],
            )

    def set_manual_event_price(
        self,
        event_id: int,
        *,
        unit_price: str,
        note: str,
        entered_at_utc: str,
    ) -> dict[str, Any] | None:
        event = self.connection.execute(
            """
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            return None
        if event["classification_status"] == "EXCLUDED":
            raise ValueError("계산 제외 이벤트는 다시 검토로 되돌린 뒤 가격을 입력하세요.")
        is_airdrop = str(event["event_type"]).upper() == "AIRDROP"
        try:
            price = Decimal(str(unit_price).replace(",", "").strip())
            quantity = abs(Decimal(str(event["quantity"])))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("수량과 자산 1개당 가격을 올바른 숫자로 입력하세요.") from None
        if price < 0 or (price == 0 and not is_airdrop):
            raise ValueError(
                "에어드랍은 무상 취득가 0을 입력할 수 있지만, 일반 이벤트 가격은 0보다 커야 합니다."
            )
        if quantity <= 0:
            raise ValueError("수량이 0보다 큰 이벤트만 가격을 입력할 수 있습니다.")
        note = note.strip()
        if len(note) > 500:
            raise ValueError("가격 입력 메모는 500자 이하로 입력하세요.")
        currency = "USD" if str(event["source_type"]).upper() == "BACKPACK" else "KRW"
        total_value = quantity * price
        reason = (
            f"사용자 수동 입력: 자산 1개당 {price} {currency}, "
            f"총 {total_value} {currency}."
        )
        if note:
            reason = f"{reason} 메모: {note}"
        self.upsert_event_valuation(
            event_id=event_id,
            valuation_status="VALUED",
            usd_value=total_value if currency == "USD" else None,
            unit_price_usd=price if currency == "USD" else None,
            native_value=total_value,
            native_currency=currency,
            valuation_method="USER_MANUAL_UNIT_PRICE",
            confidence="MEDIUM",
            reason=reason,
            valued_at_utc=event["occurred_at_utc"],
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO manual_price_entries (
                    normalized_event_id, unit_price, currency, note, entered_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, str(price), currency, note, entered_at_utc),
            )
            if is_airdrop:
                rationale = (
                    f"에어드랍 취득가를 수동 확정했습니다: "
                    f"1 {event['asset']} = {price} {currency}, 총 {total_value} {currency}."
                )
                if note:
                    rationale = f"{rationale} {note}"
                self.connection.execute(
                    """
                    UPDATE normalized_events
                    SET classification_status = 'NORMALIZED',
                        classification_reason = ?
                    WHERE id = ?
                    """,
                    (f"사용자 판정: {rationale}", event_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO review_decisions (
                        normalized_event_id, decision, rationale, decided_at_utc
                    ) VALUES (?, 'AIRDROP_BASIS', ?, ?)
                    """,
                    (event_id, rationale, entered_at_utc),
                )
        return {**dict(event), "manual_currency": currency, "airdrop_confirmed": is_airdrop}

    def record_deposit_acquisition(
        self,
        event_id: int,
        *,
        origin_type: str,
        origin_name: str,
        acquired_at_utc: str,
        unit_cost: str,
        fee_cost: str,
        note: str,
        recorded_at_utc: str,
    ) -> dict[str, Any] | None:
        event = self.connection.execute(
            """
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            return None
        origin_type = origin_type.upper().strip()
        if origin_type not in {"EXTERNAL_PURCHASE", "OPENING_BALANCE"}:
            raise ValueError("외부 거래소 매수 입금 또는 기초잔고만 취득가를 등록할 수 있습니다.")
        if str(event["event_type"]).upper() != "DEPOSIT":
            raise ValueError("입금 이벤트만 외부 취득 또는 기초잔고로 분류할 수 있습니다.")
        if event["classification_status"] != "NEEDS_REVIEW":
            raise ValueError("검토 대기 중인 입금만 취득 원천을 분류할 수 있습니다.")
        if self.transfer_match_for_event(event_id):
            raise ValueError("이미 내부 이동으로 연결된 입금은 취득 원천을 다시 분류할 수 없습니다.")
        try:
            quantity = abs(Decimal(str(event["quantity"])))
            price = Decimal(str(unit_cost).replace(",", "").strip())
            fee = Decimal(str(fee_cost or "0").replace(",", "").strip())
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("수량, 1개당 취득가와 수수료를 올바른 숫자로 입력하세요.") from None
        if quantity <= 0:
            raise ValueError("입금 수량이 0보다 커야 취득가를 등록할 수 있습니다.")
        if price <= 0:
            raise ValueError("외부 매수·기초잔고의 1개당 취득가는 0보다 커야 합니다.")
        if fee < 0:
            raise ValueError("취득 수수료는 0 이상으로 입력하세요.")

        origin_name = origin_name.strip()
        note = note.strip()
        if origin_type == "EXTERNAL_PURCHASE" and not origin_name:
            raise ValueError("원래 매수한 거래소 또는 지갑 이름을 입력하세요.")
        if not origin_name:
            origin_name = "기초잔고·자료 누락"
        if len(origin_name) > 100:
            raise ValueError("원 취득처 이름은 100자 이하로 입력하세요.")
        if len(note) > 500:
            raise ValueError("취득가 메모는 500자 이하로 입력하세요.")

        try:
            acquired_at = datetime.fromisoformat(
                acquired_at_utc.strip().replace("Z", "+00:00")
            )
            deposit_at = datetime.fromisoformat(
                str(event["occurred_at_utc"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise ValueError("원래 취득 시각과 입금 시각을 확인할 수 없습니다.") from None
        if acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=UTC)
        if deposit_at.tzinfo is None:
            deposit_at = deposit_at.replace(tzinfo=UTC)
        acquired_at = acquired_at.astimezone(UTC)
        deposit_at = deposit_at.astimezone(UTC)
        if acquired_at > deposit_at:
            raise ValueError("원래 취득 시각은 입금 시각보다 늦을 수 없습니다.")

        currency = "USD" if str(event["source_type"]).upper() == "BACKPACK" else "KRW"
        total_cost = quantity * price + fee
        origin_label = (
            "외부 거래소에서 매수 후 입금"
            if origin_type == "EXTERNAL_PURCHASE"
            else "기초잔고 또는 거래자료 누락"
        )
        rationale = (
            f"{origin_label}: {origin_name}, 원 취득 {acquired_at.isoformat()}, "
            f"{quantity} {event['asset']} × {price} {currency}"
        )
        if fee:
            rationale += f", 수수료 {fee} {currency}"
        if note:
            rationale += f". 메모: {note}"

        self.upsert_event_valuation(
            event_id=event_id,
            valuation_status="VALUED",
            usd_value=total_cost if currency == "USD" else None,
            unit_price_usd=price if currency == "USD" else None,
            native_value=total_cost,
            native_currency=currency,
            valuation_method=origin_type,
            confidence="MEDIUM",
            reason=f"사용자 취득가 입력: {rationale}",
            valued_at_utc=event["occurred_at_utc"],
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deposit_acquisitions (
                    normalized_event_id, origin_type, origin_name,
                    acquired_at_utc, quantity, unit_cost, fee_cost,
                    total_cost, currency, note, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_event_id) DO UPDATE SET
                    origin_type = excluded.origin_type,
                    origin_name = excluded.origin_name,
                    acquired_at_utc = excluded.acquired_at_utc,
                    quantity = excluded.quantity,
                    unit_cost = excluded.unit_cost,
                    fee_cost = excluded.fee_cost,
                    total_cost = excluded.total_cost,
                    currency = excluded.currency,
                    note = excluded.note,
                    recorded_at_utc = excluded.recorded_at_utc
                """,
                (
                    event_id, origin_type, origin_name, acquired_at.isoformat(),
                    str(quantity), str(price), str(fee), str(total_cost),
                    currency, note, recorded_at_utc,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO manual_price_entries (
                    normalized_event_id, unit_price, currency, note, entered_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, str(price), currency, rationale, recorded_at_utc),
            )
            self.connection.execute(
                """
                UPDATE normalized_events
                SET classification_status = 'NORMALIZED',
                    classification_reason = ?,
                    instrument_type = COALESCE(
                        instrument_type,
                        CASE WHEN asset LIKE '%.US' THEN 'TOKENIZED_STOCK' ELSE 'SPOT_CRYPTO' END
                    ),
                    is_internal = 0
                WHERE id = ?
                """,
                (f"사용자 판정: {rationale}", event_id),
            )
            self.connection.execute(
                """
                INSERT INTO review_decisions (
                    normalized_event_id, decision, rationale, decided_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                (event_id, origin_type, rationale, recorded_at_utc),
            )
        return {
            **dict(event),
            "origin_type": origin_type,
            "origin_label": origin_label,
            "currency": currency,
            "total_cost": total_cost,
        }

    def list_personal_wallets(self) -> list[dict[str, str]]:
        accounts = self.connection.execute(
            """SELECT id, wallet_name, wallet_address, network_family, network_name, note
            FROM personal_wallet_accounts
            ORDER BY LOWER(wallet_name), network_name, id"""
        ).fetchall()
        rows = self.connection.execute(
            """
            SELECT wallet_key, MAX(wallet_name) AS wallet_name,
                   MAX(wallet_address) AS wallet_address
            FROM personal_wallet_transfers
            GROUP BY wallet_key
            ORDER BY LOWER(MAX(wallet_name)), wallet_key
            """
        ).fetchall()
        result = [dict(row) for row in accounts]
        known = {
            (str(row["wallet_name"]).casefold(), str(row["wallet_address"]).casefold())
            for row in accounts
        }
        for row in rows:
            item = dict(row)
            identity = (str(item["wallet_name"]).casefold(), str(item["wallet_address"]).casefold())
            if identity not in known:
                result.append({
                    "id": None,
                    "wallet_name": item["wallet_name"],
                    "wallet_address": item["wallet_address"],
                    "network_family": "OTHER",
                    "network_name": "기존 판정 이력",
                    "note": "",
                })
        return result

    def personal_wallet_account(self, account_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM personal_wallet_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_personal_wallet_account(
        self,
        *,
        wallet_name: str,
        wallet_address: str,
        network_family: str,
        network_name: str,
        note: str,
        recorded_at_utc: str,
    ) -> int:
        wallet_name = wallet_name.strip()
        wallet_address = wallet_address.strip()
        network_family = network_family.upper().strip()
        network_name = network_name.upper().strip()
        note = note.strip()
        if not wallet_name:
            raise ValueError("지갑 이름을 입력하세요.")
        if not wallet_address:
            raise ValueError("개인 지갑 주소를 입력하세요.")
        if network_family not in {"EVM", "SVM", "BITCOIN", "OTHER"}:
            raise ValueError("EVM, SVM, Bitcoin 또는 기타 네트워크 계열을 선택하세요.")
        if not network_name:
            raise ValueError("지갑이 사용하는 네트워크를 선택하세요.")
        if len(wallet_name) > 100 or len(wallet_address) > 300 or len(note) > 500:
            raise ValueError("지갑 이름·주소·메모 길이를 확인하세요.")
        try:
            cursor = self.connection.execute(
                """INSERT INTO personal_wallet_accounts (
                    wallet_name, wallet_address, network_family, network_name,
                    note, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (wallet_name, wallet_address, network_family, network_name,
                 note, recorded_at_utc, recorded_at_utc),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError("같은 네트워크에 이미 등록된 지갑 주소입니다.") from exc
        return int(cursor.lastrowid)

    def delete_personal_wallet_account(self, account_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM personal_wallet_accounts WHERE id = ?", (account_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def list_exchange_wallet_accounts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id, exchange_code, exchange_name, wallet_address,
                      network_family, network_name, note
               FROM exchange_wallet_accounts
               ORDER BY LOWER(exchange_name), network_name, id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def create_exchange_wallet_account(
        self,
        *,
        exchange_code: str,
        exchange_name: str,
        wallet_address: str,
        network_family: str,
        network_name: str,
        note: str,
        recorded_at_utc: str,
    ) -> int:
        exchange_code = exchange_code.upper().strip()
        exchange_name = exchange_name.strip()
        wallet_address = wallet_address.strip()
        network_family = network_family.upper().strip()
        network_name = network_name.upper().strip()
        note = note.strip()
        if not exchange_code or not exchange_name:
            raise ValueError("거래소를 선택하세요.")
        if not wallet_address:
            raise ValueError("거래소 지갑 주소를 입력하세요.")
        if network_family not in {"EVM", "SVM", "BITCOIN", "OTHER"}:
            raise ValueError("EVM, SVM, Bitcoin 또는 기타 네트워크 계열을 선택하세요.")
        if not network_name:
            raise ValueError("지갑이 사용하는 네트워크를 선택하세요.")
        if len(exchange_name) > 100 or len(wallet_address) > 300 or len(note) > 500:
            raise ValueError("거래소 이름·주소·메모 길이를 확인하세요.")
        try:
            cursor = self.connection.execute(
                """INSERT INTO exchange_wallet_accounts (
                    exchange_code, exchange_name, wallet_address, network_family,
                    network_name, note, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (exchange_code, exchange_name, wallet_address, network_family,
                 network_name, note, recorded_at_utc, recorded_at_utc),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError("해당 거래소와 네트워크에 이미 등록된 지갑 주소입니다.") from exc
        return int(cursor.lastrowid)

    def delete_exchange_wallet_account(self, account_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM exchange_wallet_accounts WHERE id = ?", (account_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def record_personal_wallet_transfer(
        self,
        event_id: int,
        *,
        wallet_name: str,
        wallet_address: str,
        basis_mode: str,
        unit_cost: str,
        fee_cost: str,
        note: str,
        recorded_at_utc: str,
    ) -> dict[str, Any] | None:
        event = self.connection.execute(
            """
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            return None
        event_type = str(event["event_type"]).upper()
        if event_type not in {"DEPOSIT", "WITHDRAWAL"}:
            raise ValueError("입금과 출금만 개인 지갑 이동으로 분류할 수 있습니다.")
        if event["classification_status"] != "NEEDS_REVIEW":
            raise ValueError("검토 대기 중인 입출금만 개인 지갑 이동으로 분류할 수 있습니다.")
        if self.transfer_match_for_event(event_id):
            raise ValueError("이미 다른 입출금과 연결된 이벤트입니다.")

        wallet_name = wallet_name.strip()
        wallet_address = wallet_address.strip()
        note = note.strip()
        if not wallet_name:
            raise ValueError("개인 지갑 이름을 입력하세요.")
        if len(wallet_name) > 100:
            raise ValueError("지갑 이름은 100자 이하로 입력하세요.")
        if len(wallet_address) > 300:
            raise ValueError("지갑 주소는 300자 이하로 입력하세요.")
        if len(note) > 500:
            raise ValueError("지갑 이동 메모는 500자 이하로 입력하세요.")
        if not wallet_address:
            known_wallet = self.connection.execute(
                """
                SELECT wallet_key, wallet_address
                FROM personal_wallet_transfers
                WHERE LOWER(wallet_name) = LOWER(?)
                ORDER BY id DESC LIMIT 1
                """,
                (wallet_name,),
            ).fetchone()
            if known_wallet:
                wallet_address = str(known_wallet["wallet_address"])
                wallet_key = str(known_wallet["wallet_key"])
            else:
                wallet_key = f"NAME:{wallet_name.casefold()}"
        else:
            wallet_key = f"ADDRESS:{wallet_address.casefold()}"
        direction = "IN" if event_type == "DEPOSIT" else "OUT"
        basis_mode = basis_mode.upper().strip()
        if direction == "OUT":
            basis_mode = "AUTO_CARRY"
        elif basis_mode not in {"AUTO_CARRY", "MANUAL_BASIS", "UNKNOWN"}:
            raise ValueError("기존 원가 승계, 취득가 직접 입력 또는 취득가 미확인 중 하나를 선택하세요.")
        try:
            quantity = abs(Decimal(str(event["quantity"])))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("입출금 수량을 올바른 숫자로 확인할 수 없습니다.") from None
        if quantity <= 0:
            raise ValueError("입출금 수량은 0보다 커야 합니다.")

        currency = "USD" if str(event["source_type"]).upper() == "BACKPACK" else "KRW"
        price: Decimal | None = None
        fee = Decimal("0")
        total_cost: Decimal | None = None
        if basis_mode == "MANUAL_BASIS":
            try:
                price = Decimal(str(unit_cost).replace(",", "").strip())
                fee = Decimal(str(fee_cost or "0").replace(",", "").strip())
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError("1개당 취득가와 수수료를 올바른 숫자로 입력하세요.") from None
            if price <= 0:
                raise ValueError("개인 지갑 입금의 직접 취득가는 0보다 커야 합니다.")
            if fee < 0:
                raise ValueError("취득 수수료는 0 이상으로 입력하세요.")
            total_cost = quantity * price + fee
            self.upsert_event_valuation(
                event_id=event_id,
                valuation_status="VALUED",
                usd_value=total_cost if currency == "USD" else None,
                unit_price_usd=price if currency == "USD" else None,
                native_value=total_cost,
                native_currency=currency,
                valuation_method="PERSONAL_WALLET_MANUAL_BASIS",
                confidence="MEDIUM",
                reason=(
                    f"개인 지갑 {wallet_name} 입금 취득가 직접 입력: "
                    f"{quantity} {event['asset']} × {price} {currency}, 총 {total_cost} {currency}."
                ),
                valued_at_utc=event["occurred_at_utc"],
            )

        mode_label = {
            "AUTO_CARRY": "지갑 원장 기존 취득가 승계",
            "MANUAL_BASIS": f"취득가 직접 입력 {total_cost} {currency}",
            "UNKNOWN": "취득가 미확인 유지",
        }[basis_mode]
        rationale = f"개인 지갑 이동: {wallet_name} · {mode_label}"
        if wallet_address:
            rationale += f" · 주소 {wallet_address}"
        if note:
            rationale += f" · 메모 {note}"

        with self.review_transaction():
            self.connection.execute(
                """
                INSERT INTO personal_wallet_transfers (
                    normalized_event_id, wallet_key, wallet_name, wallet_address,
                    direction, basis_mode, quantity, unit_cost, fee_cost,
                    total_cost, currency, note, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_event_id) DO UPDATE SET
                    wallet_key = excluded.wallet_key,
                    wallet_name = excluded.wallet_name,
                    wallet_address = excluded.wallet_address,
                    direction = excluded.direction,
                    basis_mode = excluded.basis_mode,
                    quantity = excluded.quantity,
                    unit_cost = excluded.unit_cost,
                    fee_cost = excluded.fee_cost,
                    total_cost = excluded.total_cost,
                    currency = excluded.currency,
                    note = excluded.note,
                    recorded_at_utc = excluded.recorded_at_utc
                """,
                (
                    event_id, wallet_key, wallet_name, wallet_address, direction,
                    basis_mode, str(quantity), str(price) if price is not None else None,
                    str(fee), str(total_cost) if total_cost is not None else None,
                    currency, note, recorded_at_utc,
                ),
            )
            if price is not None:
                self.connection.execute(
                    """
                    INSERT INTO manual_price_entries (
                        normalized_event_id, unit_price, currency, note, entered_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (event_id, str(price), currency, rationale, recorded_at_utc),
                )
            self.connection.execute(
                """
                UPDATE normalized_events
                SET classification_status = 'NORMALIZED',
                    classification_reason = ?,
                    instrument_type = COALESCE(
                        instrument_type,
                        CASE WHEN asset LIKE '%.US' THEN 'TOKENIZED_STOCK' ELSE 'SPOT_CRYPTO' END
                    ),
                    is_internal = 1
                WHERE id = ?
                """,
                (f"사용자 판정: {rationale}", event_id),
            )
            self.connection.execute(
                """
                INSERT INTO review_decisions (
                    normalized_event_id, decision, rationale, decided_at_utc
                ) VALUES (?, 'PERSONAL_WALLET_TRANSFER', ?, ?)
                """,
                (event_id, rationale, recorded_at_utc),
            )
        return {
            **dict(event),
            "wallet_name": wallet_name,
            "basis_mode": basis_mode,
            "currency": currency,
            "total_cost": total_cost,
        }

    def classify_deposit_as_airdrop(
        self,
        event_id: int,
        *,
        rationale: str,
        decided_at_utc: str,
    ) -> dict[str, Any] | None:
        event = self.connection.execute(
            """
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            return None
        if str(event["event_type"]).upper() != "DEPOSIT":
            raise ValueError("입금 이벤트만 에어드랍으로 분류할 수 있습니다.")
        if event["classification_status"] != "NEEDS_REVIEW":
            raise ValueError("검토 대기 중인 입금만 에어드랍으로 분류할 수 있습니다.")
        active_match = self.connection.execute(
            """
            SELECT 1 FROM transfer_matches
            WHERE status = 'ACTIVE'
              AND (withdrawal_event_id = ? OR deposit_event_id = ?)
            """,
            (event_id, event_id),
        ).fetchone()
        if active_match:
            raise ValueError("내부 이동으로 연결된 입금은 연결을 해제한 뒤 에어드랍으로 분류하세요.")
        rationale = rationale.strip()
        if len(rationale) > 500:
            raise ValueError("검토 메모는 500자 이하로 입력하세요.")
        rationale = rationale or (
            f"{event['source_type']} 입금을 무상 지급된 에어드랍으로 확인했습니다."
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE normalized_events
                SET event_type = 'AIRDROP',
                    instrument_type = CASE
                        WHEN asset LIKE '%.US' THEN 'TOKENIZED_STOCK'
                        ELSE 'SPOT_CRYPTO'
                    END,
                    classification_reason = ?,
                    is_internal = NULL
                WHERE id = ?
                """,
                (f"사용자 분류: {rationale} 취득가를 확인해야 합니다.", event_id),
            )
            self.connection.execute(
                """
                INSERT INTO review_decisions (
                    normalized_event_id, decision, rationale, decided_at_utc
                ) VALUES (?, 'AIRDROP_CLASSIFIED', ?, ?)
                """,
                (event_id, rationale, decided_at_utc),
            )
        return dict(event)

    def confirm_airdrop_basis(
        self,
        event_id: int,
        *,
        rationale: str,
        decided_at_utc: str,
    ) -> dict[str, Any] | None:
        event = self.connection.execute(
            """
            SELECT ne.*, ev.valuation_status, ev.usd_value, ev.native_value,
                   ev.native_currency, ev.valuation_method
            FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            return None
        if str(event["event_type"]).upper() != "AIRDROP":
            raise ValueError("에어드랍 이벤트만 취득가를 승인할 수 있습니다.")
        if event["classification_status"] != "NEEDS_REVIEW":
            raise ValueError("이미 처리된 에어드랍입니다. 다시 검토로 되돌린 후 변경하세요.")
        expected_currency = (
            "USD" if str(event["source_type"]).upper() == "BACKPACK" else "KRW"
        )
        native_value = event["native_value"]
        native_currency = str(event["native_currency"] or "").upper()
        if (
            expected_currency == "USD"
            and event["valuation_status"] == "VALUED"
            and event["usd_value"] is not None
            and (native_value is None or native_currency != "USD")
        ):
            native_value = event["usd_value"]
            native_currency = "USD"
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE event_valuations
                    SET native_value = usd_value, native_currency = 'USD'
                    WHERE normalized_event_id = ?
                    """,
                    (event_id,),
                )
        if (
            event["valuation_status"] != "VALUED"
            or native_value is None
            or native_currency != expected_currency
        ):
            raise ValueError("먼저 거래 시점 가격을 조회하거나 취득가를 수동 입력하세요.")
        rationale = rationale.strip()
        if len(rationale) > 500:
            raise ValueError("검토 메모는 500자 이하로 입력하세요.")
        rationale = rationale or (
            f"저장된 {event['valuation_method']} 가격을 에어드랍 취득가로 승인했습니다. "
            f"총 취득가 {native_value} {expected_currency}."
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE normalized_events
                SET classification_status = 'NORMALIZED',
                    classification_reason = ?
                WHERE id = ?
                """,
                (f"사용자 판정: {rationale}", event_id),
            )
            self.connection.execute(
                """
                INSERT INTO review_decisions (
                    normalized_event_id, decision, rationale, decided_at_utc
                ) VALUES (?, 'AIRDROP_BASIS', ?, ?)
                """,
                (event_id, rationale, decided_at_utc),
            )
        return dict(event)

    def approve_dust_events(
        self,
        *,
        source_type: str | None = None,
        event_type: str | None = None,
        decided_at_utc: str,
    ) -> int:
        return self.approve_review_events(
            collection="DUST",
            source_type=source_type,
            event_type=event_type,
            decided_at_utc=decided_at_utc,
        )

    def approve_review_events(
        self,
        *,
        collection: str,
        source_type: str | None = None,
        event_type: str | None = None,
        decided_at_utc: str,
    ) -> int:
        collection = collection.upper().strip()
        rationale_by_collection = {
            "ALL": "전체 검토 모음에서 일괄 정규화 승인했습니다.",
            "DUST": "더스트 트렌젝션 모음에서 일괄 정규화 승인했습니다.",
            "UNPRICED": "가격 미확인 모음에서 일괄 정규화 승인했습니다.",
        }
        if collection not in rationale_by_collection:
            raise ValueError("지원하지 않는 검토 모음입니다.")
        conditions = [
            "ne.classification_status = 'NEEDS_REVIEW'",
            """(
                COALESCE(ne.event_type, '') != 'AIRDROP'
                OR (
                    ev.valuation_status = 'VALUED'
                    AND (
                        (ne.source_type = 'BACKPACK' AND (ev.native_value IS NOT NULL OR ev.usd_value IS NOT NULL))
                        OR (ne.source_type IN ('UPBIT', 'BITHUMB') AND ev.native_value IS NOT NULL AND ev.native_currency = 'KRW')
                    )
                )
            )""",
            "(ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)",
        ]
        if collection == "DUST":
            conditions.append(_DUST_CONDITION_SQL)
        elif collection == "UNPRICED":
            conditions.append("(ev.id IS NULL OR ev.valuation_status = 'UNPRICED')")
        parameters: list[Any] = []
        if source_type:
            conditions.append("ne.source_type = ?")
            parameters.append(source_type.upper())
        if event_type:
            conditions.append("ne.event_type = ?")
            parameters.append(event_type.upper())
        rows = self.connection.execute(
            f"""
            SELECT ne.id, ne.event_type, ne.source_type,
                   ev.usd_value, ev.native_value, ev.native_currency
            FROM normalized_events ne
            LEFT JOIN event_valuations ev ON ev.normalized_event_id = ne.id
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE {' AND '.join(conditions)}
            ORDER BY ne.id
            """,
            parameters,
        ).fetchall()
        event_ids = [int(row["id"]) for row in rows]
        if not event_ids:
            return 0
        rationale = rationale_by_collection[collection]
        placeholders = ",".join("?" for _ in event_ids)
        with self.connection:
            backpack_airdrop_ids = [
                int(row["id"])
                for row in rows
                if row["event_type"] == "AIRDROP"
                and row["source_type"] == "BACKPACK"
                and row["usd_value"] is not None
                and (
                    row["native_value"] is None
                    or str(row["native_currency"] or "").upper() != "USD"
                )
            ]
            if backpack_airdrop_ids:
                airdrop_placeholders = ",".join("?" for _ in backpack_airdrop_ids)
                self.connection.execute(
                    f"""
                    UPDATE event_valuations
                    SET native_value = usd_value, native_currency = 'USD'
                    WHERE normalized_event_id IN ({airdrop_placeholders})
                    """,
                    backpack_airdrop_ids,
                )
            self.connection.execute(
                f"""
                UPDATE normalized_events
                SET classification_status = 'NORMALIZED',
                    classification_reason = ?
                WHERE id IN ({placeholders})
                  AND classification_status = 'NEEDS_REVIEW'
                """,
                (f"사용자 판정: {rationale}", *event_ids),
            )
            self.connection.executemany(
                """
                INSERT INTO review_decisions (
                    normalized_event_id, decision, rationale, decided_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        int(row["id"]),
                        "AIRDROP_BASIS" if row["event_type"] == "AIRDROP" else "CONFIRMED",
                        (
                            f"{rationale} 저장된 평가액을 에어드랍 취득가로 반영했습니다."
                            if row["event_type"] == "AIRDROP"
                            else rationale
                        ),
                        decided_at_utc,
                    )
                    for row in rows
                ],
            )
        return len(event_ids)

    def transfer_match_for_event(self, event_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT tm.*, withdrawal.source_type AS withdrawal_source_type,
                   withdrawal.occurred_at_utc AS withdrawal_occurred_at_utc,
                   deposit.source_type AS deposit_source_type,
                   deposit.occurred_at_utc AS deposit_occurred_at_utc
            FROM transfer_matches tm
            JOIN normalized_events withdrawal ON withdrawal.id = tm.withdrawal_event_id
            JOIN normalized_events deposit ON deposit.id = tm.deposit_event_id
            WHERE tm.status = 'ACTIVE'
              AND (tm.withdrawal_event_id = ? OR tm.deposit_event_id = ?)
            """,
            (event_id, event_id),
        ).fetchone()
        return dict(row) if row else None

    def list_transfer_candidates(
        self, event_id: int, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        event = self.connection.execute(
            """
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND ne.event_type IN ('DEPOSIT', 'WITHDRAWAL')
              AND ne.classification_status = 'NEEDS_REVIEW'
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM transfer_matches tm
                  WHERE tm.status = 'ACTIVE'
                    AND (tm.withdrawal_event_id = ne.id OR tm.deposit_event_id = ne.id)
              )
            """,
            (event_id,),
        ).fetchone()
        if event is None or not event["asset"] or not event["quantity"]:
            return []

        opposite_type = "WITHDRAWAL" if event["event_type"] == "DEPOSIT" else "DEPOSIT"
        rows = self.connection.execute(
            """
            SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.event_type,
                   ne.asset, ne.quantity, ne.fee_quantity, ne.network, ne.txid,
                   sf.original_filename, rr.source_row_number
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id != ?
              AND ne.event_type = ?
              AND ne.asset = ?
              AND ne.classification_status = 'NEEDS_REVIEW'
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM transfer_matches tm
                  WHERE tm.status = 'ACTIVE'
                    AND (tm.withdrawal_event_id = ne.id OR tm.deposit_event_id = ne.id)
              )
            ORDER BY ne.occurred_at_utc DESC, ne.id DESC
            LIMIT 200
            """,
            (event_id, opposite_type, event["asset"]),
        ).fetchall()

        def decimal_value(value: object) -> Decimal | None:
            try:
                return abs(Decimal(str(value)))
            except (InvalidOperation, ValueError, TypeError):
                return None

        def timestamp(value: object) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)

        current_quantity = decimal_value(event["quantity"])
        current_time = timestamp(event["occurred_at_utc"])
        if current_quantity is None or current_quantity <= 0:
            return []
        candidates: list[dict[str, Any]] = []
        for row in rows:
            candidate_quantity = decimal_value(row["quantity"])
            candidate_time = timestamp(row["occurred_at_utc"])
            if candidate_quantity is None or candidate_quantity <= 0:
                continue
            withdrawal_quantity = (
                candidate_quantity
                if row["event_type"] == "WITHDRAWAL"
                else current_quantity
            )
            deposit_quantity = (
                candidate_quantity
                if row["event_type"] == "DEPOSIT"
                else current_quantity
            )
            if deposit_quantity > withdrawal_quantity:
                continue
            withdrawal_row = row if row["event_type"] == "WITHDRAWAL" else event
            declared_fee = decimal_value(withdrawal_row["fee_quantity"]) or Decimal("0")
            fee_quantity = max(
                withdrawal_quantity - deposit_quantity,
                declared_fee,
            )
            difference_ratio = fee_quantity / max(
                withdrawal_quantity, deposit_quantity + fee_quantity
            )
            same_txid = bool(
                event["txid"]
                and row["txid"]
                and str(event["txid"]).casefold() == str(row["txid"]).casefold()
            )
            if current_time and candidate_time:
                time_difference = abs((current_time - candidate_time).total_seconds())
            else:
                time_difference = float("inf")
            if not same_txid and (time_difference > 7 * 24 * 3600 or difference_ratio > Decimal("0.10")):
                continue
            candidate = dict(row)
            candidate.update(
                {
                    "fee_quantity": str(fee_quantity),
                    "same_txid": same_txid,
                    "time_difference_minutes": None
                    if time_difference == float("inf")
                    else int(time_difference // 60),
                    "basis_currency_compatible": (
                        (event["source_type"] == "BACKPACK")
                        == (row["source_type"] == "BACKPACK")
                    ),
                    "_score": (
                        0 if same_txid else 1,
                        difference_ratio,
                        time_difference,
                    ),
                }
            )
            candidates.append(candidate)
        candidates.sort(key=lambda item: item["_score"])
        for candidate in candidates:
            candidate.pop("_score", None)
        return candidates[: max(1, min(limit, 20))]

    def record_review_decision(
        self,
        event_id: int,
        *,
        decision: str,
        rationale: str,
        decided_at_utc: str,
        counterparty_event_id: int | None = None,
    ) -> dict[str, Any] | None:
        event = self.connection.execute(
            """
            SELECT ne.*
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE ne.id = ?
              AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        if event is None:
            return None

        trusted_krw_transfer = (
            str(event["source_type"]).upper() in {"UPBIT", "BITHUMB"}
            and str(event["event_type"]).upper() in {"DEPOSIT", "WITHDRAWAL"}
            and str(event["asset"]).upper() == "KRW"
        )
        if trusted_krw_transfer:
            raise ValueError(
                "업비트·빗썸 KRW 입출금은 인증된 본인 명의 은행계좌 거래이므로 항상 자동 정규화됩니다."
            )

        decision = decision.upper().strip()
        rationale = rationale.strip()
        if len(rationale) > 500:
            raise ValueError("검토 메모는 500자 이하로 입력하세요.")
        defaults = {
            "CONFIRMED": "원본과 거래 내용을 확인해 정규화 승인했습니다.",
            "INTERNAL_TRANSFER": "본인 소유 지갑·거래소 사이의 내부 이동으로 확인했습니다.",
            "EXCLUDED": "계산 대상이 아닌 이벤트로 확인해 제외했습니다.",
            "REOPENED": "판정을 취소하고 다시 검토하도록 되돌렸습니다.",
        }
        if decision not in defaults:
            raise ValueError("지원하지 않는 검토 판정입니다.")
        if str(event["event_type"]).upper() == "AIRDROP" and decision == "CONFIRMED":
            raise ValueError("에어드랍은 취득가를 조회 또는 직접 입력한 뒤 취득가 승인으로 처리하세요.")
        if decision == "INTERNAL_TRANSFER" and event["event_type"] not in {
            "DEPOSIT",
            "WITHDRAWAL",
        }:
            raise ValueError("입금과 출금만 내부 이동으로 판정할 수 있습니다.")
        if decision != "REOPENED" and event["classification_status"] != "NEEDS_REVIEW":
            raise ValueError("이미 판정된 이벤트입니다. 다시 검토로 되돌린 후 처리하세요.")
        if decision == "REOPENED" and event["classification_status"] == "NEEDS_REVIEW":
            raise ValueError("이미 검토 대기 중인 이벤트입니다.")
        rationale = rationale or defaults[decision]
        status = {
            "CONFIRMED": "NORMALIZED",
            "INTERNAL_TRANSFER": "NORMALIZED",
            "EXCLUDED": "EXCLUDED",
            "REOPENED": "NEEDS_REVIEW",
        }[decision]
        reason = f"사용자 판정: {rationale}"

        with self.review_transaction():
            if decision == "INTERNAL_TRANSFER":
                if counterparty_event_id is None:
                    raise ValueError("연결할 상대 입출금 이벤트를 선택하세요.")
                counterparty = self.connection.execute(
                    """
                    SELECT ne.*
                    FROM normalized_events ne
                    LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                    LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                    WHERE ne.id = ?
                      AND ne.classification_status = 'NEEDS_REVIEW'
                      AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
                    """,
                    (counterparty_event_id,),
                ).fetchone()
                if counterparty is None:
                    raise ValueError("선택한 상대 이벤트를 찾을 수 없거나 이미 판정되었습니다.")
                pair = {event["event_type"], counterparty["event_type"]}
                if pair != {"DEPOSIT", "WITHDRAWAL"}:
                    raise ValueError("입금과 출금 이벤트를 한 쌍으로 선택하세요.")
                if not event["asset"] or event["asset"] != counterparty["asset"]:
                    raise ValueError("동일한 자산의 입금과 출금만 연결할 수 있습니다.")
                already_matched = self.connection.execute(
                    """
                    SELECT 1 FROM transfer_matches
                    WHERE status = 'ACTIVE'
                      AND (
                          withdrawal_event_id IN (?, ?)
                          OR deposit_event_id IN (?, ?)
                      )
                    """,
                    (event_id, counterparty_event_id, event_id, counterparty_event_id),
                ).fetchone()
                if already_matched:
                    raise ValueError("두 이벤트 중 하나가 이미 다른 내부 이동과 연결되어 있습니다.")
                withdrawal = event if event["event_type"] == "WITHDRAWAL" else counterparty
                deposit = event if event["event_type"] == "DEPOSIT" else counterparty
                try:
                    withdrawal_quantity = abs(Decimal(str(withdrawal["quantity"])))
                    deposit_quantity = abs(Decimal(str(deposit["quantity"])))
                except (InvalidOperation, ValueError, TypeError):
                    raise ValueError("입출금 수량을 숫자로 확인할 수 없습니다.") from None
                if withdrawal_quantity <= 0 or deposit_quantity <= 0:
                    raise ValueError("입출금 수량은 0보다 커야 합니다.")
                if deposit_quantity > withdrawal_quantity:
                    raise ValueError("입금 수량이 출금 수량보다 큰 이벤트는 연결할 수 없습니다.")
                try:
                    declared_fee = abs(Decimal(str(withdrawal["fee_quantity"] or "0")))
                except (InvalidOperation, ValueError, TypeError):
                    declared_fee = Decimal("0")
                fee_quantity = max(
                    withdrawal_quantity - deposit_quantity,
                    declared_fee,
                )
                match_reason = (
                    f"{rationale} (출금 #{withdrawal['id']} ↔ 입금 #{deposit['id']})"
                )
                self.connection.execute(
                    """
                    INSERT INTO transfer_matches (
                        withdrawal_event_id, deposit_event_id, asset,
                        withdrawal_quantity, deposit_quantity, fee_quantity,
                        status, rationale, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        withdrawal["id"],
                        deposit["id"],
                        event["asset"],
                        str(withdrawal_quantity),
                        str(deposit_quantity),
                        str(fee_quantity),
                        match_reason,
                        decided_at_utc,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE normalized_events
                    SET classification_status = ?, classification_reason = ?, is_internal = 1
                    WHERE id IN (?, ?)
                    """,
                    (status, f"사용자 판정: {match_reason}", event_id, counterparty_event_id),
                )
                self.connection.executemany(
                    """
                    INSERT INTO review_decisions (
                        normalized_event_id, decision, rationale, decided_at_utc
                    ) VALUES (?, 'INTERNAL_TRANSFER', ?, ?)
                    """,
                    (
                        (event_id, match_reason, decided_at_utc),
                        (counterparty_event_id, match_reason, decided_at_utc),
                    ),
                )
            elif decision == "REOPENED":
                active_match = self.transfer_match_for_event(event_id)
                affected_ids = [event_id]
                if active_match:
                    affected_ids = [
                        int(active_match["withdrawal_event_id"]),
                        int(active_match["deposit_event_id"]),
                    ]
                    self.connection.execute(
                        """
                        UPDATE transfer_matches
                        SET status = 'CANCELLED', cancelled_at_utc = ?
                        WHERE id = ?
                        """,
                        (decided_at_utc, active_match["id"]),
                    )
                placeholders = ",".join("?" for _ in affected_ids)
                self.connection.execute(
                    f"""
                    UPDATE normalized_events
                    SET classification_status = ?, classification_reason = ?, is_internal = NULL
                    WHERE id IN ({placeholders})
                    """,
                    (status, reason, *affected_ids),
                )
                self.connection.executemany(
                    """
                    INSERT INTO review_decisions (
                        normalized_event_id, decision, rationale, decided_at_utc
                    ) VALUES (?, 'REOPENED', ?, ?)
                    """,
                    [(affected_id, rationale, decided_at_utc) for affected_id in affected_ids],
                )
            else:
                self.connection.execute(
                    """
                    UPDATE normalized_events
                    SET classification_status = ?, classification_reason = ?
                    WHERE id = ?
                    """,
                    (status, reason, event_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO review_decisions (
                        normalized_event_id, decision, rationale, decided_at_utc
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (event_id, decision, rationale, decided_at_utc),
                )
        return dict(event)

    def list_source_files(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, source_type, document_type, original_filename, sha256, byte_size,
                   row_count, imported_at_utc, status
            FROM source_files
            WHERE deleted_at_utc IS NULL
            ORDER BY imported_at_utc DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_trashed_source_files(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, source_type, document_type, original_filename, sha256,
                   byte_size, row_count, imported_at_utc, status, deleted_at_utc
            FROM source_files
            WHERE deleted_at_utc IS NOT NULL
            ORDER BY deleted_at_utc DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def source_file(
        self, file_id: int, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        deleted_filter = "" if include_deleted else "AND deleted_at_utc IS NULL"
        row = self.connection.execute(
            f"SELECT * FROM source_files WHERE id = ? {deleted_filter}", (file_id,)
        ).fetchone()
        return dict(row) if row else None

    def move_source_file_to_trash(
        self, file_id: int, *, stored_path: str, deleted_at_utc: str
    ) -> dict[str, Any] | None:
        source = self.source_file(file_id)
        if source is None:
            return None
        event_count = int(
            self.connection.execute(
                """
                SELECT COUNT(1)
                FROM normalized_events ne
                JOIN raw_records rr ON rr.id = ne.raw_record_id
                WHERE rr.source_file_id = ?
                """,
                (file_id,),
            ).fetchone()[0]
        )
        source_event_ids = {
            int(row["id"])
            for row in self.connection.execute(
                """
                SELECT ne.id
                FROM normalized_events ne
                JOIN raw_records rr ON rr.id = ne.raw_record_id
                WHERE rr.source_file_id = ?
                """,
                (file_id,),
            ).fetchall()
        }
        matched_transfers = self.connection.execute(
            """
            SELECT tm.id, tm.withdrawal_event_id, tm.deposit_event_id, tm.status
            FROM transfer_matches tm
            WHERE (
                  tm.withdrawal_event_id IN (
                      SELECT ne.id FROM normalized_events ne
                      JOIN raw_records rr ON rr.id = ne.raw_record_id
                      WHERE rr.source_file_id = ?
                  )
                  OR tm.deposit_event_id IN (
                      SELECT ne.id FROM normalized_events ne
                      JOIN raw_records rr ON rr.id = ne.raw_record_id
                      WHERE rr.source_file_id = ?
                  )
              )
            """,
            (file_id, file_id),
        ).fetchall()
        with self.connection:
            for match in matched_transfers:
                counterpart_ids = {
                    int(match["withdrawal_event_id"]),
                    int(match["deposit_event_id"]),
                } - source_event_ids
                if match["status"] == "ACTIVE":
                    for counterpart_id in counterpart_ids:
                        rationale = "연결된 상대 원본이 휴지통으로 이동되어 다시 검토해야 합니다."
                        self.connection.execute(
                            """
                            UPDATE normalized_events
                            SET classification_status = 'NEEDS_REVIEW',
                                classification_reason = ?, is_internal = NULL
                            WHERE id = ?
                            """,
                            (rationale, counterpart_id),
                        )
                        self.connection.execute(
                            """
                            INSERT INTO review_decisions (
                                normalized_event_id, decision, rationale, decided_at_utc
                            ) VALUES (?, 'REOPENED', ?, ?)
                            """,
                            (counterpart_id, rationale, deleted_at_utc),
                        )
                self.connection.execute(
                    "DELETE FROM transfer_matches WHERE id = ?", (match["id"],)
                )
            self.connection.execute(
                """
                DELETE FROM review_decisions
                WHERE normalized_event_id IN (
                    SELECT ne.id
                    FROM normalized_events ne
                    JOIN raw_records rr ON rr.id = ne.raw_record_id
                    WHERE rr.source_file_id = ?
                )
                """,
                (file_id,),
            )
            self.connection.execute(
                """
                DELETE FROM normalized_events
                WHERE raw_record_id IN (
                    SELECT id FROM raw_records WHERE source_file_id = ?
                )
                """,
                (file_id,),
            )
            self.connection.execute(
                "UPDATE source_files SET stored_path = ?, deleted_at_utc = ? WHERE id = ?",
                (stored_path, deleted_at_utc, file_id),
            )
        return {
            **source,
            "trashed_normalized_events": event_count,
        }

    def restore_source_file(
        self, file_id: int, *, stored_path: str
    ) -> dict[str, Any] | None:
        source = self.source_file(file_id, include_deleted=True)
        if source is None or source["deleted_at_utc"] is None:
            return None
        with self.connection:
            self.connection.execute(
                """
                UPDATE source_files
                SET stored_path = ?, deleted_at_utc = NULL
                WHERE id = ?
                """,
                (stored_path, file_id),
            )
        return source

    def permanently_delete_source_file(self, file_id: int) -> dict[str, Any] | None:
        source = self.source_file(file_id, include_deleted=True)
        if source is None or source["deleted_at_utc"] is None:
            return None
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM review_decisions
                WHERE normalized_event_id IN (
                    SELECT ne.id
                    FROM normalized_events ne
                    JOIN raw_records rr ON rr.id = ne.raw_record_id
                    WHERE rr.source_file_id = ?
                )
                """,
                (file_id,),
            )
            self.connection.execute(
                """
                DELETE FROM normalized_events
                WHERE raw_record_id IN (
                    SELECT id FROM raw_records WHERE source_file_id = ?
                )
                """,
                (file_id,),
            )
            self.connection.execute(
                "DELETE FROM raw_records WHERE source_file_id = ?", (file_id,)
            )
            self.connection.execute("DELETE FROM source_files WHERE id = ?", (file_id,))
        return source

    def list_normalized_events(
        self, limit: int = 500, source_type: str | None = None
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        source_filter = ""
        if source_type is not None:
            source_filter = "AND ne.source_type = ?"
            parameters.append(source_type.upper())
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.event_type,
                   ne.asset, ne.quote_asset, ne.quantity,
                   unit_price, gross_amount, market_symbol, instrument_type, side,
                   fee_asset, fee_quantity, network, source_status,
                   is_internal, txid, from_address, to_address,
                   realized_pnl, funding_quantity, trading_fee_quantity,
                   classification_status, classification_reason
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            {source_filter}
            ORDER BY ne.occurred_at_utc DESC, ne.id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_ledger_events(
        self,
        *,
        source_type: str,
        start_date: str | None = None,
        end_date: str | None = None,
        asset: str | None = None,
        txid: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [
            "(ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)",
            "ne.source_type = ?",
        ]
        parameters: list[Any] = [source_type.upper()]
        if start_date:
            conditions.append("ne.occurred_at_utc >= ?")
            parameters.append(start_date)
        if end_date:
            conditions.append("ne.occurred_at_utc < datetime(?, '+1 day')")
            parameters.append(end_date)
        if asset:
            conditions.append("ne.asset = ?")
            parameters.append(asset.upper())
        if txid:
            conditions.append("ne.txid LIKE ?")
            parameters.append(f"%{txid}%")
        if status:
            conditions.append("ne.classification_status = ?")
            parameters.append(status.upper())
        where_clause = " AND ".join(conditions)
        total = int(self.connection.execute(
            f"""SELECT COUNT(*) FROM normalized_events ne
                LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
                LEFT JOIN source_files sf ON sf.id = rr.source_file_id
                WHERE {where_clause}""",
            parameters,
        ).fetchone()[0])
        rows = self.connection.execute(
            f"""
            SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.event_type,
                   ne.asset, ne.quote_asset, ne.quantity, ne.unit_price,
                   ne.gross_amount, ne.market_symbol, ne.instrument_type, ne.side,
                   ne.fee_asset, ne.fee_quantity, ne.network, ne.source_status,
                   ne.is_internal, ne.txid, ne.from_address, ne.to_address,
                   ne.realized_pnl, ne.funding_quantity, ne.trading_fee_quantity,
                   ne.classification_status, ne.classification_reason,
                   sf.id AS source_file_id, sf.original_filename,
                   rr.source_row_number
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            WHERE {where_clause}
            ORDER BY ne.occurred_at_utc DESC, ne.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        ).fetchall()
        return [dict(row) for row in rows], total

    def ledger_event(self, event_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT ne.*, sf.id AS source_file_id, sf.original_filename,
                   rr.source_row_number, rr.raw_json,
                   rd.decision AS latest_decision, rd.rationale AS latest_rationale,
                   rd.decided_at_utc AS latest_decided_at_utc
            FROM normalized_events ne
            LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
            LEFT JOIN source_files sf ON sf.id = rr.source_file_id
            LEFT JOIN review_decisions rd ON rd.id = (
                SELECT latest.id FROM review_decisions latest
                WHERE latest.normalized_event_id = ne.id
                ORDER BY latest.id DESC LIMIT 1
            )
            WHERE ne.id = ? AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
            """,
            (event_id,),
        ).fetchone()
        return dict(row) if row else None
