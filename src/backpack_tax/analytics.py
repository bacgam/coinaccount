from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from datetime import UTC, datetime
from typing import Any

from backpack_tax.db import TaxStore


ZERO = Decimal("0")

ASSET_NAMES = {
    "APT": "Aptos",
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "HYPE": "Hyperliquid",
    "BP": "Backpack",
    "ENA": "Ethena",
    "USDC": "USD Coin",
    "USDT": "Tether",
    "ZEC": "Zcash",
    "CRCL.US": "Circle",
    "HIMS.US": "Hims & Hers",
    "RDW.US": "Redwire",
    "AMZN.US": "Amazon",
    "INTC.US": "Intel",
    "TER.US": "Teradyne",
}

INSTRUMENT_LABELS = {
    "SPOT_CRYPTO": "코인 현물",
    "RFQ_SPOT": "코인 현물",
    "TOKENIZED_STOCK": "토큰화 주식",
    "PERPETUAL_FUTURE": "무기한 선물",
}


def _decimal(value: object) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def _new_bucket(asset: str, instrument_type: str) -> dict[str, Any]:
    return {
        "asset": asset,
        "name": ASSET_NAMES.get(asset, asset.removesuffix(".US")),
        "instrument_type": instrument_type,
        "instrument_label": INSTRUMENT_LABELS.get(instrument_type, instrument_type),
        "asset_class": "stock" if instrument_type == "TOKENIZED_STOCK" else "coin",
        "volume": ZERO,
        "buy_quantity": ZERO,
        "sell_quantity": ZERO,
        "buy_amount": ZERO,
        "sell_amount": ZERO,
        "pnl": ZERO,
        "fees": ZERO,
        "inventory_quantity": ZERO,
        "inventory_cost": ZERO,
        "realized_cost": ZERO,
        "matched_sell_amount": ZERO,
        "unknown_sell_quantity": ZERO,
        "unknown_sell_amount": ZERO,
        "transfer_in_quantity": ZERO,
        "transfer_in_cost": ZERO,
        "transfer_out_quantity": ZERO,
        "transfer_fee_quantity": ZERO,
        "transfer_fee_cost": ZERO,
        "pending_transfer_basis_quantity": ZERO,
        "airdrop_quantity": ZERO,
        "airdrop_cost": ZERO,
        "external_acquisition_quantity": ZERO,
        "external_acquisition_cost": ZERO,
        "selected_activity": False,
    }


def _compact(value: Decimal, places: int = 8) -> str:
    rendered = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return rendered if rendered not in {"", "-0"} else "0"


def _timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _in_period(
    timestamp: datetime | None,
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> bool:
    if timestamp is None:
        return start_utc is None and end_utc is None
    return (start_utc is None or timestamp >= start_utc) and (
        end_utc is None or timestamp < end_utc
    )


def asset_performance(
    store: TaxStore,
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    source_type: str | None = None,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    selected_source = source_type.upper() if source_type else None
    source_filter = "AND ne.source_type = ?" if selected_source else ""
    source_parameters = (selected_source,) if selected_source else ()
    if selected_source == "BACKPACK":
        replay_sources = ("BACKPACK",)
    elif selected_source in {"UPBIT", "BITHUMB"}:
        replay_sources = ("UPBIT", "BITHUMB")
    else:
        replay_sources = ()
    replay_filter = ""
    replay_parameters: tuple[str, ...] = ()
    if replay_sources:
        placeholders = ",".join("?" for _ in replay_sources)
        replay_filter = f"AND ne.source_type IN ({placeholders})"
        replay_parameters = replay_sources
    trades = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, occurred_at_utc, asset, instrument_type, side, quantity,
               unit_price, gross_amount, fee_asset, fee_quantity
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE ne.event_type = 'TRADE_FILL'
          AND ne.asset IS NOT NULL
          AND ne.instrument_type IS NOT NULL
          AND ne.classification_status = 'NORMALIZED'
          AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
          {replay_filter}
        ORDER BY ne.occurred_at_utc, ne.id
        """,
        replay_parameters,
    ).fetchall()

    basis_inflows = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.asset,
               ne.event_type, ne.instrument_type, ne.quantity, ev.native_value,
               ev.native_currency, da.origin_type
        FROM normalized_events ne
        JOIN event_valuations ev ON ev.normalized_event_id = ne.id
        LEFT JOIN deposit_acquisitions da ON da.normalized_event_id = ne.id
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE (
              ne.event_type = 'AIRDROP'
              OR (
                  ne.event_type = 'DEPOSIT'
                  AND da.id IS NOT NULL
                  AND (
                      SELECT latest.decision
                      FROM review_decisions latest
                      WHERE latest.normalized_event_id = ne.id
                      ORDER BY latest.id DESC
                      LIMIT 1
                  ) IN ('EXTERNAL_PURCHASE', 'OPENING_BALANCE')
              )
          )
          AND ne.asset IS NOT NULL
          AND ne.quantity IS NOT NULL
          AND ne.classification_status = 'NORMALIZED'
          AND ev.valuation_status = 'VALUED'
          AND ev.native_value IS NOT NULL
          AND (
              (ne.source_type = 'BACKPACK' AND ev.native_currency = 'USD')
              OR (ne.source_type IN ('UPBIT', 'BITHUMB') AND ev.native_currency = 'KRW')
          )
          AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
          {replay_filter}
        ORDER BY ne.occurred_at_utc, ne.id
        """,
        replay_parameters,
    ).fetchall()

    transfer_rows = []
    if replay_sources:
        placeholders = ",".join("?" for _ in replay_sources)
        transfer_rows = store.connection.execute(
            f"""
            SELECT tm.id, tm.asset, tm.withdrawal_quantity,
                   tm.deposit_quantity, tm.fee_quantity,
                   withdrawal.source_type AS withdrawal_source_type,
                   withdrawal.occurred_at_utc AS withdrawal_occurred_at_utc,
                   deposit.source_type AS deposit_source_type,
                   deposit.occurred_at_utc AS deposit_occurred_at_utc
            FROM transfer_matches tm
            JOIN normalized_events withdrawal ON withdrawal.id = tm.withdrawal_event_id
            JOIN normalized_events deposit ON deposit.id = tm.deposit_event_id
            LEFT JOIN raw_records wrr ON wrr.id = withdrawal.raw_record_id
            LEFT JOIN source_files wsf ON wsf.id = wrr.source_file_id
            LEFT JOIN raw_records drr ON drr.id = deposit.raw_record_id
            LEFT JOIN source_files dsf ON dsf.id = drr.source_file_id
            WHERE tm.status = 'ACTIVE'
              AND withdrawal.classification_status = 'NORMALIZED'
              AND deposit.classification_status = 'NORMALIZED'
              AND (
                  withdrawal.source_type IN ({placeholders})
                  OR deposit.source_type IN ({placeholders})
              )
              AND (withdrawal.raw_record_id IS NULL OR wsf.deleted_at_utc IS NULL)
              AND (deposit.raw_record_id IS NULL OR dsf.deleted_at_utc IS NULL)
            """,
            (*replay_sources, *replay_sources),
        ).fetchall()

    wallet_parameters: tuple[str, ...] = ()
    wallet_filter = ""
    if replay_sources:
        placeholders = ",".join("?" for _ in replay_sources)
        wallet_filter = f"""
          AND pwt.wallet_key IN (
              SELECT related.wallet_key
              FROM personal_wallet_transfers related
              JOIN normalized_events related_event
                ON related_event.id = related.normalized_event_id
              WHERE related_event.source_type IN ({placeholders})
          )
        """
        wallet_parameters = replay_sources
    personal_wallet_rows = store.connection.execute(
        f"""
        SELECT ne.id, ne.source_type, ne.occurred_at_utc, ne.event_type,
               ne.asset, ne.instrument_type, ne.quantity, ne.fee_asset,
               ne.fee_quantity, pwt.wallet_key, pwt.wallet_name,
               pwt.direction, pwt.basis_mode, pwt.total_cost, pwt.currency
        FROM personal_wallet_transfers pwt
        JOIN normalized_events ne ON ne.id = pwt.normalized_event_id
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE ne.classification_status = 'NORMALIZED'
          AND ne.asset IS NOT NULL
          AND ne.quantity IS NOT NULL
          AND (
              SELECT latest.decision
              FROM review_decisions latest
              WHERE latest.normalized_event_id = ne.id
              ORDER BY latest.id DESC
              LIMIT 1
          ) = 'PERSONAL_WALLET_TRANSFER'
          AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
          {wallet_filter}
        ORDER BY ne.occurred_at_utc, ne.id
        """,
        wallet_parameters,
    ).fetchall()

    replay: list[tuple[datetime, int, int, str, Any]] = []
    minimum_time = datetime.min.replace(tzinfo=UTC)
    for inflow in basis_inflows:
        replay.append(
            (
                _timestamp(inflow["occurred_at_utc"]) or minimum_time,
                1,
                int(inflow["id"]),
                "BASIS_INFLOW",
                inflow,
            )
        )
    for trade in trades:
        replay.append(
            (
                _timestamp(trade["occurred_at_utc"]) or minimum_time,
                2,
                int(trade["id"]),
                "TRADE",
                trade,
            )
        )
    for transfer in transfer_rows:
        withdrawal_time = _timestamp(transfer["withdrawal_occurred_at_utc"]) or minimum_time
        deposit_time = _timestamp(transfer["deposit_occurred_at_utc"]) or withdrawal_time
        if deposit_time < withdrawal_time:
            deposit_time = withdrawal_time
        replay.append((withdrawal_time, 0, int(transfer["id"]), "TRANSFER_OUT", transfer))
        replay.append((deposit_time, 3, int(transfer["id"]), "TRANSFER_IN", transfer))
    for wallet_transfer in personal_wallet_rows:
        direction = str(wallet_transfer["direction"])
        replay.append(
            (
                _timestamp(wallet_transfer["occurred_at_utc"]) or minimum_time,
                0 if direction == "OUT" else 3,
                int(wallet_transfer["id"]),
                "WALLET_OUT" if direction == "OUT" else "WALLET_IN",
                wallet_transfer,
            )
        )
    replay.sort(key=lambda item: (item[0], item[1], item[2]))

    inventories: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(dict)
    in_transit: dict[int, dict[str, Decimal]] = {}
    wallet_inventories: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(dict)

    def inventory_for(source: str, asset: str, instrument: str) -> dict[str, Decimal]:
        inventory_source = source if selected_source else "ALL"
        key = (inventory_source, asset, instrument)
        if not inventories[key]:
            inventories[key] = {"quantity": ZERO, "cost": ZERO}
        return inventories[key]

    def bucket_for(asset: str, instrument: str) -> dict[str, Any]:
        key = (asset, instrument)
        if not buckets[key]:
            buckets[key] = _new_bucket(*key)
        return buckets[key]

    def wallet_inventory_for(
        wallet_key: str,
        asset: str,
        instrument: str,
        currency: str,
    ) -> dict[str, Decimal]:
        key = (wallet_key, asset, instrument, currency)
        if not wallet_inventories[key]:
            wallet_inventories[key] = {
                "known_quantity": ZERO,
                "unknown_quantity": ZERO,
                "cost": ZERO,
            }
        return wallet_inventories[key]

    for occurred_at, _priority, _sequence, replay_type, row in replay:
        if replay_type == "TRADE":
            trade = row
            key = (trade["asset"], trade["instrument_type"])
            inventory = inventory_for(
                str(trade["source_type"]), trade["asset"], trade["instrument_type"]
            )
            selected = (
                (selected_source is None or trade["source_type"] == selected_source)
                and _in_period(occurred_at, start_utc, end_utc)
            )
            bucket = bucket_for(*key) if selected else None
            quantity = abs(_decimal(trade["quantity"]))
            price = _decimal(trade["unit_price"])
            gross = abs(_decimal(trade["gross_amount"]))
            if bucket is not None:
                bucket["selected_activity"] = True
                bucket["volume"] += gross

            if trade["side"] == "BUY":
                if bucket is not None:
                    bucket["buy_quantity"] += quantity
                    bucket["buy_amount"] += gross
                if trade["instrument_type"] != "PERPETUAL_FUTURE":
                    inventory["quantity"] += quantity
                    inventory["cost"] += gross
            elif trade["side"] == "SELL":
                if bucket is not None:
                    bucket["sell_quantity"] += quantity
                    bucket["sell_amount"] += gross
                if trade["instrument_type"] != "PERPETUAL_FUTURE":
                    available = inventory["quantity"]
                    matched = min(available, quantity)
                    if matched > ZERO and available > ZERO:
                        average_cost = inventory["cost"] / available
                        matched_proceeds = gross * matched / quantity if quantity > ZERO else ZERO
                        if bucket is not None:
                            realized_cost = average_cost * matched
                            bucket["realized_cost"] += realized_cost
                            bucket["matched_sell_amount"] += matched_proceeds
                            bucket["pnl"] += matched_proceeds - realized_cost
                        inventory["quantity"] -= matched
                        inventory["cost"] -= average_cost * matched
                    if bucket is not None:
                        unknown_quantity = quantity - matched
                        bucket["unknown_sell_quantity"] += unknown_quantity
                        if quantity > ZERO:
                            bucket["unknown_sell_amount"] += gross * unknown_quantity / quantity

            if bucket is not None and trade["instrument_type"] != "PERPETUAL_FUTURE":
                fee = abs(_decimal(trade["fee_quantity"]))
                if trade["fee_asset"] == trade["asset"]:
                    bucket["fees"] += fee * price
                else:
                    bucket["fees"] += fee
            continue

        if replay_type == "BASIS_INFLOW":
            inflow = row
            instrument_type = str(inflow["instrument_type"] or "SPOT_CRYPTO")
            inventory = inventory_for(
                str(inflow["source_type"]),
                str(inflow["asset"]),
                instrument_type,
            )
            quantity = abs(_decimal(inflow["quantity"]))
            cost = abs(_decimal(inflow["native_value"]))
            inventory["quantity"] += quantity
            inventory["cost"] += cost
            if (
                (selected_source is None or inflow["source_type"] == selected_source)
                and _in_period(occurred_at, start_utc, end_utc)
            ):
                bucket = bucket_for(str(inflow["asset"]), instrument_type)
                bucket["selected_activity"] = True
                if inflow["event_type"] == "AIRDROP":
                    bucket["airdrop_quantity"] += quantity
                    bucket["airdrop_cost"] += cost
                else:
                    bucket["external_acquisition_quantity"] += quantity
                    bucket["external_acquisition_cost"] += cost
            continue

        if replay_type in {"WALLET_OUT", "WALLET_IN"}:
            wallet_transfer = row
            asset = str(wallet_transfer["asset"])
            instrument_type = str(wallet_transfer["instrument_type"] or "SPOT_CRYPTO")
            quantity = abs(_decimal(wallet_transfer["quantity"]))
            currency = "USD" if wallet_transfer["source_type"] == "BACKPACK" else "KRW"
            wallet_inventory = wallet_inventory_for(
                str(wallet_transfer["wallet_key"]),
                asset,
                instrument_type,
                currency,
            )
            selected = (
                wallet_transfer["source_type"] == selected_source
                and _in_period(occurred_at, start_utc, end_utc)
            )
            bucket = bucket_for(asset, instrument_type) if selected else None
            if replay_type == "WALLET_OUT":
                inventory = inventory_for(
                    str(wallet_transfer["source_type"]), asset, instrument_type
                )
                available = inventory["quantity"]
                known_quantity = min(available, quantity)
                average_cost = inventory["cost"] / available if available > ZERO else ZERO
                carried_cost = average_cost * known_quantity
                inventory["quantity"] -= known_quantity
                inventory["cost"] -= carried_cost
                wallet_inventory["known_quantity"] += known_quantity
                wallet_inventory["unknown_quantity"] += quantity - known_quantity
                wallet_inventory["cost"] += carried_cost
                if bucket is not None:
                    bucket["selected_activity"] = True
                    bucket["transfer_out_quantity"] += quantity
                continue

            wallet_total = (
                wallet_inventory["known_quantity"]
                + wallet_inventory["unknown_quantity"]
            )
            consumed = min(wallet_total, quantity)
            known_taken = ZERO
            unknown_taken = ZERO
            carried_cost = ZERO
            if wallet_total > ZERO and consumed > ZERO:
                known_taken = min(
                    wallet_inventory["known_quantity"],
                    consumed * wallet_inventory["known_quantity"] / wallet_total,
                )
                unknown_taken = min(
                    wallet_inventory["unknown_quantity"],
                    consumed - known_taken,
                )
                if wallet_inventory["known_quantity"] > ZERO:
                    carried_cost = (
                        wallet_inventory["cost"]
                        * known_taken
                        / wallet_inventory["known_quantity"]
                    )
                wallet_inventory["known_quantity"] -= known_taken
                wallet_inventory["unknown_quantity"] -= unknown_taken
                wallet_inventory["cost"] -= carried_cost
            missing_from_wallet = quantity - known_taken - unknown_taken
            basis_mode = str(wallet_transfer["basis_mode"])
            inventory = inventory_for(
                str(wallet_transfer["source_type"]), asset, instrument_type
            )
            if basis_mode == "MANUAL_BASIS":
                recognized_quantity = quantity
                recognized_cost = abs(_decimal(wallet_transfer["total_cost"]))
                pending_quantity = ZERO
            elif basis_mode == "AUTO_CARRY":
                recognized_quantity = known_taken
                recognized_cost = carried_cost
                pending_quantity = unknown_taken + missing_from_wallet
            else:
                recognized_quantity = ZERO
                recognized_cost = ZERO
                pending_quantity = quantity
            inventory["quantity"] += recognized_quantity
            inventory["cost"] += recognized_cost
            if bucket is not None:
                bucket["selected_activity"] = True
                bucket["transfer_in_quantity"] += quantity
                bucket["transfer_in_cost"] += recognized_cost
                bucket["pending_transfer_basis_quantity"] += pending_quantity
            continue

        transfer = row
        transfer_id = int(transfer["id"])
        asset = transfer["asset"]
        basis_currency_compatible = (
            (transfer["withdrawal_source_type"] == "BACKPACK")
            == (transfer["deposit_source_type"] == "BACKPACK")
        )
        withdrawal_quantity = abs(_decimal(transfer["withdrawal_quantity"]))
        deposit_quantity = abs(_decimal(transfer["deposit_quantity"]))
        fee_quantity = abs(_decimal(transfer["fee_quantity"]))
        removal_quantity = max(withdrawal_quantity, deposit_quantity + fee_quantity)
        if replay_type == "TRANSFER_OUT":
            inventory = inventory_for(
                str(transfer["withdrawal_source_type"]), asset, "SPOT_CRYPTO"
            )
            available = inventory["quantity"]
            matched = min(available, removal_quantity)
            average_cost = inventory["cost"] / available if available > ZERO else ZERO
            known_deposit_quantity = (
                matched * deposit_quantity / removal_quantity
                if removal_quantity > ZERO
                else ZERO
            )
            known_fee_quantity = min(
                fee_quantity,
                max(ZERO, matched - known_deposit_quantity),
            )
            carried_cost = average_cost * known_deposit_quantity
            fee_cost = average_cost * known_fee_quantity
            inventory["quantity"] -= matched
            inventory["cost"] -= average_cost * matched
            in_transit[transfer_id] = {
                "known_quantity": known_deposit_quantity,
                "cost": carried_cost,
                "unknown_quantity": deposit_quantity - known_deposit_quantity,
            }
            if (
                transfer["withdrawal_source_type"] == selected_source
                and _in_period(occurred_at, start_utc, end_utc)
            ):
                bucket = bucket_for(asset, "SPOT_CRYPTO")
                bucket["selected_activity"] = True
                bucket["transfer_out_quantity"] += withdrawal_quantity
                bucket["transfer_fee_quantity"] += fee_quantity
                bucket["transfer_fee_cost"] += fee_cost
                bucket["fees"] += fee_cost
        else:
            carried = in_transit.get(
                transfer_id,
                {"known_quantity": ZERO, "cost": ZERO, "unknown_quantity": deposit_quantity},
            )
            inventory = inventory_for(
                str(transfer["deposit_source_type"]), asset, "SPOT_CRYPTO"
            )
            if basis_currency_compatible:
                inventory["quantity"] += carried["known_quantity"]
                inventory["cost"] += carried["cost"]
            else:
                carried = {
                    "known_quantity": ZERO,
                    "cost": ZERO,
                    "unknown_quantity": deposit_quantity,
                }
            if (
                transfer["deposit_source_type"] == selected_source
                and _in_period(occurred_at, start_utc, end_utc)
            ):
                bucket = bucket_for(asset, "SPOT_CRYPTO")
                bucket["selected_activity"] = True
                bucket["transfer_in_quantity"] += deposit_quantity
                bucket["transfer_in_cost"] += carried["cost"]
                bucket["pending_transfer_basis_quantity"] += carried["unknown_quantity"]

    futures = store.connection.execute(
        f"""
        SELECT occurred_at_utc, asset, realized_pnl, funding_quantity, interest_quantity,
               trading_fee_quantity, liquidation_fee_quantity
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE ne.event_type = 'FUTURES_POSITION_CLOSED'
          AND ne.asset IS NOT NULL
          AND ne.classification_status = 'NORMALIZED'
          AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
          {source_filter}
        """,
        source_parameters,
    ).fetchall()
    for future in futures:
        if not _in_period(_timestamp(future["occurred_at_utc"]), start_utc, end_utc):
            continue
        key = (future["asset"], "PERPETUAL_FUTURE")
        if not buckets[key]:
            buckets[key] = _new_bucket(*key)
        bucket = buckets[key]
        bucket["selected_activity"] = True
        bucket["pnl"] += (
            _decimal(future["realized_pnl"])
            + _decimal(future["funding_quantity"])
            - abs(_decimal(future["interest_quantity"]))
        )
        bucket["fees"] += abs(_decimal(future["trading_fee_quantity"]))
        bucket["fees"] += abs(_decimal(future["liquidation_fee_quantity"]))

    results: list[dict[str, Any]] = []
    for bucket in buckets.values():
        if not bucket["selected_activity"]:
            continue
        pnl = bucket["pnl"]
        results.append(
            {
                **bucket,
                "symbol_mark": bucket["asset"].split(".", 1)[0][:3],
                "volume_display": _compact(bucket["volume"], 2),
                "buy_quantity_display": _compact(bucket["buy_quantity"]),
                "sell_quantity_display": _compact(bucket["sell_quantity"]),
                "buy_amount_display": _compact(bucket["buy_amount"], 2),
                "sell_amount_display": _compact(bucket["sell_amount"], 2),
                "pnl_display": _compact(pnl, 2),
                "fee_display": _compact(bucket["fees"], 2),
                "transfer_in_display": _compact(bucket["transfer_in_quantity"]),
                "transfer_in_cost_display": _compact(bucket["transfer_in_cost"], 2),
                "transfer_out_display": _compact(bucket["transfer_out_quantity"]),
                "transfer_fee_display": _compact(bucket["transfer_fee_quantity"]),
                "transfer_fee_cost_display": _compact(bucket["transfer_fee_cost"], 2),
                "pending_transfer_basis_display": _compact(
                    bucket["pending_transfer_basis_quantity"]
                ),
                "airdrop_quantity_display": _compact(bucket["airdrop_quantity"]),
                "airdrop_cost_display": _compact(bucket["airdrop_cost"], 2),
                "has_airdrop_activity": bucket["airdrop_quantity"] > ZERO,
                "external_acquisition_quantity_display": _compact(
                    bucket["external_acquisition_quantity"]
                ),
                "external_acquisition_cost_display": _compact(
                    bucket["external_acquisition_cost"], 2
                ),
                "has_external_acquisition_activity": (
                    bucket["external_acquisition_quantity"] > ZERO
                ),
                "has_transfer_activity": (
                    bucket["transfer_in_quantity"] > ZERO
                    or bucket["transfer_out_quantity"] > ZERO
                ),
                "has_pending_transfer_basis": (
                    bucket["pending_transfer_basis_quantity"] > ZERO
                ),
                "pnl_class": "positive" if pnl > ZERO else "negative" if pnl < ZERO else "neutral",
                "has_unknown_basis": bucket["unknown_sell_quantity"] > ZERO,
                "unknown_basis_display": _compact(bucket["unknown_sell_quantity"]),
                "unknown_basis_amount_display": _compact(bucket["unknown_sell_amount"], 2),
            }
        )
    results.sort(key=lambda item: item["volume"], reverse=True)
    return results


def performance_summary(rows: list[dict[str, Any]]) -> dict[str, str | int]:
    return {
        "asset_count": len(rows),
        "volume": _compact(sum((row["volume"] for row in rows), ZERO), 2),
        "pnl": _compact(sum((row["pnl"] for row in rows), ZERO), 2),
        "fees": _compact(sum((row["fees"] for row in rows), ZERO), 2),
    }
