from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pypdf import PdfReader


_DATE_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAIN_LINE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<asset>[A-Z0-9.]+)\s+"
    r"(?P<transaction_type>매수|매도|입금|출금)\s+"
    r"(?P<quantity>[-+\d,.]+)$"
)
_NUMBER_LINE = re.compile(r"^[-+]?\d[\d,]*(?:\.\d+)?$")
_UNIT_AND_DETAIL = re.compile(r"^(?P<unit>[A-Z0-9.]+)(?:\s+(?P<detail>.+))?$")


def _clean_number(value: str) -> str:
    return value.replace(",", "")


def _fee(gross_amount: str, settlement_amount: str) -> str:
    gross = abs(Decimal(_clean_number(gross_amount)))
    settlement = abs(Decimal(_clean_number(settlement_amount)))
    return format(abs(settlement - gross), "f")


def extract_bithumb_pdf_text(content: bytes) -> list[str]:
    if not content:
        raise ValueError("빈 PDF 파일은 가져올 수 없습니다.")
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:
        raise ValueError("빗썸 거래내역확인서 PDF를 읽을 수 없습니다.") from exc
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception as exc:
            raise ValueError("비밀번호가 없는 빗썸 거래내역확인서가 아닙니다.") from exc
        if not decrypted:
            raise ValueError("비밀번호가 설정된 PDF는 빗썸 자료로 가져올 수 없습니다.")
    try:
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ValueError("빗썸 PDF의 거래 표를 읽을 수 없습니다.") from exc
    first_page = pages[0].replace("\x01", " ") if pages else ""
    if "거래내역 확인서" not in " ".join(first_page.split()):
        raise ValueError("지원하는 빗썸 거래내역확인서 형식이 아닙니다.")
    return pages


def parse_bithumb_statement_pages(pages: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    duplicate_counts: Counter[str] = Counter()

    for page_number, text in enumerate(pages, start=1):
        lines = [line.strip() for line in text.replace("\x01", " ").splitlines() if line.strip()]
        starts = [
            index
            for index, line in enumerate(lines[:-1])
            if _DATE_LINE.fullmatch(line) and _MAIN_LINE.fullmatch(lines[index + 1])
        ]
        for block_index, start in enumerate(starts):
            end = starts[block_index + 1] if block_index + 1 < len(starts) else len(lines)
            main = _MAIN_LINE.fullmatch(lines[start + 1])
            if main is None:
                continue
            transaction_type = main.group("transaction_type")
            expected_pairs = 5 if transaction_type in {"매수", "매도"} else 3
            body = lines[start + 2 : end]
            if not body or body[0] != main.group("asset"):
                raise ValueError(
                    f"빗썸 PDF {page_number}페이지 거래 행의 수량 단위를 읽지 못했습니다."
                )

            pairs: list[tuple[str, str]] = []
            detail = ""
            offset = 1
            while offset + 1 < len(body) and len(pairs) < expected_pairs:
                if not _NUMBER_LINE.fullmatch(body[offset]):
                    break
                unit_match = _UNIT_AND_DETAIL.fullmatch(body[offset + 1])
                if unit_match is None:
                    break
                pairs.append((_clean_number(body[offset]), unit_match.group("unit")))
                detail = unit_match.group("detail") or detail
                offset += 2
            if len(pairs) != expected_pairs:
                raise ValueError(
                    f"빗썸 PDF {page_number}페이지 거래 행의 금액 항목이 완전하지 않습니다."
                )

            occurred_kst = datetime.fromisoformat(
                f"{lines[start]}T{main.group('time')}"
            ).replace(tzinfo=ZoneInfo("Asia/Seoul"))
            record: dict[str, Any] = {
                "statement_format": "BITHUMB_PDF_V1",
                "occurred_at_utc": occurred_kst.astimezone(ZoneInfo("UTC")).isoformat(),
                "transaction_type": transaction_type,
                "asset": main.group("asset"),
                "quantity": _clean_number(main.group("quantity")),
                "quantity_asset": body[0],
            }
            if transaction_type in {"매수", "매도"}:
                record.update(
                    {
                        "unit_price": pairs[0][0],
                        "unit_price_asset": pairs[0][1],
                        "gross_amount": pairs[1][0],
                        "gross_asset": pairs[1][1],
                        "settlement_amount": pairs[2][0],
                        "settlement_asset": pairs[2][1],
                        "asset_balance": pairs[3][0],
                        "asset_balance_unit": pairs[3][1],
                        "krw_balance": pairs[4][0],
                        "fee": _fee(pairs[1][0], pairs[2][0]),
                        "fee_asset": pairs[2][1],
                        "is_maker": "maker" in detail.lower(),
                        "order_type": "LIMIT" if "지정가" in detail else "MARKET" if "시장가" in detail else None,
                    }
                )
            else:
                record.update(
                    {
                        "gross_amount": pairs[0][0],
                        "gross_asset": pairs[0][1],
                        "settlement_amount": pairs[1][0],
                        "settlement_asset": pairs[1][1],
                        "asset_balance": pairs[2][0],
                        "asset_balance_unit": pairs[2][1],
                        "fee": _fee(pairs[0][0], pairs[1][0]),
                        "fee_asset": pairs[1][1],
                    }
                )

            fingerprint_source = json.dumps(record, ensure_ascii=False, sort_keys=True)
            duplicate_index = duplicate_counts[fingerprint_source]
            duplicate_counts[fingerprint_source] += 1
            record["event_fingerprint"] = hashlib.sha256(
                f"{fingerprint_source}|{duplicate_index}".encode("utf-8")
            ).hexdigest()
            records.append(record)

    if not records:
        raise ValueError("빗썸 거래내역확인서에서 거래 항목을 찾지 못했습니다.")
    return records


def parse_bithumb_pdf_statement(content: bytes) -> list[dict[str, Any]]:
    return parse_bithumb_statement_pages(extract_bithumb_pdf_text(content))
