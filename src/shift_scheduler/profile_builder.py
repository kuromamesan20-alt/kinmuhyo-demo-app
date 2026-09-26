"""CSVの行 -> StaffProfile への変換。"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence

from .condition_parser import ConditionParser
from .models import StaffProfile
from .shifts import EARLY_SHIFTS, LATE_SHIFTS
from .text_utils import normalize

_QUALIFICATION_SPLIT = re.compile(r"[、,／/・\n]+")


def build_profiles(rows: Sequence[Dict[str, str]]) -> List[StaffProfile]:
    """CSVの全行から、自由文を解釈済みの StaffProfile 一覧を作る。"""
    roster = [row.get("name", "") for row in rows]
    # 条件文の中では「スタッフW」のような仮名でスタッフが指されている。
    # 苗字に切り替えた後もこの書き方は残っているので、対応表を渡して解決する。
    aliases: Dict[str, str] = {}
    collisions: Dict[str, List[str]] = {}
    for row in rows:
        alias, display = row.get("alias", ""), row.get("name", "")
        if not alias or not display or alias == display:
            continue
        if alias in aliases and aliases[alias] != display:
            # 同じ仮名が2人に付いていると、同席制約の相手を取り違える。
            # 黙って後勝ちにせず、両方の苗字を控えて後で確認に回す。
            collisions.setdefault(alias, [aliases[alias]]).append(display)
            continue
        aliases[alias] = display
    parser = ConditionParser(roster, aliases)

    profiles: List[StaffProfile] = []
    for row in rows:
        profile = _base_profile(row)
        parser.apply(profile)
        for alias, names in collisions.items():
            profile.flag_review(
                f"仮名「{alias}」が複数の方({'、'.join(names)})に付いています。"
                "条件文でこの仮名が使われていると、同席の相手を取り違えます",
                code="alias_collision",
                label=f"alias/{alias}",
            )
        _apply_early_late_columns(profile, row)
        _cross_check(profile)
        profiles.append(profile)
    return profiles


def _apply_early_late_columns(profile: StaffProfile, row: Dict[str, str]) -> None:
    """CSVの「早出」「遅出」列を、入れる日勤帯コードに反映する。

    これらの列には時間が書かれていないため、条件文に時間の記載がある人は
    そちらを正として、この列は使わない。時間の手がかりが無い人だけ
    EARLY_SHIFTS / LATE_SHIFTS を外し、解釈が正しいか確認に回す。
    """
    if profile.fixed_time_slots:
        # 曜日ごとに勤務時間が決まっている人は、番号のシフトには入らない。
        # 勤務表のセルには時間をそのまま書く。
        profile.available_day_shifts = []
        return
    if profile.work_hours:
        return  # 勤務できる時間帯が分かっている人。番号の可否では判断しない
    if profile.night_shift_exclusive or profile.late_night_only:
        return  # 夜勤・深夜専従。日勤帯の可否は関係ない
    if profile.time_restricted:
        return  # 条件文の時間指定で既に絞り込み済み。時間の記載が優先

    for column, shifts, label in (
        ("can_early", EARLY_SHIFTS, "早出"),
        ("can_late", LATE_SHIFTS, "遅出"),
    ):
        if _to_bool(row.get(column, "")):
            continue
        before = list(profile.available_day_shifts)
        profile.restrict_day_shifts([c for c in before if c not in shifts])
        if profile.available_day_shifts != before:
            profile.flag_review(
                f"CSVの「{label}」列が不可だったので{'・'.join(shifts)}を外しました。"
                "この解釈で合っているか確認してください",
                code="early_late_column",
                label=label,
                removed=list(shifts),
            )


def _base_profile(row: Dict[str, str]) -> StaffProfile:
    return StaffProfile(
        staff_id=row.get("submission_id", "") or row.get("name", ""),
        name=normalize(row.get("name", "")),
        sheet_label=row.get("sheet_label", ""),
        role=row.get("role", ""),
        employment_type=row.get("employment_type", ""),
        social_insurance=row.get("social_insurance", ""),
        weekly_work_days=_to_int(row.get("weekly_work_days", "")),
        can_night=_to_bool(row.get("can_night", "")),
        # CSVには深夜の列がない。既定は夜勤列に合わせ、条件文に記載があれば上書きする。
        can_late_night=_to_bool(row.get("can_night", "")),
        qualifications=_split_qualifications(row.get("qualifications", "")),
        raw_conditions=row.get("conditions", ""),
        raw_notes=row.get("notes", ""),
    )


def _cross_check(profile: StaffProfile) -> None:
    """列の可否と自由文の内容が食い違っていないかを点検する。"""
    if not profile.can_night and (
        profile.night_shift_count or profile.late_night_shift_count
    ):
        profile.flag_review(
            "夜勤列が「不可」ですが、条件文に夜勤/深夜の回数が書かれています",
            code="night_mismatch",
        )
    if not profile.can_late_night and profile.late_night_shift_count:
        profile.flag_review(
            "深夜が「不可」ですが、条件文に深夜の回数が書かれています",
            code="night_mismatch",
        )
    exclusive_flags = [
        name
        for name, value in (
            ("夜勤専従", profile.night_shift_exclusive),
            ("深夜のみ", profile.late_night_only),
            ("日勤のみ", profile.day_shift_only),
        )
        if value
    ]
    if len(exclusive_flags) > 1:
        profile.flag_review(
            f"勤務形態の限定が複数読み取られました({' / '.join(exclusive_flags)})。"
            "どれが正しいか確認してください",
            code="exclusive_conflict",
            candidates=exclusive_flags,
        )
    if profile.weekly_work_days is None:
        profile.flag_review("週勤務日数が読み取れませんでした", code="weekly_days")
    if (
        not profile.available_day_shifts
        and not profile.writes_own_hours
        and not (profile.night_shift_exclusive or profile.late_night_only)
    ):
        profile.flag_review(
            "入れる日勤帯が1つも残りませんでした。条件が厳しすぎないか確認してください",
            code="no_shift_left",
        )
    if profile.is_support_staff and not profile.writes_own_hours:
        profile.flag_review(
            "介護補助ですが、決まった勤務時間(勤務時間 または 曜日ごとの勤務時間)が"
            "読み取れませんでした。このままだと勤務表では全日「公」(全休)になります。"
            "本来の勤務時間を条件文またはExcelの「勤務時間」列に入れてください",
            code="support_no_hours",
        )


def _to_int(value: str):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _to_bool(value: str) -> bool:
    """「可」=True、「不可」=False。それ以外は安全側に倒して False。"""
    return (value or "").strip() == "可"


def _split_qualifications(value: str) -> List[str]:
    return [part.strip() for part in _QUALIFICATION_SPLIT.split(value or "") if part.strip()]
