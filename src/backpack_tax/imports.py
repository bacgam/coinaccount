from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backpack_tax.bithumb_pdf import parse_bithumb_pdf_statement
from backpack_tax.db import TaxStore
from backpack_tax.upbit_pdf import parse_upbit_pdf_statement


ALLOWED_SOURCES = {"UPBIT", "BITHUMB", "BACKPACK"}
DOCUMENT_TYPES_BY_SOURCE = {
    "UPBIT": {"TRANSACTIONS", "DEPOSITS", "WITHDRAWALS"},
    "BITHUMB": {"TRANSACTIONS", "DEPOSITS", "WITHDRAWALS"},
    "BACKPACK": {
        "BALANCES",
        "DEPOSITS",
        "WITHDRAWALS",
        "TRADES",
        "FUTURES",
        "LENDING",
        "VAULT",
        "SETTLEMENTS",
    },
}


@dataclass(frozen=True)
class ImportResult:
    file_id: int
    source_type: str
    document_type: str
    original_filename: str
    sha256: str
    row_count: int
    created: bool
    retention_mode: str = "ORIGINAL"


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949 파일을 사용하세요.")


def import_exchange_csv(
    store: TaxStore,
    *,
    evidence_directory: Path,
    source_type: str,
    document_type: str = "TRANSACTIONS",
    original_filename: str,
    content: bytes,
) -> ImportResult:
    source_type = source_type.upper().strip()
    document_type = document_type.upper().strip()
    if source_type not in ALLOWED_SOURCES:
        raise ValueError("지원하지 않는 CSV 출처입니다.")
    if document_type not in DOCUMENT_TYPES_BY_SOURCE[source_type]:
        raise ValueError("해당 출처에서 지원하지 않는 자료 종류입니다.")
    if not original_filename.lower().endswith(".csv"):
        raise ValueError(".csv 파일만 업로드할 수 있습니다.")
    if not content:
        raise ValueError("빈 CSV 파일은 업로드할 수 없습니다.")

    digest = hashlib.sha256(content).hexdigest()
    existing = store.connection.execute(
        "SELECT id, row_count FROM source_files WHERE source_type = ? AND sha256 = ?",
        (source_type, digest),
    ).fetchone()
    if existing:
        return ImportResult(
            file_id=int(existing["id"]),
            source_type=source_type,
            document_type=document_type,
            original_filename=original_filename,
            sha256=digest,
            row_count=int(existing["row_count"]),
            created=False,
        )

    text = _decode_csv(content)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or not any(name and name.strip() for name in reader.fieldnames):
        raise ValueError("CSV 헤더를 찾을 수 없습니다.")
    rows = list(reader)
    now = datetime.now(UTC).isoformat()
    evidence_directory = evidence_directory.resolve()
    evidence_directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"{now[:10]}_{uuid.uuid4().hex}_{digest[:12]}.csv"
    stored_path = (evidence_directory / stored_name).resolve()
    if not stored_path.is_relative_to(evidence_directory):
        raise ValueError("안전하지 않은 증빙 파일 경로입니다.")
    stored_path.write_bytes(content)

    try:
        cursor = store.connection.execute(
            """
            INSERT INTO source_files (
                source_type, document_type, original_filename, stored_path, sha256, byte_size,
                row_count, imported_at_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RAW_IMPORTED')
            """,
            (
                source_type,
                document_type,
                Path(original_filename).name,
                str(stored_path),
                digest,
                len(content),
                len(rows),
                now,
            ),
        )
        file_id = int(cursor.lastrowid)
        for row_number, row in enumerate(rows, start=2):
            raw_json = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row_digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            store.connection.execute(
                """
                INSERT INTO raw_records (
                    source_file_id, source_row_number, raw_json, row_sha256,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, row_number, raw_json, row_digest, now),
            )
        store.connection.commit()
    except (sqlite3.Error, Exception):
        store.connection.rollback()
        stored_path.unlink(missing_ok=True)
        raise

    return ImportResult(
        file_id=file_id,
        source_type=source_type,
        document_type=document_type,
        original_filename=Path(original_filename).name,
        sha256=digest,
        row_count=len(rows),
        created=True,
    )


def import_upbit_pdf_statement(
    store: TaxStore,
    *,
    evidence_directory: Path,
    original_filename: str,
    content: bytes,
    password: str,
) -> ImportResult:
    if not original_filename.lower().endswith(".pdf"):
        raise ValueError("업비트 거래내역서는 .pdf 파일이어야 합니다.")
    digest = hashlib.sha256(content).hexdigest()
    existing = store.connection.execute(
        "SELECT id, row_count FROM source_files WHERE source_type = 'UPBIT' AND sha256 = ?",
        (digest,),
    ).fetchone()
    if existing:
        return ImportResult(
            file_id=int(existing["id"]),
            source_type="UPBIT",
            document_type="TRANSACTIONS",
            original_filename=Path(original_filename).name,
            sha256=digest,
            row_count=int(existing["row_count"]),
            created=False,
        )

    records = parse_upbit_pdf_statement(content, password)
    now = datetime.now(UTC).isoformat()
    evidence_directory = evidence_directory.resolve()
    evidence_directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"{now[:10]}_{uuid.uuid4().hex}_{digest[:12]}.pdf"
    stored_path = (evidence_directory / stored_name).resolve()
    if not stored_path.is_relative_to(evidence_directory):
        raise ValueError("안전하지 않은 증빙 파일 경로입니다.")
    stored_path.write_bytes(content)

    try:
        cursor = store.connection.execute(
            """
            INSERT INTO source_files (
                source_type, document_type, original_filename, stored_path,
                sha256, byte_size, row_count, imported_at_utc, status, notes
            ) VALUES (
                'UPBIT', 'TRANSACTIONS', ?, ?, ?, ?, ?, ?,
                'RAW_IMPORTED', 'Upbit encrypted transaction statement PDF; password not stored'
            )
            """,
            (
                Path(original_filename).name,
                str(stored_path),
                digest,
                len(content),
                len(records),
                now,
            ),
        )
        file_id = int(cursor.lastrowid)
        for row_number, record in enumerate(records, start=1):
            raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            row_digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            store.connection.execute(
                """
                INSERT INTO raw_records (
                    source_file_id, source_row_number, raw_json, row_sha256,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, row_number, raw_json, row_digest, now),
            )
        store.connection.commit()
    except Exception:
        store.connection.rollback()
        stored_path.unlink(missing_ok=True)
        raise

    return ImportResult(
        file_id=file_id,
        source_type="UPBIT",
        document_type="TRANSACTIONS",
        original_filename=Path(original_filename).name,
        sha256=digest,
        row_count=len(records),
        created=True,
    )


def import_bithumb_pdf_statement(
    store: TaxStore,
    *,
    evidence_directory: Path,
    original_filename: str,
    content: bytes,
) -> ImportResult:
    if not original_filename.lower().endswith(".pdf"):
        raise ValueError("빗썸 거래내역확인서는 .pdf 파일이어야 합니다.")
    digest = hashlib.sha256(content).hexdigest()
    existing = store.connection.execute(
        """
        SELECT id, row_count, original_filename
        FROM source_files
        WHERE source_type = 'BITHUMB' AND sha256 = ?
        """,
        (digest,),
    ).fetchone()
    if existing:
        return ImportResult(
            file_id=int(existing["id"]),
            source_type="BITHUMB",
            document_type="TRANSACTIONS",
            original_filename=existing["original_filename"],
            sha256=digest,
            row_count=int(existing["row_count"]),
            created=False,
            retention_mode="SANITIZED_ONLY",
        )

    records = parse_bithumb_pdf_statement(content)
    now = datetime.now(UTC).isoformat()
    safe_filename = f"bithumb-transactions-{now[:10]}-{digest[:12]}.sanitized.json"
    sanitized_evidence = json.dumps(
        {
            "evidence_format": "BITHUMB_SANITIZED_EVIDENCE_V1",
            "source_document_sha256": digest,
            "privacy": {
                "original_pdf_retained": False,
                "personal_information_retained": False,
                "account_numbers_retained": False,
                "free_text_memos_retained": False,
            },
            "transactions": records,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sanitized_digest = hashlib.sha256(sanitized_evidence).hexdigest()
    evidence_directory = evidence_directory.resolve()
    evidence_directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"{now[:10]}_{uuid.uuid4().hex}_{digest[:12]}.sanitized.json"
    stored_path = (evidence_directory / stored_name).resolve()
    if not stored_path.is_relative_to(evidence_directory):
        raise ValueError("안전하지 않은 증빙 파일 경로입니다.")
    stored_path.write_bytes(sanitized_evidence)

    try:
        cursor = store.connection.execute(
            """
            INSERT INTO source_files (
                source_type, document_type, original_filename, stored_path,
                sha256, byte_size, row_count, imported_at_utc, status, notes
            ) VALUES (
                'BITHUMB', 'TRANSACTIONS', ?, ?, ?, ?, ?, ?,
                'SANITIZED_IMPORTED', ?
            )
            """,
            (
                safe_filename,
                str(stored_path),
                digest,
                len(content),
                len(records),
                now,
                "Bithumb PDF parsed in memory; original and personal information discarded; "
                f"sanitized evidence sha256={sanitized_digest}",
            ),
        )
        file_id = int(cursor.lastrowid)
        for row_number, record in enumerate(records, start=1):
            raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            row_digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            store.connection.execute(
                """
                INSERT INTO raw_records (
                    source_file_id, source_row_number, raw_json, row_sha256,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, row_number, raw_json, row_digest, now),
            )
        store.connection.commit()
    except Exception:
        store.connection.rollback()
        stored_path.unlink(missing_ok=True)
        raise

    return ImportResult(
        file_id=file_id,
        source_type="BITHUMB",
        document_type="TRANSACTIONS",
        original_filename=safe_filename,
        sha256=digest,
        row_count=len(records),
        created=True,
        retention_mode="SANITIZED_ONLY",
    )


def import_backpack_settlement_snapshot(
    store: TaxStore,
    *,
    evidence_directory: Path,
    records: list[dict[str, object]],
) -> ImportResult:
    content = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    existing = store.connection.execute(
        "SELECT id, row_count, original_filename FROM source_files WHERE source_type = 'BACKPACK' AND sha256 = ?",
        (digest,),
    ).fetchone()
    if existing:
        return ImportResult(
            file_id=int(existing["id"]),
            source_type="BACKPACK",
            document_type="SETTLEMENTS",
            original_filename=existing["original_filename"],
            sha256=digest,
            row_count=int(existing["row_count"]),
            created=False,
        )

    now = datetime.now(UTC).isoformat()
    evidence_directory = evidence_directory.resolve()
    evidence_directory.mkdir(parents=True, exist_ok=True)
    original_filename = f"backpack-settlements-{now[:10]}.json"
    stored_name = f"{now[:10]}_{uuid.uuid4().hex}_{digest[:12]}.json"
    stored_path = (evidence_directory / stored_name).resolve()
    if not stored_path.is_relative_to(evidence_directory):
        raise ValueError("안전하지 않은 증빙 파일 경로입니다.")
    stored_path.write_bytes(content)

    try:
        cursor = store.connection.execute(
            """
            INSERT INTO source_files (
                source_type, document_type, original_filename, stored_path,
                sha256, byte_size, row_count, imported_at_utc, status, notes
            ) VALUES (
                'BACKPACK', 'SETTLEMENTS', ?, ?, ?, ?, ?, ?,
                'RAW_IMPORTED', 'Backpack signed read-only API snapshot'
            )
            """,
            (
                original_filename,
                str(stored_path),
                digest,
                len(content),
                len(records),
                now,
            ),
        )
        file_id = int(cursor.lastrowid)
        for row_number, record in enumerate(records, start=1):
            raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            row_digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            store.connection.execute(
                """
                INSERT INTO raw_records (
                    source_file_id, source_row_number, raw_json, row_sha256,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, row_number, raw_json, row_digest, now),
            )
        store.connection.commit()
    except Exception:
        store.connection.rollback()
        stored_path.unlink(missing_ok=True)
        raise

    return ImportResult(
        file_id=file_id,
        source_type="BACKPACK",
        document_type="SETTLEMENTS",
        original_filename=original_filename,
        sha256=digest,
        row_count=len(records),
        created=True,
    )
