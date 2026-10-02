from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from typing import Any
from datetime import UTC, datetime

from backpack_tax.bithumb_pdf import parse_bithumb_pdf_statement
from backpack_tax.db import TaxStore
from backpack_tax.imports import _decode_csv
from backpack_tax.upbit_pdf import parse_upbit_pdf_statement


@dataclass(frozen=True)
class PreviewAnalysis:
    total: int
    new: int
    duplicate: int
    conflict: int
    duplicate_rows: tuple[int, ...]
    conflict_rows: tuple[int, ...]
    conflict_samples: tuple[dict[str, str], ...]
    conflict_details: tuple[dict[str, Any], ...]


def parse_upload_records(
    *, source_type: str, filename: str, content: bytes, password: str = ""
) -> tuple[list[dict[str, Any]], int]:
    if source_type == "UPBIT":
        return parse_upbit_pdf_statement(content, password), 1
    if source_type == "BITHUMB":
        return parse_bithumb_pdf_statement(content), 1
    text = _decode_csv(content)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise ValueError("CSV 헤더를 찾을 수 없습니다.")
    return list(reader), 2


def _canonical(record: dict[str, Any]) -> tuple[str, str]:
    raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
    return raw_json, hashlib.sha256(raw_json.encode("utf-8")).hexdigest()


def _identity(record: dict[str, Any]) -> tuple[str, str] | None:
    for key, value in record.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized in {
            "tradeid", "orderid", "txid", "transactionid", "uuid",
            "depositid", "withdrawalid", "externalid",
        } and value not in (None, ""):
            return normalized, str(value).strip()
    return None


def analyze_records(
    store: TaxStore,
    *,
    source_type: str,
    records: list[dict[str, Any]],
    first_row_number: int,
) -> PreviewAnalysis:
    existing_rows = store.connection.execute(
        """
        SELECT rr.id, rr.raw_json, rr.row_sha256
        FROM raw_records rr JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE sf.source_type = ? AND sf.deleted_at_utc IS NULL
        """,
        (source_type,),
    ).fetchall()
    hashes = {str(row["row_sha256"]) for row in existing_rows}
    identities: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_rows:
        raw_json = str(row["raw_json"])
        identity = _identity(json.loads(raw_json))
        if identity:
            identities[identity] = {
                "digest": hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                "raw_record_id": int(row["id"]),
                "raw_json": raw_json,
            }

    duplicate_rows: list[int] = []
    conflict_rows: list[int] = []
    samples: list[dict[str, str]] = []
    details: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_identities: dict[tuple[str, str], str] = {}
    for index, record in enumerate(records):
        row_number = first_row_number + index
        _raw_json, digest = _canonical(record)
        identity = _identity(record)
        if digest in hashes or digest in seen_hashes:
            duplicate_rows.append(row_number)
        elif identity and (
            (identity in identities and identities[identity]["digest"] != digest)
            or (identity in seen_identities and seen_identities[identity] != digest)
        ):
            conflict_rows.append(row_number)
            prior = identities.get(identity)
            details.append({
                "row": row_number,
                "identity_kind": identity[0],
                "identity_value": identity[1],
                "existing_raw_record_id": prior["raw_record_id"] if prior else None,
                "existing_raw_json": prior["raw_json"] if prior else None,
                "incoming_raw_json": _raw_json,
                "incoming_row_sha256": digest,
            })
            if len(samples) < 10:
                samples.append({
                    "row": str(row_number),
                    "key": identity[0],
                    "value": identity[1],
                })
        seen_hashes.add(digest)
        if identity:
            seen_identities[identity] = digest
    skipped = len(duplicate_rows) + len(conflict_rows)
    return PreviewAnalysis(
        total=len(records),
        new=len(records) - skipped,
        duplicate=len(duplicate_rows),
        conflict=len(conflict_rows),
        duplicate_rows=tuple(duplicate_rows),
        conflict_rows=tuple(conflict_rows),
        conflict_samples=tuple(samples),
        conflict_details=tuple(details),
    )


def keep_new_rows_only(
    store: TaxStore, *, file_id: int, skipped_rows: tuple[int, ...]
) -> int:
    if skipped_rows:
        placeholders = ",".join("?" for _ in skipped_rows)
        store.connection.execute(
            f"DELETE FROM raw_records WHERE source_file_id = ? AND source_row_number IN ({placeholders})",
            (file_id, *skipped_rows),
        )
    remaining = int(store.connection.execute(
        "SELECT COUNT(*) FROM raw_records WHERE source_file_id = ?", (file_id,)
    ).fetchone()[0])
    store.connection.execute(
        "UPDATE source_files SET row_count = ? WHERE id = ?", (remaining, file_id)
    )
    store.connection.commit()
    return remaining


def save_import_conflicts(
    store: TaxStore,
    *,
    file_id: int,
    source_type: str,
    original_filename: str,
    details: tuple[dict[str, Any], ...],
) -> int:
    now = datetime.now(UTC).isoformat()
    for detail in details:
        store.connection.execute(
            """
            INSERT INTO import_conflicts (
                source_file_id, source_type, original_filename, source_row_number,
                identity_kind, identity_value, existing_raw_record_id,
                existing_raw_json, incoming_raw_json, incoming_row_sha256,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id, source_type, original_filename, detail["row"],
                detail["identity_kind"], detail["identity_value"],
                detail["existing_raw_record_id"], detail["existing_raw_json"],
                detail["incoming_raw_json"], detail["incoming_row_sha256"], now,
            ),
        )
    store.connection.commit()
    return len(details)


def conflict_field_differences(existing_json: str | None, incoming_json: str) -> list[dict[str, str]]:
    existing = json.loads(existing_json) if existing_json else {}
    incoming = json.loads(incoming_json)
    keys = list(dict.fromkeys([*existing.keys(), *incoming.keys()]))
    return [
        {
            "field": str(key),
            "existing": "" if existing.get(key) is None else str(existing.get(key)),
            "incoming": "" if incoming.get(key) is None else str(incoming.get(key)),
        }
        for key in keys
        if existing.get(key) != incoming.get(key)
    ]
