"""StaffProfile の曜日限定シフト操作(shifts_allowed_on / block_shifts_on_weekday)のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from shift_scheduler.models import StaffProfile  # noqa: E402


def test_shifts_allowed_on_reflects_weekday_limit():
    """曜日限定の制限が shifts_allowed_on に反映されること。制限の無い曜日には影響しない。"""
    profile = StaffProfile(staff_id="x", name="本人")
    profile.block_shifts_on_weekday(1, ["日①", "日②"])  # 火曜だけ制限

    assert "日①" not in profile.shifts_allowed_on(1)
    assert "日②" not in profile.shifts_allowed_on(1)
    # 木曜(3)は制限対象外なので、通常どおり入れる
    assert "日①" in profile.shifts_allowed_on(3)
    assert "日②" in profile.shifts_allowed_on(3)


def test_block_shifts_on_weekday_does_not_duplicate():
    """同じコードを2回足しても重複しないこと。"""
    profile = StaffProfile(staff_id="x", name="本人")
    profile.block_shifts_on_weekday(1, ["日①"])
    profile.block_shifts_on_weekday(1, ["日①"])
    assert profile.weekday_unavailable_shifts[1] == ["日①"]
