from __future__ import annotations

import io
import hashlib
import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pypdf import PdfReader


_ENTRY_START = re.compile(
    r"(?m)^(?P<number>\d+)\s*\n(?=\d{4}-\d{2}-\d{2}\s+(?:매수|매도|입금|출금)\s)"
)
_MAIN_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<transaction_type>매수|매도|입금|출금)\s+"
    r"(?P<asset_name>\S+)\s+"
    r"(?P<quantity>[-\d,.]+)\s+(?P<quantity_asset>\S+)\s+"
    r"(?P<fee>[-\d,.]+)\s+(?P<fee_asset>\S+)"
    r"(?:\s+(?P<counterparty>.*))?$"
)
_TRADE_DETAIL = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<unit_price>[-\d,.]+)\s+(?P<unit_price_asset>\S+)\s+"
    r"(?P<gross_amount>[-\d,.]+)\s+(?P<gross_asset>\S+)\s+"
    r"(?P<settlement_amount>[-\d,.]+)\s+(?P<settlement_asset>\S+)"
    r"(?:\s+(?P<extra>.*))?$"
)
_TRANSFER_DETAIL = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<gross_amount>[-\d,.]+)\s+(?P<gross_asset>\S+)\s+"
    r"(?P<settlement_amount>[-\d,.]+)\s+(?P<settlement_asset>\S+)"
    r"(?:\s+(?P<extra>.*))?$"
)


def _clean_number(value: str) -> str:
    return value.replace(",", "")


def _is_page_decoration(line: str) -> bool:
    return bool(
        re.fullmatch(r"\d+\s*/\s*\d+", line)
        or line == "번호"
        or line.startswith("거래일자 거래유형")
        or line.startswith("거래시간 적요")
    )


def extract_upbit_pdf_text(content: bytes, password: str) -> str:
    if not content:
        raise ValueError("빈 PDF 파일은 가져올 수 없습니다.")
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:
        raise ValueError("PDF 파일을 읽을 수 없습니다.") from exc
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt(password)
        except Exception as exc:
            raise ValueError("업비트 거래내역서 비밀번호가 올바르지 않습니다.") from exc
        if not decrypted:
            raise ValueError("업비트 거래내역서 비밀번호가 올바르지 않습니다.")
    try:
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ValueError("PDF의 거래 표를 읽을 수 없습니다.") from exc
    if "거래내역서" not in text or "거래소 거래내역" not in text:
        raise ValueError("지원하는 업비트 거래내역서 형식이 아닙니다.")
    return text


def parse_upbit_statement_text(text: str) -> list[dict[str, Any]]:
    statement_match = re.search(r"발급번호\s+(\S+)", text)
    statement_number = statement_match.group(1) if statement_match else "UNKNOWN"
    starts = list(_ENTRY_START.finditer(text.replace("\r", "")))
    if not starts:
        raise ValueError("업비트 거래내역서에서 거래 항목을 찾지 못했습니다.")

    records: list[dict[str, Any]] = []
    normalized_text = text.replace("\r", "")
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized_text)
        block = normalized_text[start.start() : end]
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        lines = [line for line in lines if not _is_page_decoration(line)]
        if len(lines) < 3:
            raise ValueError(f"업비트 거래 {start.group('number')}번의 행이 완전하지 않습니다.")

        main = _MAIN_LINE.fullmatch(lines[1])
        if main is None:
            raise ValueError(f"업비트 거래 {start.group('number')}번의 기본 항목을 읽지 못했습니다.")
        transaction_type = main.group("transaction_type")
        detail_pattern = _TRADE_DETAIL if transaction_type in {"매수", "매도"} else _TRANSFER_DETAIL
        detail = detail_pattern.fullmatch(lines[2])
        if detail is None:
            raise ValueError(f"업비트 거래 {start.group('number')}번의 금액 항목을 읽지 못했습니다.")

        values = main.groupdict() | detail.groupdict()
        address_parts = [values.pop("extra", None), *lines[3:]]
        wallet_address = "".join(part.strip() for part in address_parts if part)
        occurred_kst = datetime.fromisoformat(
            f"{values['date']}T{values['time']}"
        ).replace(tzinfo=ZoneInfo("Asia/Seoul"))
        record: dict[str, Any] = {
            "statement_format": "UPBIT_PDF_V1",
            "statement_number": statement_number,
            "transaction_number": start.group("number"),
            "occurred_at_utc": occurred_kst.astimezone(ZoneInfo("UTC")).isoformat(),
            "transaction_type": transaction_type,
            "asset_name": values["asset_name"],
            "quantity": _clean_number(values["quantity"]),
            "quantity_asset": values["quantity_asset"],
            "fee": _clean_number(values["fee"]),
            "fee_asset": values["fee_asset"],
            "counterparty": values.get("counterparty") or None,
            "description": values.get("description") or None,
            "unit_price": _clean_number(values["unit_price"])
            if values.get("unit_price")
            else None,
            "unit_price_asset": values.get("unit_price_asset"),
            "gross_amount": _clean_number(values["gross_amount"]),
            "gross_asset": values["gross_asset"],
            "settlement_amount": _clean_number(values["settlement_amount"]),
            "settlement_asset": values["settlement_asset"],
            "wallet_address": wallet_address or None,
        }
        fingerprint_fields = {
            key: value
            for key, value in record.items()
            if key not in {"statement_number", "transaction_number"}
        }
        record["event_fingerprint"] = hashlib.sha256(
            json.dumps(
                fingerprint_fields, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        records.append(record)
    return records


def parse_upbit_pdf_statement(content: bytes, password: str) -> list[dict[str, Any]]:
    return parse_upbit_statement_text(extract_upbit_pdf_text(content, password))
