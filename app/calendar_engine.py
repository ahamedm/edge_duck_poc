"""Financial calendar engine.

A fiscal_calendar has start_month 1 (Jan-Dec) or 4 (Apr-Mar).
Periods are numbered 1..12 within the fiscal year:
  - Jan-Dec:  FY2026 P01 = Jan 2026 ... P12 = Dec 2026
  - Apr-Mar:  FY2026 P01 = Apr 2026 ... P12 = Mar 2027
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Period:
    period_num: int
    label: str
    month: int
    start_date: date
    end_date: date


def validate_start_month(start_month: int) -> int:
    if start_month not in (1, 4):
        raise ValueError(f"start_month must be 1 (Jan-Dec) or 4 (Apr-Mar), got {start_month}")
    return start_month


def period_month(start_month: int, period_num: int) -> int:
    """Real calendar month for period N."""
    validate_start_month(start_month)
    return ((start_month + period_num - 2) % 12) + 1


def fiscal_year_for_date(start_month: int, d: date) -> int:
    """Fiscal year a real date belongs to."""
    validate_start_month(start_month)
    if start_month == 1:
        return d.year
    return d.year if d.month >= start_month else d.year - 1


def month_end(y: int, m: int) -> date:
    return date(y + (1 if m == 12 else 0), (m % 12) + 1, 1) - timedelta(days=1)


def generate_periods(start_month: int, fiscal_year: int) -> list[Period]:
    validate_start_month(start_month)
    months_in_fy = 12 - start_month + 1  # periods of this FY falling inside FY calendar year
    out = []
    for p in range(1, 13):
        m = period_month(start_month, p)
        cal_year = fiscal_year + (0 if start_month == 1 or p <= months_in_fy else 1)
        start = date(cal_year, m, 1)
        out.append(
            Period(
                period_num=p,
                label=f"P{p:02d} {start.strftime('%b %Y')}",
                month=m,
                start_date=start,
                end_date=month_end(cal_year, m),
            )
        )
    return out
