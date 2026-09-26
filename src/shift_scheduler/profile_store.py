"""StaffProfile の YAML 入出力。

この YAML が「人がレビューする画面」を兼ねる。専用UIは作らず、
needs_review が true の項目をエディタで直してもらう運用にする。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Sequence

import yaml

from .models import (
    WEEKDAY_NAMES,
    FixedTimeSlot,
    PairConstraint,
    ReviewItem,
    StaffProfile,
)


def save_profiles(profiles: Sequence[StaffProfile], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "_使い方": [
            "needs_review が true の人を上から確認し、内容を直してください。",
            "直したら review_reasons を空にし、ファイル名から _draft を外して保存します。",
            "曜日は 0=月 1=火 2=水 3=木 4=金 5=土 6=日 です。",
            "unparsed_notes は自動で読み取れなかった原文です。必要なら手で条件に反映してください。",
        ],
        "staff": [_to_dict(profile) for profile in profiles],
    }
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            document, handle, allow_unicode=True, sort_keys=False, width=200
        )


def load_profiles(path: Path) -> List[StaffProfile]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    return [_from_dict(entry) for entry in document.get("staff", [])]


def save_requests(requests: Sequence[Any], path: Path) -> None:
    """その月の希望(StaffRequests)をYAMLに保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "_使い方": [
            "その月の希望休・希望出勤です。日にち -> 記号。",
            "「公」は希望休、それ以外はその勤務での希望出勤です。",
        ],
        "requests": [
            {
                "staff_id": request.staff_id,
                "name": request.name,
                "entries": {int(day): mark for day, mark in sorted(request.entries.items())},
            }
            for request in requests
        ],
    }
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False, width=200)


def load_requests(path: Path) -> List[Any]:
    from .request_sheet import StaffRequests

    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    return [
        StaffRequests(
            staff_id=entry.get("staff_id", ""),
            name=entry.get("name", ""),
            entries={int(day): mark for day, mark in (entry.get("entries") or {}).items()},
        )
        for entry in document.get("requests", [])
    ]


def _to_dict(profile: StaffProfile) -> Dict[str, Any]:
    data = asdict(profile)
    # 読み手のために曜日番号の意味を併記する
    data["needs_review"] = profile.needs_review
    data["_固定休み曜日"] = [WEEKDAY_NAMES[d] for d in profile.fixed_off_weekdays]
    data["night_shift_count"] = list(profile.night_shift_count or []) or None
    data["late_night_shift_count"] = list(profile.late_night_shift_count or []) or None
    data["monthly_off_quota"] = {
        WEEKDAY_NAMES[weekday]: count
        for weekday, count in profile.monthly_off_quota.items()
    }
    data["work_hours"] = list(profile.work_hours) if profile.work_hours else None
    data["review_reasons"] = profile.review_reasons  # 読み手向け(戻すのは review_items)
    return data


# プロパティなので、YAMLに書いてあっても読み戻すときは無視する
_READ_ONLY_KEYS = ("needs_review", "review_reasons", "writes_own_hours")


def _from_dict(entry: Dict[str, Any]) -> StaffProfile:
    entry = {k: v for k, v in entry.items() if not k.startswith("_")}
    for key in _READ_ONLY_KEYS:
        entry.pop(key, None)

    profile = StaffProfile(
        staff_id=entry.get("staff_id", ""),
        name=entry.get("name", ""),
    )
    for key, value in entry.items():
        if key in ("staff_id", "name"):
            continue
        if not hasattr(profile, key):
            continue
        setattr(profile, key, _convert(key, value))
    return profile


def _convert(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in ("night_shift_count", "late_night_shift_count"):
        return tuple(value)
    if key == "monthly_off_quota":
        return {
            WEEKDAY_NAMES.index(name) if isinstance(name, str) else int(name): count
            for name, count in value.items()
        }
    if key == "work_hours":
        return tuple(value)
    if key == "fixed_time_slots":
        return [FixedTimeSlot(**item) for item in value]
    if key == "pair_constraints":
        return [PairConstraint(**item) for item in value]
    if key == "weekday_unavailable_shifts":
        return {int(w): list(codes) for w, codes in value.items()}
    if key == "review_items":
        return [ReviewItem(**item) for item in value]
    return value
