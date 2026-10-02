from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class PeriodSelection:
    period_type: str
    start_utc: datetime | None
    end_utc: datetime | None
    label: str
    year_value: str
    month_value: str
    week_value: str
    day_value: str


def _at_seoul_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=SEOUL)


def _parse_optional_integer(
    values: Mapping[str, str], name: str, minimum: int, maximum: int
) -> int | None:
    raw = (values.get(name) or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError("기간 입력값이 올바르지 않습니다.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError("기간 입력값이 올바르지 않습니다.")
    return parsed


def _next_month(year: int, month: int) -> date:
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def month_week_options(year: int, month: int) -> list[tuple[int, str]]:
    last_day = calendar.monthrange(year, month)[1]
    options: list[tuple[int, str]] = []
    for week in range(5, 0, -1):
        start_day = (week - 1) * 7 + 1
        if start_day > last_day:
            continue
        end_day = min(start_day + 6, last_day)
        options.append((week, f"{start_day}일 ~ {end_day}일"))
    return options


def month_day_options(year: int, month: int) -> list[int]:
    return list(range(calendar.monthrange(year, month)[1], 0, -1))


def select_period(
    values: Mapping[str, str], *, now: datetime | None = None
) -> PeriodSelection:
    current = (now or datetime.now(SEOUL)).astimezone(SEOUL)
    year = _parse_optional_integer(values, "year", 2000, 2100)
    month = _parse_optional_integer(values, "month", 1, 12)
    week = _parse_optional_integer(values, "week", 1, 5)
    day = _parse_optional_integer(values, "day", 1, 31)

    if year is None:
        return PeriodSelection(
            period_type="all",
            start_utc=None,
            end_utc=None,
            label="전체 기간",
            year_value="",
            month_value="",
            week_value="",
            day_value="",
        )

    year_value = str(year)
    if month is None:
        start_day = date(year, 1, 1)
        end_day = date(year + 1, 1, 1)
        period_type = "year"
        label = f"{year}년"
        month_value = week_value = day_value = ""
    else:
        last_day = calendar.monthrange(year, month)[1]
        month_value = str(month)
        if day is not None:
            if day > last_day:
                raise ValueError("선택한 월에 존재하지 않는 날짜입니다.")
            start_day = date(year, month, day)
            end_day = start_day + timedelta(days=1)
            period_type = "day"
            label = f"{year}년 {month}월 {day}일"
            week_value = ""
            day_value = str(day)
        elif week is not None:
            first_day = (week - 1) * 7 + 1
            if first_day > last_day:
                raise ValueError("선택한 월에 존재하지 않는 주간입니다.")
            final_day = min(first_day + 6, last_day)
            start_day = date(year, month, first_day)
            end_day = date(year, month, final_day) + timedelta(days=1)
            period_type = "week"
            label = f"{year}년 {month}월 {first_day}~{final_day}일 ({week}주차)"
            week_value = str(week)
            day_value = ""
        else:
            start_day = date(year, month, 1)
            end_day = _next_month(year, month)
            period_type = "month"
            label = f"{year}년 {month}월"
            week_value = day_value = ""

    return PeriodSelection(
        period_type=period_type,
        start_utc=_at_seoul_midnight(start_day).astimezone(UTC),
        end_utc=_at_seoul_midnight(end_day).astimezone(UTC),
        label=label,
        year_value=year_value,
        month_value=month_value,
        week_value=week_value,
        day_value=day_value,
    )


def calendar_option_context(
    period: PeriodSelection, *, now: datetime | None = None
) -> dict[str, object]:
    current = (now or datetime.now(SEOUL)).astimezone(SEOUL)
    year = int(period.year_value) if period.year_value else current.year
    month = int(period.month_value) if period.month_value else current.month
    return {
        "display_year": year,
        "display_month": month,
        "week_options": month_week_options(year, month),
        "day_options": month_day_options(year, month),
    }


def available_year_options(
    connection,
    *,
    now: datetime | None = None,
    source_type: str | None = None,
) -> list[int]:
    current_year = (now or datetime.now(SEOUL)).astimezone(SEOUL).year
    source_filter = "AND ne.source_type = ?" if source_type else ""
    parameters = (source_type.upper(),) if source_type else ()
    row = connection.execute(
        f"""
        SELECT MIN(occurred_at_utc) AS first_at, MAX(occurred_at_utc) AS last_at
        FROM normalized_events ne
        LEFT JOIN raw_records rr ON rr.id = ne.raw_record_id
        LEFT JOIN source_files sf ON sf.id = rr.source_file_id
        WHERE ne.occurred_at_utc IS NOT NULL
          AND (ne.raw_record_id IS NULL OR sf.deleted_at_utc IS NULL)
          {source_filter}
        """,
        parameters,
    ).fetchone()
    years = {current_year}
    for value in (row["first_at"], row["last_at"]):
        if not value:
            continue
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        years.add(parsed.astimezone(SEOUL).year)
    earliest = min(min(years), current_year - 10)
    latest = max(years)
    return list(range(latest, earliest - 1, -1))
