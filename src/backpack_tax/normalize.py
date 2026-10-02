from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from backpack_tax.db import TaxStore


@dataclass(frozen=True)
class NormalizationResult:
    created: int
    skipped: int


def _text(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _decimal_text(value: object) -> str | None:
    result = _text(value)
    if result is None:
        return None
    try:
        Decimal(result.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"숫자로 해석할 수 없는 값입니다: {result}") from exc
    return result.replace(",", "")


def _utc_timestamp(value: object) -> str | None:
    result = _text(value)
    if result is None:
        return None
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _boolean(value: object) -> int | None:
    result = (_text(value) or "").lower()
    if not result:
        return None
    if result in {"true", "1", "yes"}:
        return 1
    if result in {"false", "0", "no"}:
        return 0
    raise ValueError(f"참/거짓으로 해석할 수 없는 값입니다: {result}")


def _product(left: object, right: object) -> str | None:
    left_text = _decimal_text(left)
    right_text = _decimal_text(right)
    if left_text is None or right_text is None:
        return None
    return format(Decimal(left_text) * Decimal(right_text), "f")


def _market_parts(symbol_value: object) -> tuple[str | None, str | None, str]:
    symbol = _text(symbol_value)
    if symbol is None:
        return None, None, "UNKNOWN"
    if symbol.endswith("_PERP"):
        clean = symbol.removesuffix("_PERP")
        instrument_type = "PERPETUAL_FUTURE"
    elif symbol.endswith("_RFQ"):
        clean = symbol.removesuffix("_RFQ")
        instrument_type = "TOKENIZED_STOCK" if ".US_" in clean else "RFQ_SPOT"
    else:
        clean = symbol
        instrument_type = "SPOT_CRYPTO"
    if "_" not in clean:
        return clean, None, instrument_type
    base, quote = clean.rsplit("_", 1)
    return base, quote, instrument_type


def normalize_backpack_transfers(store: TaxStore) -> NormalizationResult:
    rows = store.connection.execute(
        """
        SELECT rr.id AS raw_record_id, rr.raw_json, sf.document_type
        FROM raw_records rr
        JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN normalized_events ne ON ne.raw_record_id = rr.id
        WHERE sf.source_type = 'BACKPACK'
          AND sf.deleted_at_utc IS NULL
          AND sf.document_type IN ('DEPOSITS', 'WITHDRAWALS')
          AND ne.id IS NULL
        ORDER BY rr.id
        """
    ).fetchall()
    created = 0
    skipped = 0
    now = datetime.now(UTC).isoformat()

    for row in rows:
        raw = json.loads(row["raw_json"])
        document_type = row["document_type"]
        status = _text(raw.get("status"))
        confirmed = (status or "").lower() == "confirmed"
        event_type = "DEPOSIT" if document_type == "DEPOSITS" else "WITHDRAWAL"
        network = _text(raw.get("source") if event_type == "DEPOSIT" else raw.get("blockchain"))
        is_airdrop = event_type == "DEPOSIT" and str(network or "").lower() == "internal"
        if is_airdrop:
            event_type = "AIRDROP"
        from_address = _text(raw.get("from_address"))
        to_address = _text(raw.get("to_address"))
        is_internal = _boolean(raw.get("is_internal")) if event_type == "WITHDRAWAL" else None
        fee = _decimal_text(raw.get("fee")) if event_type == "WITHDRAWAL" else None
        classification_status = "NEEDS_REVIEW" if confirmed else "EXCLUDED"
        if confirmed and is_airdrop:
            reason = "Backpack Internal 입금은 에어드랍으로 자동 분류했습니다. 취득가를 확인해야 합니다."
        elif confirmed:
            reason = "확정된 입출금입니다. 본인 지갑·거래소 간 이동 여부를 확인해야 합니다."
        else:
            reason = f"원본 상태가 {status or '없음'}이므로 계산 대상에서 임시 제외했습니다."

        cursor = store.connection.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                raw_record_id, source_type, occurred_at_utc, event_type,
                asset, quantity, fee_asset, fee_quantity, external_id, txid,
                wallet_address, classification_status, classification_reason,
                created_at_utc, network, source_status, is_internal,
                subaccount_id, from_address, to_address, instrument_type
            ) VALUES (?, 'BACKPACK', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["raw_record_id"],
                _utc_timestamp(raw.get("created_at")),
                event_type,
                _text(raw.get("symbol")),
                _decimal_text(raw.get("quantity")),
                _text(raw.get("symbol")) if fee is not None else None,
                fee,
                _text(raw.get("id")),
                _text(raw.get("transaction_hash")),
                from_address if event_type == "DEPOSIT" else to_address,
                classification_status,
                reason,
                now,
                network,
                status,
                is_internal,
                _text(raw.get("subaccount_id")),
                from_address,
                to_address,
                "TOKENIZED_STOCK" if str(raw.get("symbol") or "").upper().endswith(".US") else "SPOT_CRYPTO",
            ),
        )
        created += cursor.rowcount

    store.connection.commit()
    return NormalizationResult(created=created, skipped=skipped)


def normalize_backpack_trades(store: TaxStore) -> NormalizationResult:
    rows = store.connection.execute(
        """
        SELECT rr.id AS raw_record_id, rr.raw_json
        FROM raw_records rr
        JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN normalized_events ne ON ne.raw_record_id = rr.id
        WHERE sf.source_type = 'BACKPACK'
          AND sf.deleted_at_utc IS NULL
          AND sf.document_type = 'TRADES'
          AND ne.id IS NULL
        ORDER BY rr.id
        """
    ).fetchall()
    created = 0
    now = datetime.now(UTC).isoformat()
    for row in rows:
        raw = json.loads(row["raw_json"])
        symbol = _text(raw.get("symbol"))
        asset, quote_asset, instrument_type = _market_parts(symbol)
        is_bid = _boolean(raw.get("is_bid"))
        side = "BUY" if is_bid == 1 else "SELL"
        system_order_type = _text(raw.get("system_order_type"))
        needs_review = system_order_type not in {None, "USER"}
        reason = (
            f"시스템 체결 유형 {system_order_type}의 세무 처리를 확인해야 합니다."
            if needs_review
            else "Backpack 체결 원본에서 자동 분류했습니다."
        )
        cursor = store.connection.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                raw_record_id, source_type, occurred_at_utc, event_type,
                asset, quantity, quote_asset, unit_price, gross_amount,
                fee_asset, fee_quantity, order_id, trade_id,
                classification_status, classification_reason, created_at_utc,
                subaccount_id, market_symbol, instrument_type, side,
                is_maker, system_order_type
            ) VALUES (
                ?, 'BACKPACK', ?, 'TRADE_FILL', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["raw_record_id"],
                _utc_timestamp(raw.get("timestamp")),
                asset,
                _decimal_text(raw.get("quantity")),
                quote_asset,
                _decimal_text(raw.get("price")),
                _product(raw.get("price"), raw.get("quantity")),
                _text(raw.get("fee_asset")),
                _decimal_text(raw.get("fee")),
                _text(raw.get("order_id")),
                _text(raw.get("trade_id")),
                "NEEDS_REVIEW" if needs_review else "NORMALIZED",
                reason,
                now,
                _text(raw.get("subaccount_id")),
                symbol,
                instrument_type,
                side,
                _boolean(raw.get("is_maker")),
                system_order_type,
            ),
        )
        created += cursor.rowcount
    store.connection.commit()
    return NormalizationResult(created=created, skipped=0)


def normalize_backpack_futures(store: TaxStore) -> NormalizationResult:
    rows = store.connection.execute(
        """
        SELECT rr.id AS raw_record_id, rr.raw_json
        FROM raw_records rr
        JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN normalized_events ne ON ne.raw_record_id = rr.id
        WHERE sf.source_type = 'BACKPACK'
          AND sf.deleted_at_utc IS NULL
          AND sf.document_type = 'FUTURES'
          AND ne.id IS NULL
        ORDER BY rr.id
        """
    ).fetchall()
    created = 0
    now = datetime.now(UTC).isoformat()
    for row in rows:
        raw = json.loads(row["raw_json"])
        symbol = _text(raw.get("market_symbol"))
        asset, quote_asset, instrument_type = _market_parts(symbol)
        direction_value = _decimal_text(raw.get("max_net_quantity_direction"))
        direction = "LONG" if direction_value == "1" else "SHORT" if direction_value == "-1" else None
        last_event_type = _text(raw.get("last_event_type"))
        closed_at = _utc_timestamp(raw.get("closed_at"))
        normalized = last_event_type == "CLOSED" and closed_at is not None
        cursor = store.connection.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                raw_record_id, source_type, occurred_at_utc, event_type,
                asset, quantity, quote_asset, unit_price, external_id,
                classification_status, classification_reason, created_at_utc,
                source_status, market_symbol, instrument_type, side,
                opened_at_utc, closed_at_utc, realized_pnl,
                funding_quantity, interest_quantity, trading_fee_quantity,
                liquidation_fee_quantity, entry_price, closing_price,
                direction, leverage
            ) VALUES (
                ?, 'BACKPACK', ?, 'FUTURES_POSITION_CLOSED', ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["raw_record_id"],
                closed_at or _utc_timestamp(raw.get("opened_at")),
                asset,
                _decimal_text(raw.get("max_net_quantity")),
                quote_asset,
                _decimal_text(raw.get("entry_price")),
                _text(raw.get("position_id")),
                "NORMALIZED" if normalized else "NEEDS_REVIEW",
                "종료 포지션의 집계 손익입니다." if normalized else "종료 여부와 시각을 확인해야 합니다.",
                now,
                last_event_type,
                symbol,
                instrument_type,
                direction,
                _utc_timestamp(raw.get("opened_at")),
                closed_at,
                _decimal_text(raw.get("cumulative_pnl_realized")),
                _decimal_text(raw.get("total_funding_quantity")),
                _decimal_text(raw.get("total_interest")),
                _decimal_text(raw.get("total_trading_fees")),
                _decimal_text(raw.get("total_liquidation_fees")),
                _decimal_text(raw.get("entry_price")),
                _decimal_text(raw.get("closing_price")),
                direction,
                _decimal_text(raw.get("account_leverage")),
            ),
        )
        created += cursor.rowcount
    store.connection.commit()
    return NormalizationResult(created=created, skipped=0)


def normalize_backpack_settlements(store: TaxStore) -> NormalizationResult:
    rows = store.connection.execute(
        """
        SELECT rr.id AS raw_record_id, rr.raw_json
        FROM raw_records rr
        JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN normalized_events ne ON ne.raw_record_id = rr.id
        WHERE sf.source_type = 'BACKPACK'
          AND sf.deleted_at_utc IS NULL
          AND sf.document_type = 'SETTLEMENTS'
          AND ne.id IS NULL
        ORDER BY rr.id
        """
    ).fetchall()
    created = 0
    skipped = 0
    now = datetime.now(UTC).isoformat()
    source_to_event = {
        "RealizePnl": "REALIZED_PNL",
        "FundingPayment": "FUNDING_PAYMENT",
        "TradingFees": "TRADING_FEE",
        "TradingFeesSystem": "TRADING_FEE_SYSTEM",
        "BackstopLiquidation": "BACKSTOP_LIQUIDATION",
    }
    for row in rows:
        raw = json.loads(row["raw_json"])
        source = _text(raw.get("source"))
        event_type = source_to_event.get(source or "", "SETTLEMENT_OTHER")
        quantity = _decimal_text(raw.get("quantity"))
        cursor = store.connection.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                raw_record_id, source_type, occurred_at_utc, event_type,
                asset, quantity, external_id, classification_status,
                classification_reason, created_at_utc, source_status,
                subaccount_id, position_id, realized_pnl,
                funding_quantity, trading_fee_quantity
            ) VALUES (
                ?, 'BACKPACK', ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["raw_record_id"],
                _utc_timestamp(raw.get("timestamp")),
                event_type,
                quantity,
                _text(raw.get("engineSequence")),
                "NORMALIZED" if source in source_to_event else "NEEDS_REVIEW",
                "Backpack 정산 API 원본에서 분류했습니다."
                if source in source_to_event
                else f"알 수 없는 정산 유형 {source or '없음'}을 확인해야 합니다.",
                now,
                source,
                _text(raw.get("subaccountId")),
                _text(raw.get("positionId")),
                quantity if event_type == "REALIZED_PNL" else None,
                quantity if event_type == "FUNDING_PAYMENT" else None,
                quantity if event_type in {"TRADING_FEE", "TRADING_FEE_SYSTEM"} else None,
            ),
        )
        if cursor.rowcount:
            created += 1
        else:
            skipped += 1
    store.connection.commit()
    return NormalizationResult(created=created, skipped=skipped)


def normalize_upbit_transactions(store: TaxStore) -> NormalizationResult:
    rows = store.connection.execute(
        """
        SELECT rr.id AS raw_record_id, rr.raw_json
        FROM raw_records rr
        JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN normalized_events ne ON ne.raw_record_id = rr.id
        WHERE sf.source_type = 'UPBIT'
          AND sf.deleted_at_utc IS NULL
          AND sf.document_type = 'TRANSACTIONS'
          AND ne.id IS NULL
        ORDER BY rr.id
        """
    ).fetchall()
    created = 0
    skipped = 0
    now = datetime.now(UTC).isoformat()
    type_to_event = {
        "매수": ("TRADE_FILL", "BUY"),
        "매도": ("TRADE_FILL", "SELL"),
        "입금": ("DEPOSIT", None),
        "출금": ("WITHDRAWAL", None),
    }

    for row in rows:
        raw = json.loads(row["raw_json"])
        if raw.get("statement_format") != "UPBIT_PDF_V1":
            skipped += 1
            continue
        transaction_type = _text(raw.get("transaction_type"))
        event_definition = type_to_event.get(transaction_type or "")
        if event_definition is None:
            skipped += 1
            continue
        event_type, side = event_definition
        market_symbol = _text(raw.get("asset_name"))
        quantity_asset = _text(raw.get("quantity_asset"))
        if event_type == "TRADE_FILL" and market_symbol and "-" in market_symbol:
            quote_asset, asset = market_symbol.split("-", 1)
        else:
            quote_asset = None
            asset = quantity_asset or market_symbol
        wallet_address = _text(raw.get("wallet_address"))
        trusted_krw_transfer = event_type in {"DEPOSIT", "WITHDRAWAL"} and asset == "KRW"
        needs_review = event_type in {"DEPOSIT", "WITHDRAWAL"} and not trusted_krw_transfer
        if trusted_krw_transfer:
            direction_label = "입금" if event_type == "DEPOSIT" else "출금"
            reason = f"업비트 KRW {direction_label}은 본인 명의 케이뱅크 인증계좌 거래로 자동 정규화했습니다."
        elif needs_review:
            reason = "업비트 거래내역서의 디지털 자산 입출금입니다. 본인 계정·지갑 이동 여부를 확인해야 합니다."
        else:
            reason = "업비트 거래내역서의 체결 금액과 수수료를 정규화했습니다."
        cursor = store.connection.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                raw_record_id, source_type, occurred_at_utc, event_type,
                asset, quantity, quote_asset, unit_price, gross_amount,
                fee_asset, fee_quantity, external_id, wallet_address,
                classification_status, classification_reason, created_at_utc,
                source_status, market_symbol, instrument_type, side,
                from_address, to_address
            ) VALUES (
                ?, 'UPBIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'ISSUED_STATEMENT', ?, ?, ?, ?, ?
            )
            """,
            (
                row["raw_record_id"],
                _utc_timestamp(raw.get("occurred_at_utc")),
                event_type,
                asset,
                _decimal_text(raw.get("quantity")),
                quote_asset,
                _decimal_text(raw.get("unit_price")),
                _decimal_text(raw.get("gross_amount")),
                _text(raw.get("fee_asset")),
                _decimal_text(raw.get("fee")),
                _text(raw.get("event_fingerprint")),
                wallet_address,
                "NEEDS_REVIEW" if needs_review else "NORMALIZED",
                reason,
                now,
                market_symbol if event_type == "TRADE_FILL" else None,
                "SPOT_CRYPTO" if asset != "KRW" else "FIAT",
                side,
                wallet_address if event_type == "DEPOSIT" else None,
                wallet_address if event_type == "WITHDRAWAL" else None,
            ),
        )
        if cursor.rowcount:
            created += 1
        else:
            skipped += 1
    store.connection.commit()
    return NormalizationResult(created=created, skipped=skipped)


def normalize_bithumb_transactions(store: TaxStore) -> NormalizationResult:
    rows = store.connection.execute(
        """
        SELECT rr.id AS raw_record_id, rr.raw_json
        FROM raw_records rr
        JOIN source_files sf ON sf.id = rr.source_file_id
        LEFT JOIN normalized_events ne ON ne.raw_record_id = rr.id
        WHERE sf.source_type = 'BITHUMB'
          AND sf.deleted_at_utc IS NULL
          AND sf.document_type = 'TRANSACTIONS'
          AND ne.id IS NULL
        ORDER BY rr.id
        """
    ).fetchall()
    created = 0
    skipped = 0
    now = datetime.now(UTC).isoformat()
    type_to_event = {
        "매수": ("TRADE_FILL", "BUY"),
        "매도": ("TRADE_FILL", "SELL"),
        "입금": ("DEPOSIT", None),
        "출금": ("WITHDRAWAL", None),
    }

    for row in rows:
        raw = json.loads(row["raw_json"])
        if raw.get("statement_format") != "BITHUMB_PDF_V1":
            skipped += 1
            continue
        transaction_type = _text(raw.get("transaction_type"))
        event_definition = type_to_event.get(transaction_type or "")
        if event_definition is None:
            skipped += 1
            continue
        event_type, side = event_definition
        asset = _text(raw.get("asset"))
        quote_asset = (
            _text(raw.get("unit_price_asset")) if event_type == "TRADE_FILL" else None
        )
        market_symbol = (
            f"{quote_asset}-{asset}" if quote_asset and asset and event_type == "TRADE_FILL" else None
        )
        trusted_krw_transfer = event_type in {"DEPOSIT", "WITHDRAWAL"} and asset == "KRW"
        needs_review = event_type in {"DEPOSIT", "WITHDRAWAL"} and not trusted_krw_transfer
        if trusted_krw_transfer:
            direction_label = "입금" if event_type == "DEPOSIT" else "출금"
            reason = f"빗썸 KRW {direction_label}은 본인 명의 국민은행 인증계좌 거래로 자동 정규화했습니다."
        elif needs_review:
            reason = "빗썸 거래내역확인서의 디지털 자산 입출금입니다. 본인 계정 이동 여부를 확인해야 합니다."
        else:
            reason = "빗썸 거래내역확인서에서 개인정보를 제외하고 체결 금액과 수수료를 정규화했습니다."
        cursor = store.connection.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                raw_record_id, source_type, occurred_at_utc, event_type,
                asset, quantity, quote_asset, unit_price, gross_amount,
                fee_asset, fee_quantity, external_id,
                classification_status, classification_reason, created_at_utc,
                source_status, market_symbol, instrument_type, side,
                is_maker, system_order_type
            ) VALUES (
                ?, 'BITHUMB', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'ISSUED_STATEMENT_SANITIZED', ?, ?, ?, ?, ?
            )
            """,
            (
                row["raw_record_id"],
                _utc_timestamp(raw.get("occurred_at_utc")),
                event_type,
                asset,
                _decimal_text(raw.get("quantity")),
                quote_asset,
                _decimal_text(raw.get("unit_price")),
                _decimal_text(raw.get("gross_amount")),
                _text(raw.get("fee_asset")),
                _decimal_text(raw.get("fee")),
                _text(raw.get("event_fingerprint")),
                "NEEDS_REVIEW" if needs_review else "NORMALIZED",
                reason,
                now,
                market_symbol,
                "FIAT" if asset == "KRW" else "SPOT_CRYPTO",
                side,
                _boolean(raw.get("is_maker")) if event_type == "TRADE_FILL" else None,
                _text(raw.get("order_type")),
            ),
        )
        if cursor.rowcount:
            created += 1
        else:
            skipped += 1
    store.connection.commit()
    return NormalizationResult(created=created, skipped=skipped)


def normalize_all_backpack(store: TaxStore) -> NormalizationResult:
    results = (
        normalize_backpack_transfers(store),
        normalize_backpack_trades(store),
        normalize_backpack_futures(store),
        normalize_backpack_settlements(store),
    )
    return NormalizationResult(
        created=sum(result.created for result in results),
        skipped=sum(result.skipped for result in results),
    )


def normalize_backpack_internal_airdrops(store: TaxStore) -> int:
    """Reclassify existing Backpack Internal deposits without guessing a basis."""
    reason = "Backpack Internal 입금은 에어드랍으로 자동 분류했습니다. 취득가를 확인해야 합니다."
    with store.connection:
        cursor = store.connection.execute(
            """
            UPDATE normalized_events
            SET event_type = 'AIRDROP',
                instrument_type = CASE
                    WHEN asset LIKE '%.US' THEN 'TOKENIZED_STOCK'
                    ELSE 'SPOT_CRYPTO'
                END,
                classification_status = CASE
                    WHEN LOWER(COALESCE(source_status, 'confirmed')) = 'confirmed'
                    THEN 'NEEDS_REVIEW'
                    ELSE classification_status
                END,
                classification_reason = CASE
                    WHEN LOWER(COALESCE(source_status, 'confirmed')) = 'confirmed'
                    THEN ?
                    ELSE classification_reason
                END,
                is_internal = NULL
            WHERE source_type = 'BACKPACK'
              AND event_type = 'DEPOSIT'
              AND LOWER(COALESCE(network, '')) = 'internal'
            """,
            (reason,),
        )
    return cursor.rowcount


def normalize_trusted_krw_deposits(store: TaxStore) -> int:
    updated = 0
    rules = {
        "UPBIT": {
            "DEPOSIT": "업비트 KRW 입금은 본인 명의 케이뱅크 인증계좌 거래로 자동 정규화했습니다.",
            "WITHDRAWAL": "업비트 KRW 출금은 본인 명의 케이뱅크 인증계좌 거래로 자동 정규화했습니다.",
        },
        "BITHUMB": {
            "DEPOSIT": "빗썸 KRW 입금은 본인 명의 국민은행 인증계좌 거래로 자동 정규화했습니다.",
            "WITHDRAWAL": "빗썸 KRW 출금은 본인 명의 국민은행 인증계좌 거래로 자동 정규화했습니다.",
        },
    }
    with store.connection:
        for source_type, event_rules in rules.items():
            for event_type, reason in event_rules.items():
                cursor = store.connection.execute(
                    """
                    UPDATE normalized_events
                    SET classification_status = 'NORMALIZED', classification_reason = ?
                    WHERE source_type = ?
                      AND event_type = ?
                      AND asset = 'KRW'
                      AND (
                          classification_status != 'NORMALIZED'
                          OR classification_reason IS NULL
                          OR classification_reason != ?
                      )
                    """,
                    (reason, source_type, event_type, reason),
                )
                updated += cursor.rowcount
    return updated


def normalize_all_sources(store: TaxStore) -> NormalizationResult:
    results = (
        normalize_all_backpack(store),
        normalize_upbit_transactions(store),
        normalize_bithumb_transactions(store),
    )
    normalize_backpack_internal_airdrops(store)
    normalize_trusted_krw_deposits(store)
    return NormalizationResult(
        created=sum(result.created for result in results),
        skipped=sum(result.skipped for result in results),
    )
