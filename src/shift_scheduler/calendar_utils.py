"""対象月の日付・曜日を扱う。"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import List

from .models import WEEKDAY_NAMES


@dataclass(frozen=True)
class Day:
    """勤務表の1日分。"""

    date: date

    @property
    def day(self) -> int:
        return self.date.day

    @property
    def weekday(self) -> int:
        """0=月 ... 6=日"""
        return self.date.weekday()

    @property
    def weekday_name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    @property
    def is_weekend(self) -> bool:
        return self.weekday >= 5


def month_days(year: int, month: int) -> List[Day]:
    """その月の全日を返す。"""
    _, last = calendar.monthrange(year, month)
    return [Day(date(year, month, day)) for day in range(1, last + 1)]


def month_label(year: int, month: int) -> str:
    return f"{year}年{month}月"
