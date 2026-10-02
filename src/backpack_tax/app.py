from __future__ import annotations

import secrets
import os
import threading
import webbrowser
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from requests import RequestException

from backpack_tax.analytics import asset_performance, performance_summary
from backpack_tax.auto_logos import download_missing_asset_logos
from backpack_tax.db import TaxStore
from backpack_tax.excel_export import (
    build_asset_performance_workbook,
    build_income_statement_workbook,
    excel_filename,
    income_statement_filename,
)
from backpack_tax.income_statement import build_income_statement
from backpack_tax.imports import (
    import_bithumb_pdf_statement,
    import_exchange_csv,
    import_upbit_pdf_statement,
)
from backpack_tax.import_preview import (
    analyze_records,
    conflict_field_differences,
    keep_new_rows_only,
    parse_upload_records,
    save_import_conflicts,
)
from backpack_tax.logos import LogoUpload, logo_filenames, save_asset_logos
from backpack_tax.periods import (
    available_year_options,
    calendar_option_context,
    select_period,
)
from backpack_tax.normalize import normalize_all_sources
from backpack_tax.historical_pricing import refresh_historical_valuations
from backpack_tax.health import run_data_health_checks
from backpack_tax.basis_gaps import basis_gap_summary
from backpack_tax.upbit_market import (
    fetch_upbit_usdt_history,
    fetch_upbit_usdt_ticker,
)
from backpack_tax.smart_review import apply_smart_review_rule, smart_review_inbox
from backpack_tax.transfer_review import apply_transfer_batch, transfer_review_queue

LEDGER_EXCHANGES = {
    "BACKPACK": "Backpack",
    "UPBIT": "업비트",
    "BITHUMB": "빗썸",
}

WALLET_EXCHANGES = {
    **LEDGER_EXCHANGES,
    "BINANCE": "Binance",
    "BYBIT": "Bybit",
    "OKX": "OKX",
    "COINBASE": "Coinbase",
}

REVIEW_EVENT_LABELS = {
    "AIRDROP": "에어드랍",
    "DEPOSIT": "입금",
    "WITHDRAWAL": "출금",
    "TRADE_FILL": "현물 체결",
    "FUTURES_POSITION_CLOSED": "선물 종료",
    "REALIZED_PNL": "실현손익 정산",
    "FUNDING_PAYMENT": "펀딩 정산",
    "TRADING_FEE": "거래 수수료",
    "TRADING_FEE_SYSTEM": "시스템 수수료",
    "BACKSTOP_LIQUIDATION": "백스톱 청산",
    "SETTLEMENT_OTHER": "기타 정산",
}

REVIEW_DECISION_LABELS = {
    "AIRDROP_CLASSIFIED": "에어드랍 분류",
    "AIRDROP_BASIS": "에어드랍 취득가 승인",
    "EXTERNAL_PURCHASE": "외부 거래소 매수 입금",
    "OPENING_BALANCE": "기초잔고·자료 누락",
    "PERSONAL_WALLET_TRANSFER": "개인 지갑 이동",
    "CONFIRMED": "정규화 승인",
    "INTERNAL_TRANSFER": "내부 이동",
    "EXCLUDED": "계산 제외",
    "REOPENED": "다시 검토",
}

WALLET_NETWORKS = {
    "EVM": ("EVM 계열 (0x 주소)", "EVM"),
    "SOLANA": ("Solana (SVM)", "SVM"),
    "BITCOIN": ("Bitcoin", "BITCOIN"),
    "OTHER": ("기타 네트워크", "OTHER"),
}


def format_grouped_decimal(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    rendered = format(abs(number), "f")
    integer, separator, fraction = rendered.partition(".")
    grouped = f"{int(integer or '0'):,}"
    fraction = fraction.rstrip("0")
    if separator and fraction:
        grouped = f"{grouped}.{fraction}"
    if number < 0:
        grouped = f"-{grouped}"
    return grouped


def create_app(*, project_root: Path | None = None, testing: bool = False) -> Flask:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,
        TESTING=testing,
        TAX_DATABASE=root / "data" / "tax_ledger.db",
        EVIDENCE_DIRECTORY=root / "data" / "tax_evidence" / "raw",
        EVIDENCE_TRASH_DIRECTORY=root / "data" / "tax_evidence" / "trash",
        ASSET_LOGO_DIRECTORY=root / "data" / "tax_assets" / "logos",
    )
    app.jinja_env.filters["group_number"] = format_grouped_decimal
    pending_import_previews: dict[str, dict[str, object]] = {}

    def open_store() -> TaxStore:
        return TaxStore(Path(app.config["TAX_DATABASE"]))

    def delete_csrf_token() -> str:
        token = session.get("delete_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["delete_csrf_token"] = token
        return token

    def validate_delete_csrf() -> None:
        submitted_token = request.form.get("csrf_token", "")
        expected_token = session.get("delete_csrf_token", "")
        if not expected_token or not secrets.compare_digest(
            submitted_token, expected_token
        ):
            abort(400)

    def review_csrf_token() -> str:
        token = session.get("review_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["review_csrf_token"] = token
        return token

    def validate_review_csrf() -> None:
        submitted_token = request.form.get("csrf_token", "")
        expected_token = session.get("review_csrf_token", "")
        if not expected_token or not secrets.compare_digest(
            submitted_token, expected_token
        ):
            abort(400)

    def selected_wallet_values(store: TaxStore) -> tuple[str, str]:
        try:
            account_id = int(request.form.get("wallet_account_id", ""))
        except (TypeError, ValueError):
            account_id = 0
        if account_id:
            account = store.personal_wallet_account(account_id)
            if account is None:
                raise ValueError("선택한 개인 지갑을 찾을 수 없습니다. 지갑주소 등록 화면에서 다시 확인하세요.")
            return str(account["wallet_name"]), str(account["wallet_address"])
        return request.form.get("wallet_name", ""), request.form.get("wallet_address", "")

    with open_store() as store:
        normalize_all_sources(store)
        store.seed_intrinsic_event_valuations()

    @app.errorhandler(413)
    def request_too_large(_error):
        flash(
            "한 번에 올린 파일의 합계가 200MB를 넘었습니다. 파일을 나누어 올려주세요.",
            "error",
        )
        return redirect(url_for("assets"), code=303)

    @app.get("/")
    def dashboard():
        with open_store() as store:
            statistics = store.dashboard_statistics()
            source_files = store.list_source_files()[:5]
            exchange_coverage = store.exchange_data_coverage()
        return render_template(
            "dashboard.html", statistics=statistics, source_files=source_files,
            exchange_coverage=exchange_coverage, exchanges=LEDGER_EXCHANGES,
        )

    @app.get("/reviews")
    def reviews():
        try:
            selected_event_id = int(request.args["event_id"])
        except (KeyError, TypeError, ValueError):
            selected_event_id = None
        status_filter = request.args.get("status", "PENDING").upper()
        if status_filter not in {"PENDING", "RESOLVED", "ALL"}:
            status_filter = "PENDING"
        source_type = request.args.get("source", "").upper()
        event_type = request.args.get("event_type", "").upper()
        asset = request.args.get("asset", "").upper()
        dust_only = request.args.get("dust") == "1"
        unpriced_only = request.args.get("unpriced") == "1"
        if dust_only:
            unpriced_only = False
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 50

        with open_store() as store:
            statistics = store.review_statistics()
            smart_inbox = smart_review_inbox(store)
            filter_options = store.review_filter_options()
            personal_wallets = store.list_personal_wallets()
            if source_type not in filter_options["sources"]:
                source_type = ""
            if event_type not in filter_options["event_types"]:
                event_type = ""
            if asset not in filter_options["assets"]:
                asset = ""
            events, total = store.list_review_events(
                event_id=selected_event_id,
                status_filter=status_filter,
                source_type=source_type or None,
                event_type=event_type or None,
                asset=asset or None,
                dust_only=dust_only,
                unpriced_only=unpriced_only,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            history = store.list_review_decisions(
                [int(event["id"]) for event in events]
            )
            for event in events:
                current_event_id = int(event["id"])
                event["decision_history"] = history[current_event_id]
                event["transfer_match"] = store.transfer_match_for_event(current_event_id)
                event["transfer_candidates"] = (
                    store.list_transfer_candidates(current_event_id)
                    if event["classification_status"] == "NEEDS_REVIEW"
                    and event["event_type"] in {"DEPOSIT", "WITHDRAWAL"}
                    else []
                )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if total and page > total_pages:
            return redirect(
                url_for(
                    "reviews",
                    status=status_filter,
                    source=source_type,
                    event_type=event_type,
                    dust="1" if dust_only else None,
                    unpriced="1" if unpriced_only else None,
                    page=total_pages,
                )
            )
        return render_template(
            "reviews.html",
            events=events,
            statistics=statistics,
            filter_options=filter_options,
            status_filter=status_filter,
            source_type=source_type,
            event_type=event_type,
            asset=asset,
            dust_only=dust_only,
            unpriced_only=unpriced_only,
            event_labels=REVIEW_EVENT_LABELS,
            decision_labels=REVIEW_DECISION_LABELS,
            page=page,
            total=total,
            total_pages=total_pages,
            review_csrf_token=review_csrf_token(),
            personal_wallets=personal_wallets,
            event_id=selected_event_id,
            smart_inbox=smart_inbox,
        )

    @app.get("/reviews/transfers")
    def review_transfers():
        with open_store() as store:
            queue = transfer_review_queue(store)
            wallets = store.list_personal_wallets()
        return render_template(
            "review_transfers.html", queue=queue, wallets=wallets,
            exchanges=LEDGER_EXCHANGES, review_csrf_token=review_csrf_token(),
        )

    @app.post("/reviews/transfers/apply")
    def apply_review_transfers():
        validate_review_csrf()
        try:
            if request.form.get("ownership_confirmed") != "1":
                raise ValueError("선택한 거래가 본인 소유 계정·지갑 사이의 이동인지 확인해 주세요.")
            with open_store() as store:
                wallet_name, wallet_address = ("", "")
                if request.form.get("mode") == "WALLET":
                    wallet_name, wallet_address = selected_wallet_values(store)
                    if not wallet_name.strip() or not wallet_address.strip():
                        raise ValueError("본인 개인 지갑의 이름과 주소를 입력하거나 등록된 지갑을 선택하세요.")
                completed = apply_transfer_batch(
                    store, mode=request.form.get("mode", ""),
                    snapshot=request.form.get("snapshot", ""),
                    selected=request.form.getlist("selected"),
                    wallet_name=wallet_name, wallet_address=wallet_address,
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"선택한 {completed:,}건을 이동으로 저장했습니다. 거래별 판정 이력이 남으며, 기존 원가나 통화 간 가격 증빙이 없으면 취득가 확인이 계속 필요합니다.", "success")
        return redirect(url_for("review_transfers"), code=303)

    @app.post("/reviews/smart/<rule>/apply")
    def apply_smart_review(rule: str):
        validate_review_csrf()
        try:
            with open_store() as store:
                completed = apply_smart_review_rule(store, rule)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            if completed:
                flash(f"스마트 검토 추천 {completed:,}건을 처리했습니다. 판정 이력과 PNL에 반영됩니다.", "success")
            else:
                flash("현재 처리할 수 있는 스마트 검토 추천 항목이 없습니다.", "info")
        return redirect(url_for("reviews", status="PENDING"), code=303)

    @app.get("/reviews/health")
    def review_health():
        with open_store() as store:
            health = run_data_health_checks(store)
        return render_template(
            "review_health.html",
            health=health,
            event_labels=REVIEW_EVENT_LABELS,
        )

    @app.get("/reviews/basis-gaps")
    def review_basis_gaps():
        with open_store() as store:
            summary = basis_gap_summary(store)
        return render_template(
            "review_basis_gaps.html",
            summary=summary,
            exchanges=LEDGER_EXCHANGES,
        )

    @app.post("/reviews/valuations/refresh")
    def refresh_review_valuations():
        validate_review_csrf()
        source_type = request.form.get("source", "").upper()
        event_type = request.form.get("event_type", "").upper()
        with open_store() as store:
            result = refresh_historical_valuations(
                store,
                source_type=source_type or None,
                event_type=event_type or None,
                limit=500,
            )
        if result["attempted"] or result["intrinsic"]:
            flash(
                f"가격 평가 완료: 확인 {result['valued'] + result['intrinsic']:,}건, "
                f"미확인 {result['unpriced']:,}건. 대기 이벤트를 순서대로 자동 처리했습니다.",
                "success" if result["valued"] or result["intrinsic"] else "info",
            )
        else:
            flash("현재 필터에 가격을 채울 검토 대기 이벤트가 없습니다.", "info")
        return redirect(
            url_for(
                "reviews",
                status="PENDING",
                unpriced=1,
                source=source_type,
                event_type=event_type,
            ),
            code=303,
        )

    @app.post("/reviews/dust/approve")
    def approve_dust_reviews():
        validate_review_csrf()
        source_type = request.form.get("source", "").upper()
        event_type = request.form.get("event_type", "").upper()
        with open_store() as store:
            approved = store.approve_dust_events(
                source_type=source_type or None,
                event_type=event_type or None,
                decided_at_utc=datetime.now(UTC).isoformat(),
            )
        if approved:
            flash(f"더스트 트렌젝션 {approved:,}건을 일괄 승인했습니다.", "success")
        else:
            flash("현재 필터에서 일괄 승인할 더스트 트렌젝션이 없습니다.", "info")
        return redirect(
            url_for(
                "reviews",
                status="ALL",
                dust=1,
                source=source_type,
                event_type=event_type,
            ),
            code=303,
        )

    @app.post("/reviews/bulk/approve")
    def approve_review_collection():
        validate_review_csrf()
        collection = request.form.get("collection", "ALL").upper()
        if collection not in {"ALL", "UNPRICED"}:
            abort(400)
        source_type = request.form.get("source", "").upper()
        event_type = request.form.get("event_type", "").upper()
        with open_store() as store:
            approved = store.approve_review_events(
                collection=collection,
                source_type=source_type or None,
                event_type=event_type or None,
                decided_at_utc=datetime.now(UTC).isoformat(),
            )
        collection_label = "전체 검토" if collection == "ALL" else "가격 미확인"
        if approved:
            flash(
                f"{collection_label}의 검토 대기 {approved:,}건을 일괄 승인했습니다.",
                "success",
            )
        else:
            flash(
                f"현재 필터에서 일괄 승인할 {collection_label} 이벤트가 없습니다.",
                "info",
            )
        return redirect(
            url_for(
                "reviews",
                status="PENDING",
                unpriced=1 if collection == "UNPRICED" else None,
                source=source_type,
                event_type=event_type,
            ),
            code=303,
        )

    @app.post("/reviews/<int:event_id>/decision")
    def review_decision(event_id: int):
        validate_review_csrf()
        decision = request.form.get("decision", "").upper()
        rationale = request.form.get("rationale", "")
        try:
            counterparty_event_id = int(request.form["counterparty_event_id"])
        except (KeyError, TypeError, ValueError):
            counterparty_event_id = None
        try:
            with open_store() as store:
                event = store.record_review_decision(
                    event_id,
                    decision=decision,
                    rationale=rationale,
                    decided_at_utc=datetime.now(UTC).isoformat(),
                    counterparty_event_id=counterparty_event_id,
                )
            if event is None:
                abort(404)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"{event['source_type']} {event['asset'] or '이벤트'}를 "
                f"{REVIEW_DECISION_LABELS[decision]} 처리했습니다.",
                "success",
            )

        return_status = request.form.get("return_status", "PENDING").upper()
        if return_status not in {"PENDING", "RESOLVED", "ALL"}:
            return_status = "PENDING"
        return redirect(
            url_for(
                "reviews",
                status=return_status,
                source=request.form.get("return_source", "").upper(),
                event_type=request.form.get("return_event_type", "").upper(),
                asset=request.form.get("return_asset", "").upper(),
                dust=request.form.get("return_dust") or None,
                unpriced=request.form.get("return_unpriced") or None,
                page=request.form.get("return_page", "1"),
            ),
            code=303,
        )

    @app.post("/reviews/<int:event_id>/manual-price")
    def manual_event_price(event_id: int):
        validate_review_csrf()
        try:
            with open_store() as store:
                event = store.set_manual_event_price(
                    event_id,
                    unit_price=request.form.get("unit_price", ""),
                    note=request.form.get("price_note", ""),
                    entered_at_utc=datetime.now(UTC).isoformat(),
                )
            if event is None:
                abort(404)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            currency = str(event["manual_currency"])
            if event["airdrop_confirmed"]:
                flash(
                    f"{event['asset']} 에어드랍의 {currency} 취득가를 저장하고 PNL 원가에 반영했습니다.",
                    "success",
                )
            else:
                flash(
                    f"{event['asset'] or '이벤트'}의 수동 {currency} 가격을 저장했습니다.",
                    "success",
                )
        return redirect(
            url_for(
                "reviews",
                status=request.form.get("return_status", "PENDING").upper(),
                source=request.form.get("return_source", "").upper(),
                event_type=request.form.get("return_event_type", "").upper(),
                dust=request.form.get("return_dust") or None,
                unpriced=request.form.get("return_unpriced") or None,
                page=request.form.get("return_page", "1"),
            ),
            code=303,
        )

    @app.post("/reviews/<int:event_id>/deposit-origin")
    def classify_deposit_origin(event_id: int):
        validate_review_csrf()
        origin_type = request.form.get("origin_type", "").upper()
        try:
            with open_store() as store:
                if origin_type == "INTERNAL_TRANSFER":
                    if request.form.get("internal_mode", "LINKED").upper() == "PERSONAL_WALLET":
                        wallet_name, wallet_address = selected_wallet_values(store)
                        event = store.record_personal_wallet_transfer(
                            event_id,
                            wallet_name=wallet_name,
                            wallet_address=wallet_address,
                            basis_mode=request.form.get("wallet_basis_mode", "AUTO_CARRY"),
                            unit_cost=request.form.get("wallet_unit_cost", ""),
                            fee_cost=request.form.get("wallet_fee_cost", "0"),
                            note=request.form.get("origin_note", ""),
                            recorded_at_utc=datetime.now(UTC).isoformat(),
                        )
                        result_message = "상대 기록 없는 개인 지갑 이동으로 승인했습니다."
                    else:
                        try:
                            counterparty_event_id = int(request.form["counterparty_event_id"])
                        except (KeyError, TypeError, ValueError):
                            counterparty_event_id = None
                        event = store.record_review_decision(
                            event_id,
                            decision="INTERNAL_TRANSFER",
                            rationale=request.form.get("origin_note", ""),
                            decided_at_utc=datetime.now(UTC).isoformat(),
                            counterparty_event_id=counterparty_event_id,
                        )
                        result_message = "선택한 출금과 연결하고 기존 취득가를 승계하도록 처리했습니다."
                elif origin_type == "AIRDROP":
                    event = store.classify_deposit_as_airdrop(
                        event_id,
                        rationale=request.form.get("origin_note", ""),
                        decided_at_utc=datetime.now(UTC).isoformat(),
                    )
                    result_message = "에어드랍으로 분류했습니다. 취득가를 입력하거나 조회 가격을 승인하세요."
                elif origin_type in {"EXTERNAL_PURCHASE", "OPENING_BALANCE"}:
                    event = store.record_deposit_acquisition(
                        event_id,
                        origin_type=origin_type,
                        origin_name=request.form.get("origin_name", ""),
                        acquired_at_utc=request.form.get("acquired_at_utc", ""),
                        unit_cost=request.form.get("unit_cost", ""),
                        fee_cost=request.form.get("fee_cost", "0"),
                        note=request.form.get("origin_note", ""),
                        recorded_at_utc=datetime.now(UTC).isoformat(),
                    )
                    result_message = (
                        f"{event['origin_label']}으로 분류하고 총 취득가 "
                        f"{format_grouped_decimal(event['total_cost'])} {event['currency']}를 PNL 원가에 반영했습니다."
                    )
                else:
                    raise ValueError("입금 성격 네 가지 중 하나를 선택하세요.")
            if event is None:
                abort(404)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"{event['source_type']} {event['asset']} 입금을 {result_message}", "success")

        return redirect(
            url_for(
                "reviews",
                status=request.form.get("return_status", "PENDING").upper(),
                source=request.form.get("return_source", "").upper(),
                event_type=request.form.get("return_event_type", "").upper(),
                asset=request.form.get("return_asset", "").upper(),
                dust=request.form.get("return_dust") or None,
                unpriced=request.form.get("return_unpriced") or None,
                page=request.form.get("return_page", "1"),
            ),
            code=303,
        )

    @app.post("/reviews/<int:event_id>/personal-wallet-transfer")
    def classify_personal_wallet_transfer(event_id: int):
        validate_review_csrf()
        try:
            with open_store() as store:
                wallet_name, wallet_address = selected_wallet_values(store)
                event = store.record_personal_wallet_transfer(
                    event_id,
                    wallet_name=wallet_name,
                    wallet_address=wallet_address,
                    basis_mode=request.form.get("wallet_basis_mode", "AUTO_CARRY"),
                    unit_cost=request.form.get("wallet_unit_cost", ""),
                    fee_cost=request.form.get("wallet_fee_cost", "0"),
                    note=request.form.get("origin_note", ""),
                    recorded_at_utc=datetime.now(UTC).isoformat(),
                )
            if event is None:
                abort(404)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"{event['source_type']} {event['asset']}를 개인 지갑 {event['wallet_name']} 이동으로 승인했습니다.",
                "success",
            )
        return redirect(
            url_for(
                "reviews",
                status=request.form.get("return_status", "PENDING").upper(),
                source=request.form.get("return_source", "").upper(),
                event_type=request.form.get("return_event_type", "").upper(),
                dust=request.form.get("return_dust") or None,
                unpriced=request.form.get("return_unpriced") or None,
                page=request.form.get("return_page", "1"),
            ),
            code=303,
        )

    @app.post("/reviews/<int:event_id>/airdrop-classification")
    def classify_airdrop(event_id: int):
        validate_review_csrf()
        try:
            with open_store() as store:
                event = store.classify_deposit_as_airdrop(
                    event_id,
                    rationale=request.form.get("rationale", ""),
                    decided_at_utc=datetime.now(UTC).isoformat(),
                )
            if event is None:
                abort(404)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            currency = "USD" if event["source_type"] == "BACKPACK" else "KRW"
            flash(
                f"{event['source_type']} {event['asset']} 입금을 에어드랍으로 분류했습니다. "
                f"이제 {currency} 취득가를 입력하거나 조회 가격을 승인하세요.",
                "success",
            )
        return redirect(
            url_for(
                "reviews",
                status=request.form.get("return_status", "PENDING").upper(),
                source=request.form.get("return_source", "").upper(),
                event_type="",
                dust=request.form.get("return_dust") or None,
                unpriced=request.form.get("return_unpriced") or None,
                page=request.form.get("return_page", "1"),
            ),
            code=303,
        )

    @app.post("/reviews/<int:event_id>/airdrop-basis")
    def confirm_airdrop_basis(event_id: int):
        validate_review_csrf()
        try:
            with open_store() as store:
                event = store.confirm_airdrop_basis(
                    event_id,
                    rationale=request.form.get("rationale", ""),
                    decided_at_utc=datetime.now(UTC).isoformat(),
                )
            if event is None:
                abort(404)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"{event['asset']} 에어드랍의 저장된 취득가를 PNL 원가에 반영했습니다.",
                "success",
            )
        return redirect(
            url_for(
                "reviews",
                status=request.form.get("return_status", "PENDING").upper(),
                source=request.form.get("return_source", "").upper(),
                event_type=request.form.get("return_event_type", "").upper(),
                dust=request.form.get("return_dust") or None,
                unpriced=request.form.get("return_unpriced") or None,
                page=request.form.get("return_page", "1"),
            ),
            code=303,
        )

    @app.get("/api/market/upbit/usdt/history")
    def upbit_usdt_history():
        try:
            return jsonify(fetch_upbit_usdt_history(count=60))
        except (RequestException, ValueError, KeyError, TypeError) as exc:
            app.logger.warning("Upbit USDT history unavailable: %s", exc)
            return jsonify({"error": "업비트 최근 시세를 불러오지 못했습니다."}), 502

    @app.get("/api/market/upbit/usdt/ticker")
    def upbit_usdt_ticker():
        try:
            return jsonify(fetch_upbit_usdt_ticker())
        except (RequestException, ValueError, KeyError, TypeError) as exc:
            app.logger.warning("Upbit USDT ticker unavailable: %s", exc)
            return jsonify({"error": "업비트 현재가를 불러오지 못했습니다."}), 502

    @app.route("/imports", methods=["GET", "POST"])
    def imports_page():
        if request.method == "POST":
            upload = request.files.get("source_file") or request.files.get("csv_file")
            source_type = request.form.get("source_type", "").upper()
            document_type = request.form.get("document_type", "")
            if upload is None or not upload.filename:
                flash("원본 파일을 선택하세요.", "error")
                return redirect(url_for("imports_page"))
            try:
                if source_type not in LEDGER_EXCHANGES:
                    raise ValueError("지원하지 않는 거래소입니다.")
                if source_type == "UPBIT" and not upload.filename.lower().endswith(".pdf"):
                    raise ValueError("업비트는 거래내역서 PDF 파일을 선택하세요.")
                if source_type == "BITHUMB" and not upload.filename.lower().endswith(".pdf"):
                    raise ValueError("빗썸은 거래내역확인서 PDF 파일을 선택하세요.")
                password = request.form.get("pdf_password", "")
                if source_type == "UPBIT" and not password:
                    raise ValueError("업비트 거래내역서 PDF 비밀번호를 입력하세요.")
                content = upload.read()
                records, first_row = parse_upload_records(
                    source_type=source_type,
                    filename=upload.filename,
                    content=content,
                    password=password,
                )
                with open_store() as store:
                    analysis = analyze_records(
                        store,
                        source_type=source_type,
                        records=records,
                        first_row_number=first_row,
                    )
                token = secrets.token_urlsafe(24)
                if len(pending_import_previews) >= 10:
                    pending_import_previews.pop(next(iter(pending_import_previews)))
                pending_import_previews[token] = {
                    "source_type": source_type,
                    "document_type": document_type,
                    "filename": upload.filename,
                    "content": content,
                    "analysis": analysis,
                }
                return render_template(
                    "import_preview.html",
                    token=token,
                    preview=pending_import_previews[token],
                    analysis=analysis,
                    delete_csrf_token=delete_csrf_token(),
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("imports_page"))
            try:
                if source_type in {"UPBIT", "BITHUMB"}:
                    if not upload.filename.lower().endswith(".pdf"):
                        raise ValueError(
                            f"{'업비트' if source_type == 'UPBIT' else '빗썸'}는 거래내역서 PDF 파일을 선택하세요."
                        )
                if source_type == "UPBIT":
                    if not request.form.get("pdf_password", ""):
                        raise ValueError("업비트 거래내역서 PDF 비밀번호를 입력하세요.")
                with open_store() as store:
                    content = upload.read()
                    if source_type == "UPBIT":
                        result = import_upbit_pdf_statement(
                            store,
                            evidence_directory=Path(app.config["EVIDENCE_DIRECTORY"]),
                            original_filename=upload.filename,
                            content=content,
                            password=request.form.get("pdf_password", ""),
                        )
                    elif source_type == "BITHUMB":
                        result = import_bithumb_pdf_statement(
                            store,
                            evidence_directory=Path(app.config["EVIDENCE_DIRECTORY"]),
                            original_filename=upload.filename,
                            content=content,
                        )
                    else:
                        result = import_exchange_csv(
                            store,
                            evidence_directory=Path(app.config["EVIDENCE_DIRECTORY"]),
                            source_type=source_type,
                            document_type=document_type,
                            original_filename=upload.filename,
                            content=content,
                        )
                    normalization = normalize_all_sources(store)
                if result.created:
                    if result.retention_mode == "SANITIZED_ONLY":
                        flash(
                            f"빗썸 PDF에서 개인정보를 제외한 거래 {result.row_count:,}건을 가져왔습니다. "
                            "원본 PDF는 저장하지 않았으며 "
                            f"새 원장 이벤트 {normalization.created:,}건을 만들었습니다.",
                            "success",
                        )
                    else:
                        flash(
                            f"원본 파일과 {result.row_count:,}개 행/항목을 안전하게 저장했습니다. "
                            f"새 원장 이벤트 {normalization.created:,}건을 만들었습니다.",
                            "success",
                        )
                else:
                    flash("이미 가져온 동일한 파일이라 중복 반영하지 않았습니다.", "info")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("imports_page"))
        view_mode = "trash" if request.args.get("view") == "trash" else "archive"
        with open_store() as store:
            source_files = (
                store.list_trashed_source_files()
                if view_mode == "trash"
                else store.list_source_files()
            )
        return render_template(
            "imports.html",
            source_files=source_files,
            view_mode=view_mode,
            delete_csrf_token=delete_csrf_token(),
        )

    @app.post("/imports/preview/<token>/confirm")
    def confirm_import_preview(token: str):
        validate_delete_csrf()
        preview = pending_import_previews.get(token)
        if preview is None:
            flash("가져오기 미리보기가 만료되었습니다. 파일을 다시 선택하세요.", "error")
            return redirect(url_for("imports_page"))
        source_type = str(preview["source_type"])
        password = request.form.get("pdf_password", "")
        if source_type == "UPBIT" and not password:
            flash("확정을 위해 업비트 PDF 비밀번호를 다시 입력하세요.", "error")
            return redirect(url_for("imports_page"))
        analysis = preview["analysis"]
        try:
            with open_store() as store:
                content = bytes(preview["content"])
                filename = str(preview["filename"])
                if source_type == "UPBIT":
                    result = import_upbit_pdf_statement(
                        store,
                        evidence_directory=Path(app.config["EVIDENCE_DIRECTORY"]),
                        original_filename=filename,
                        content=content,
                        password=password,
                    )
                elif source_type == "BITHUMB":
                    result = import_bithumb_pdf_statement(
                        store,
                        evidence_directory=Path(app.config["EVIDENCE_DIRECTORY"]),
                        original_filename=filename,
                        content=content,
                    )
                else:
                    result = import_exchange_csv(
                        store,
                        evidence_directory=Path(app.config["EVIDENCE_DIRECTORY"]),
                        source_type=source_type,
                        document_type=str(preview["document_type"]),
                        original_filename=filename,
                        content=content,
                    )
                if result.created:
                    save_import_conflicts(
                        store,
                        file_id=result.file_id,
                        source_type=source_type,
                        original_filename=filename,
                        details=analysis.conflict_details,
                    )
                    skipped = (*analysis.duplicate_rows, *analysis.conflict_rows)
                    imported_count = keep_new_rows_only(
                        store, file_id=result.file_id, skipped_rows=skipped
                    )
                    normalization = normalize_all_sources(store)
                else:
                    imported_count = 0
                    normalization = normalize_all_sources(store)
            pending_import_previews.pop(token, None)
            flash(
                f"신규 {imported_count:,}건을 가져왔습니다. "
                f"중복 {analysis.duplicate:,}건과 충돌 {analysis.conflict:,}건은 건너뛰었습니다. "
                f"원장 이벤트 {normalization.created:,}건을 생성했습니다.",
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("imports_page"))

    @app.post("/imports/preview/<token>/cancel")
    def cancel_import_preview(token: str):
        validate_delete_csrf()
        pending_import_previews.pop(token, None)
        flash("가져오기 미리보기를 취소했습니다.", "info")
        return redirect(url_for("imports_page"))

    @app.get("/imports/conflicts")
    def import_conflicts():
        status = request.args.get("status", "PENDING").upper()
        if status not in {"PENDING", "KEEP_EXISTING", "ADD_SEPARATE", "ALL"}:
            status = "PENDING"
        with open_store() as store:
            where = "" if status == "ALL" else "WHERE ic.status = ?"
            params = () if status == "ALL" else (status,)
            rows = store.connection.execute(
                f"""
                SELECT ic.*, sf.document_type
                FROM import_conflicts ic
                JOIN source_files sf ON sf.id = ic.source_file_id
                {where}
                ORDER BY CASE WHEN ic.status = 'PENDING' THEN 0 ELSE 1 END, ic.id DESC
                """,
                params,
            ).fetchall()
            conflicts = []
            for row in rows:
                item = dict(row)
                item["differences"] = conflict_field_differences(
                    item["existing_raw_json"], item["incoming_raw_json"]
                )
                conflicts.append(item)
            counts = {
                row["status"]: int(row["count"])
                for row in store.connection.execute(
                    "SELECT status, COUNT(*) AS count FROM import_conflicts GROUP BY status"
                )
            }
        return render_template(
            "import_conflicts.html", conflicts=conflicts, counts=counts,
            status=status, delete_csrf_token=delete_csrf_token(),
        )

    @app.post("/imports/conflicts/<int:conflict_id>/resolve")
    def resolve_import_conflict(conflict_id: int):
        validate_delete_csrf()
        action = request.form.get("action", "")
        if action not in {"KEEP_EXISTING", "ADD_SEPARATE"}:
            abort(400)
        with open_store() as store:
            conflict = store.connection.execute(
                "SELECT * FROM import_conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()
            if conflict is None:
                abort(404)
            if conflict["status"] != "PENDING":
                flash("이미 처리한 충돌입니다.", "info")
                return redirect(url_for("import_conflicts"))
            if action == "ADD_SEPARATE":
                store.connection.execute(
                    """INSERT INTO raw_records (
                        source_file_id, source_row_number, raw_json, row_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        conflict["source_file_id"], conflict["source_row_number"],
                        conflict["incoming_raw_json"], conflict["incoming_row_sha256"],
                        datetime.now(UTC).isoformat(),
                    ),
                )
                store.connection.execute(
                    "UPDATE source_files SET row_count = row_count + 1 WHERE id = ?",
                    (conflict["source_file_id"],),
                )
            store.connection.execute(
                """UPDATE import_conflicts
                SET status = ?, resolution_note = ?, resolved_at_utc = ? WHERE id = ?""",
                (action, request.form.get("note", "").strip(), datetime.now(UTC).isoformat(), conflict_id),
            )
            store.connection.commit()
            if action == "ADD_SEPARATE":
                normalize_all_sources(store)
        flash("새 거래로 별도 추가했습니다." if action == "ADD_SEPARATE" else "기존 거래를 유지했습니다.", "success")
        return redirect(url_for("import_conflicts"))

    @app.get("/imports/<int:file_id>/download")
    def download_source(file_id: int):
        with open_store() as store:
            source = store.source_file(file_id)
        if source is None:
            abort(404)
        evidence_root = Path(app.config["EVIDENCE_DIRECTORY"]).resolve()
        stored_path = Path(source["stored_path"]).resolve()
        if not stored_path.is_relative_to(evidence_root) or not stored_path.is_file():
            abort(404)
        return send_file(
            stored_path,
            as_attachment=True,
            download_name=source["original_filename"],
        )

    @app.post("/imports/<int:file_id>/delete")
    def delete_source(file_id: int):
        validate_delete_csrf()

        evidence_root = Path(app.config["EVIDENCE_DIRECTORY"]).resolve()
        trash_root = Path(app.config["EVIDENCE_TRASH_DIRECTORY"]).resolve()
        with open_store() as store:
            source = store.source_file(file_id)
            if source is None:
                abort(404)
            stored_path = Path(source["stored_path"]).resolve()
            if not stored_path.is_relative_to(evidence_root):
                abort(400)

            deleted_at_utc = datetime.now(UTC).isoformat()
            trash_root.mkdir(parents=True, exist_ok=True)
            deleted_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            safe_name = Path(source["original_filename"]).name
            trash_path = (
                trash_root / f"{deleted_at}_{file_id}_{safe_name}"
            ).resolve()
            if not trash_path.is_relative_to(trash_root):
                abort(400)
            if stored_path.is_file():
                stored_path.replace(trash_path)

            try:
                deleted = store.move_source_file_to_trash(
                    file_id,
                    stored_path=str(trash_path),
                    deleted_at_utc=deleted_at_utc,
                )
            except Exception:
                if trash_path.is_file() and not stored_path.exists():
                    trash_path.replace(stored_path)
                raise
            if deleted is None:
                if trash_path.is_file() and not stored_path.exists():
                    trash_path.replace(stored_path)
                abort(404)
            normalize_all_sources(store)

        flash(
            f"{deleted['original_filename']} 원장을 휴지통으로 이동했습니다. "
            f"원장 이벤트 {deleted['trashed_normalized_events']:,}건은 복구할 수 있습니다.",
            "success",
        )
        return redirect(url_for("imports_page"), code=303)

    @app.post("/imports/<int:file_id>/restore")
    def restore_source(file_id: int):
        validate_delete_csrf()
        evidence_root = Path(app.config["EVIDENCE_DIRECTORY"]).resolve()
        trash_root = Path(app.config["EVIDENCE_TRASH_DIRECTORY"]).resolve()
        with open_store() as store:
            source = store.source_file(file_id, include_deleted=True)
            if source is None or source["deleted_at_utc"] is None:
                abort(404)
            trash_path = Path(source["stored_path"]).resolve()
            if not trash_path.is_relative_to(trash_root) or not trash_path.is_file():
                abort(404)
            evidence_root.mkdir(parents=True, exist_ok=True)
            suffix = Path(source["original_filename"]).suffix.lower()
            restored_name = (
                f"{datetime.now(UTC).strftime('%Y-%m-%d')}_"
                f"{secrets.token_hex(16)}_{source['sha256'][:12]}{suffix}"
            )
            restored_path = (evidence_root / restored_name).resolve()
            if not restored_path.is_relative_to(evidence_root):
                abort(400)
            trash_path.replace(restored_path)
            try:
                restored = store.restore_source_file(
                    file_id, stored_path=str(restored_path)
                )
            except Exception:
                if restored_path.is_file() and not trash_path.exists():
                    restored_path.replace(trash_path)
                raise
            if restored is None:
                if restored_path.is_file() and not trash_path.exists():
                    restored_path.replace(trash_path)
                abort(404)
            normalize_all_sources(store)

        flash(f"{restored['original_filename']} 원장을 복구했습니다.", "success")
        return redirect(url_for("imports_page"), code=303)

    @app.post("/imports/<int:file_id>/permanent-delete")
    def permanently_delete_source(file_id: int):
        validate_delete_csrf()
        trash_root = Path(app.config["EVIDENCE_TRASH_DIRECTORY"]).resolve()
        with open_store() as store:
            source = store.source_file(file_id, include_deleted=True)
            if source is None or source["deleted_at_utc"] is None:
                abort(404)
            trash_path = Path(source["stored_path"]).resolve()
            if not trash_path.is_relative_to(trash_root):
                abort(400)
            deleting_path: Path | None = None
            if trash_path.is_file():
                deleting_path = (
                    trash_root / f".deleting-{file_id}-{secrets.token_hex(12)}"
                ).resolve()
                trash_path.replace(deleting_path)
            try:
                deleted = store.permanently_delete_source_file(file_id)
            except Exception:
                if deleting_path and deleting_path.is_file() and not trash_path.exists():
                    deleting_path.replace(trash_path)
                raise
            if deleted is None:
                if deleting_path and deleting_path.is_file() and not trash_path.exists():
                    deleting_path.replace(trash_path)
                abort(404)
            if deleting_path:
                deleting_path.unlink(missing_ok=True)

        flash(f"{deleted['original_filename']} 원장을 영구삭제했습니다.", "success")
        return redirect(url_for("imports_page", view="trash"), code=303)

    @app.route("/accounts", methods=["GET", "POST"])
    def accounts_page():
        if request.method == "POST":
            validate_delete_csrf()
            network_name = request.form.get("network_name", "").upper()
            network = WALLET_NETWORKS.get(network_name)
            if network is None:
                flash("지원하는 네트워크를 선택하세요.", "error")
                return redirect(url_for("accounts_page"), code=303)
            try:
                with open_store() as store:
                    store.create_personal_wallet_account(
                        wallet_name=request.form.get("wallet_name", ""),
                        wallet_address=request.form.get("wallet_address", ""),
                        network_family=network[1],
                        network_name=network_name,
                        note=request.form.get("note", ""),
                        recorded_at_utc=datetime.now(UTC).isoformat(),
                    )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash("개인 지갑주소를 등록했습니다.", "success")
            return redirect(url_for("accounts_page"), code=303)
        with open_store() as store:
            wallets = [wallet for wallet in store.list_personal_wallets() if wallet.get("id")]
            exchange_wallets = store.list_exchange_wallet_accounts()
        return render_template(
            "accounts.html", wallets=wallets, exchange_wallets=exchange_wallets,
            networks=WALLET_NETWORKS, wallet_exchanges=WALLET_EXCHANGES,
            delete_csrf_token=delete_csrf_token(),
        )

    @app.post("/accounts/<int:account_id>/delete")
    def delete_account(account_id: int):
        validate_delete_csrf()
        with open_store() as store:
            deleted = store.delete_personal_wallet_account(account_id)
        flash("등록 지갑을 삭제했습니다." if deleted else "삭제할 지갑을 찾지 못했습니다.", "success" if deleted else "info")
        return redirect(url_for("accounts_page"), code=303)

    @app.post("/accounts/exchange")
    def create_exchange_account():
        validate_delete_csrf()
        exchange_code = request.form.get("exchange_code", "").upper()
        exchange_name = WALLET_EXCHANGES.get(exchange_code)
        network_name = request.form.get("network_name", "").upper()
        network = WALLET_NETWORKS.get(network_name)
        if exchange_name is None:
            flash("지원하는 거래소를 선택하세요.", "error")
        elif network is None:
            flash("지원하는 네트워크를 선택하세요.", "error")
        else:
            try:
                with open_store() as store:
                    store.create_exchange_wallet_account(
                        exchange_code=exchange_code,
                        exchange_name=exchange_name,
                        wallet_address=request.form.get("wallet_address", ""),
                        network_family=network[1],
                        network_name=network_name,
                        note=request.form.get("note", ""),
                        recorded_at_utc=datetime.now(UTC).isoformat(),
                    )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash(f"{exchange_name} 지갑주소를 등록했습니다.", "success")
        return redirect(url_for("accounts_page"), code=303)

    @app.post("/accounts/exchange/<int:account_id>/delete")
    def delete_exchange_account(account_id: int):
        validate_delete_csrf()
        with open_store() as store:
            deleted = store.delete_exchange_wallet_account(account_id)
        flash(
            "등록한 거래소 지갑을 삭제했습니다." if deleted else "삭제할 거래소 지갑을 찾지 못했습니다.",
            "success" if deleted else "info",
        )
        return redirect(url_for("accounts_page"), code=303)

    @app.get("/ledger")
    def ledger():
        selected_exchange = request.args.get("exchange", "BACKPACK").upper()
        if selected_exchange not in LEDGER_EXCHANGES:
            selected_exchange = "BACKPACK"
        start_date = request.args.get("start", "").strip()
        end_date = request.args.get("end", "").strip()
        asset = request.args.get("asset", "").strip().upper()
        txid = request.args.get("txid", "").strip()
        status = request.args.get("status", "").strip().upper()
        if status not in {"ALL", "NORMALIZED", "NEEDS_REVIEW", "EXCLUDED"}:
            status = "ALL"
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        per_page = 100
        with open_store() as store:
            events, total = store.list_ledger_events(
                source_type=selected_exchange,
                start_date=start_date or None,
                end_date=end_date or None,
                asset=asset or None,
                txid=txid or None,
                status=None if status == "ALL" else status,
                limit=per_page,
                offset=(page - 1) * per_page,
            )
        total_pages = max(1, (total + per_page - 1) // per_page)
        return render_template(
            "ledger.html",
            events=events,
            total=total,
            page=page,
            total_pages=total_pages,
            start_date=start_date,
            end_date=end_date,
            asset=asset,
            txid=txid,
            status=status,
            exchanges=LEDGER_EXCHANGES,
            selected_exchange=selected_exchange,
            selected_exchange_name=LEDGER_EXCHANGES[selected_exchange],
        )

    @app.get("/ledger/<int:event_id>")
    def ledger_detail(event_id: int):
        with open_store() as store:
            event = store.ledger_event(event_id)
        if event is None:
            abort(404)
        return render_template(
            "ledger_detail.html",
            event=event,
            event_labels=REVIEW_EVENT_LABELS,
        )

    @app.get("/reports")
    def reports():
        selected_exchange = request.args.get("exchange", "BACKPACK").upper()
        if selected_exchange not in LEDGER_EXCHANGES:
            selected_exchange = "BACKPACK"
        try:
            period = select_period(request.args)
        except ValueError as exc:
            flash(str(exc), "error")
            period = select_period({})
        with open_store() as store:
            year_options = available_year_options(
                store.connection, source_type=selected_exchange
            )
            rows = asset_performance(
                store,
                start_utc=period.start_utc,
                end_utc=period.end_utc,
                source_type=selected_exchange,
            )
        if period.year_value and int(period.year_value) not in year_options:
            year_options = sorted(
                [*year_options, int(period.year_value)], reverse=True
            )
        currency_code = "USD" if selected_exchange == "BACKPACK" else "KRW"
        return render_template(
            "reports.html",
            period=period,
            year_options=year_options,
            exchanges=LEDGER_EXCHANGES,
            selected_exchange=selected_exchange,
            selected_exchange_name=LEDGER_EXCHANGES[selected_exchange],
            currency_code=currency_code,
            statement=build_income_statement(rows),
            asset_rows=rows,
            **calendar_option_context(period),
        )

    @app.get("/reports/export.xlsx")
    def export_income_statement_excel():
        selected_exchange = request.args.get("exchange", "BACKPACK").upper()
        if selected_exchange not in LEDGER_EXCHANGES:
            selected_exchange = "BACKPACK"
        try:
            period = select_period(request.args)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("reports", exchange=selected_exchange))
        with open_store() as store:
            rows = asset_performance(
                store,
                start_utc=period.start_utc,
                end_utc=period.end_utc,
                source_type=selected_exchange,
            )
        currency_code = "USD" if selected_exchange == "BACKPACK" else "KRW"
        workbook = build_income_statement_workbook(
            period=period,
            exchange_name=LEDGER_EXCHANGES[selected_exchange],
            currency_code=currency_code,
            statement=build_income_statement(rows),
            asset_rows=rows,
        )
        return send_file(
            workbook,
            as_attachment=True,
            download_name=income_statement_filename(period, selected_exchange),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/assets")
    def assets():
        selected_exchange = request.args.get("exchange", "BACKPACK").upper()
        if selected_exchange not in LEDGER_EXCHANGES:
            selected_exchange = "BACKPACK"
        try:
            period = select_period(request.args)
        except ValueError as exc:
            flash(str(exc), "error")
            period = select_period({})
        with open_store() as store:
            year_options = available_year_options(
                store.connection, source_type=selected_exchange
            )
            assets = asset_performance(
                store,
                start_utc=period.start_utc,
                end_utc=period.end_utc,
                source_type=selected_exchange,
            )
        if period.year_value and int(period.year_value) not in year_options:
            year_options = sorted(
                [*year_options, int(period.year_value)], reverse=True
            )
        calendar_options = calendar_option_context(period)
        logos = logo_filenames(Path(app.config["ASSET_LOGO_DIRECTORY"]))
        for item in assets:
            item["logo_filename"] = logos.get(item["asset"])
        return render_template(
            "assets.html",
            assets=assets,
            period=period,
            summary=performance_summary(assets),
            year_options=year_options,
            exchanges=LEDGER_EXCHANGES,
            selected_exchange=selected_exchange,
            selected_exchange_name=LEDGER_EXCHANGES[selected_exchange],
            currency_code="USD" if selected_exchange == "BACKPACK" else "KRW",
            currency_symbol="$" if selected_exchange == "BACKPACK" else "₩",
            **calendar_options,
        )

    @app.get("/assets/export.xlsx")
    def export_assets_excel():
        selected_exchange = request.args.get("exchange", "BACKPACK").upper()
        if selected_exchange not in LEDGER_EXCHANGES:
            selected_exchange = "BACKPACK"
        try:
            period = select_period(request.args)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("assets"))
        with open_store() as store:
            assets = asset_performance(
                store,
                start_utc=period.start_utc,
                end_utc=period.end_utc,
                source_type=selected_exchange,
            )
        logo_root = Path(app.config["ASSET_LOGO_DIRECTORY"])
        logos = logo_filenames(logo_root)
        for item in assets:
            item["logo_filename"] = logos.get(item["asset"])
        workbook = build_asset_performance_workbook(
            period=period,
            rows=assets,
            summary=performance_summary(assets),
            logo_directory=logo_root,
            exchange_name=LEDGER_EXCHANGES[selected_exchange],
            currency_code="USD" if selected_exchange == "BACKPACK" else "KRW",
        )
        return send_file(
            workbook,
            as_attachment=True,
            download_name=excel_filename(period, selected_exchange),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.post("/assets/logos")
    def upload_asset_logos():
        uploads = [
            LogoUpload(filename=upload.filename, stream=upload.stream)
            for upload in request.files.getlist("logo_files")
            if upload.filename
        ]
        try:
            saved = save_asset_logos(
                Path(app.config["ASSET_LOGO_DIRECTORY"]), uploads
            )
            flash(f"자산 로고 {len(saved):,}개를 저장했습니다.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("assets"))

    @app.post("/assets/logos/auto")
    def auto_download_asset_logos():
        with open_store() as store:
            assets = asset_performance(store, source_type="BACKPACK")
        try:
            result = download_missing_asset_logos(
                directory=Path(app.config["ASSET_LOGO_DIRECTORY"]),
                assets=assets,
            )
            if result.downloaded:
                flash(
                    f"없는 로고 {result.downloaded:,}개를 자동으로 저장했습니다. "
                    f"실패 {result.failed:,}개입니다.",
                    "success" if result.failed == 0 else "info",
                )
            elif result.failed:
                flash(
                    f"자동 로고 수집에 실패했습니다: {result.failures[0]}",
                    "error",
                )
            else:
                flash("현재 거래 자산의 로고가 모두 준비되어 있습니다.", "info")
        except Exception as exc:
            flash(f"자동 로고 수집을 완료하지 못했습니다: {exc}", "error")
        return redirect(url_for("assets"))

    @app.get("/assets/logos/<path:filename>")
    def asset_logo(filename: str):
        logo_root = Path(app.config["ASSET_LOGO_DIRECTORY"]).resolve()
        logo_path = (logo_root / Path(filename).name).resolve()
        if not logo_path.is_relative_to(logo_root) or not logo_path.is_file():
            abort(404)
        return send_file(logo_path, conditional=True)

    return app


def main() -> None:
    app = create_app()
    address = "http://127.0.0.1:8765"
    print(f"코인어카운트를 시작합니다: {address}")
    print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    if os.getenv("PORTFOLIO_LAUNCH") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(address)).start()
    app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)
