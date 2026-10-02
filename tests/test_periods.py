from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backpack_tax.db import TaxStore
from backpack_tax.periods import (
    SEOUL,
    available_year_options,
    month_week_options,
    select_period,
)


def test_year_only_selects_full_year() -> None:
    period = select_period({"year": "2024"})

    assert period.period_type == "year"
    assert period.label == "2024년"
    assert period.start_utc == datetime(2023, 12, 31, 15, tzinfo=UTC)
    assert period.end_utc == datetime(2024, 12, 31, 15, tzinfo=UTC)


def test_year_and_month_select_full_month() -> None:
    period = select_period({"year": "2025", "month": "5"})

    assert period.period_type == "month"
    assert period.label == "2025년 5월"
    assert period.start_utc == datetime(2025, 4, 30, 15, tzinfo=UTC)
    assert period.end_utc == datetime(2025, 5, 31, 15, tzinfo=UTC)


def test_second_week_is_days_eight_through_fourteen() -> None:
    period = select_period({"year": "2026", "month": "5", "week": "2"})

    assert period.period_type == "week"
    assert period.label == "2026년 5월 8~14일 (2주차)"
    assert period.start_utc == datetime(2026, 5, 7, 15, tzinfo=UTC)
    assert period.end_utc == datetime(2026, 5, 14, 15, tzinfo=UTC)


def test_day_selection_takes_priority_when_week_is_omitted() -> None:
    period = select_period({"year": "2023", "month": "6", "day": "14"})

    assert period.period_type == "day"
    assert period.label == "2023년 6월 14일"
    assert period.start_utc == datetime(2023, 6, 13, 15, tzinfo=UTC)
    assert period.end_utc == datetime(2023, 6, 14, 15, tzinfo=UTC)


def test_week_options_are_descending_and_end_at_month_end() -> None:
    assert month_week_options(2026, 2) == [
        (4, "22일 ~ 28일"),
        (3, "15일 ~ 21일"),
        (2, "8일 ~ 14일"),
        (1, "1일 ~ 7일"),
    ]
    assert month_week_options(2026, 5)[0] == (5, "29일 ~ 31일")


def test_default_is_all_time() -> None:
    period = select_period({}, now=datetime(2026, 8, 24, tzinfo=SEOUL))

    assert period.period_type == "all"
    assert period.label == "전체 기간"
    assert period.start_utc is None


def test_invalid_day_is_rejected() -> None:
    with pytest.raises(ValueError, match="존재하지 않는 날짜"):
        select_period({"year": "2026", "month": "2", "day": "30"})


def test_year_dropdown_provides_recent_ten_years_in_descending_order(tmp_path) -> None:
    with TaxStore(tmp_path / "tax.db") as store:
        years = available_year_options(
            store.connection, now=datetime(2026, 8, 24, tzinfo=SEOUL)
        )

    assert years == list(range(2026, 2015, -1))
    assert years.index(2024) < years.index(2023)
