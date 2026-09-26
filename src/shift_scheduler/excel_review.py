"""確認・修正用のExcelシートの書き出しと読み戻し。

YAMLは機械向けで確認しづらいため、普段使っているスプレッドシートと同じ感覚で
直せるようにする。読み戻しは元のYAMLを土台にし、**シートにある列だけ**を
上書きするので、シートに出していない情報(原文など)は失われない。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .facility import facility
from .models import (
    WEEKDAY_NAMES,
    FixedTimeSlot,
    PairConstraint,
    StaffProfile,
)
from .shifts import DAY_SHIFT_CODES, DAY_SHIFTS

SHEET_NAME = "勤務条件"
LEGEND_SHEET = "書き方の凡例"

_WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAY_NAMES)}
_SEPARATOR = "・"
_YES, _NO = "はい", "いいえ"

# 見た目
_HEAD_FILL = PatternFill("solid", fgColor="2F5D8C")
_LOCKED_FILL = PatternFill("solid", fgColor="EDF0F4")
_ATTENTION_FILL = PatternFill("solid", fgColor="FBF1E3")
_ANSWER_FILL = PatternFill("solid", fgColor="EAF3EA")  # お客様からご回答をいただいた欄
_THIN = Side(style="thin", color="D9DEE5")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


class ReviewSheetError(ValueError):
    """シートの記入内容が読み取れない場合に送出する。"""


# --- 列の定義 ------------------------------------------------------------------
# (見出し, 属性名, 幅, 編集可, 入力候補)
_COLUMNS: List[Tuple[str, Optional[str], int, bool, Optional[Sequence[str]]]] = [
    ("ID", "staff_id", 10, False, None),
    ("名前", "name", 12, False, None),
    ("勤務表の表記", "sheet_label", 12, True, None),
    ("職種", "role", 14, False, None),
    ("ユニット", "unit", 10, True, ("",) + facility().units),
    ("介護補助", "is_support_staff", 9, True, (_YES, _NO)),
    ("雇用形態", "employment_type", 15, False, None),
    ("顧客が書いた原文", "_raw", 46, False, None),
    ("週の勤務日数", "weekly_work_days", 12, True, None),
] + [
    # 日勤帯は1コード1列。可/不可 のプルダウンで選ぶ。
    (f"{code}\n{DAY_SHIFTS[code][0]}-{DAY_SHIFTS[code][1]}", f"_shift_{code}", 9, True, ("可", "不可"))
    for code in DAY_SHIFT_CODES
] + [
    ("勤務時間(番号を使わない人)", "work_hours", 20, True, None),
    ("曜日限定で入れないシフト", "weekday_unavailable_shifts", 28, True, None),
    ("○ 夜勤", "can_night", 8, True, ("可", "不可")),
    ("◉ 深夜", "can_late_night", 8, True, ("可", "不可")),
    # 深夜は原則として介護職が入る。看護職で入れる人だけ「可」にする。
    ("看護職だが深夜可", "can_late_night_as_nurse", 14, True, (_YES, _NO)),
    ("夜勤 最少回数", "_night_min", 12, True, None),
    ("夜勤 最多回数", "_night_max", 12, True, None),
    ("深夜 最少回数", "_late_night_min", 12, True, None),
    ("深夜 最多回数", "_late_night_max", 12, True, None),
    ("毎週の固定休み", "fixed_off_weekdays", 16, True, None),
    ("月ごとの休み", "monthly_off_quota", 16, True, None),
    ("曜日ごとの勤務時間", "fixed_time_slots", 30, True, None),
    ("夜勤に入る曜日", "night_weekdays", 14, True, None),
    ("勤務する曜日(固定)", "fixed_work_weekdays", 16, True, None),
    ("勤務形態の限定", "_exclusive", 14, True, ("", "夜勤専従", "深夜のみ", "日勤のみ")),
    ("日勤責任者", "day_responsible", 12, True, ("", "可", "不可", "条件付き可")),
    ("師長", "is_head_nurse", 8, True, (_YES, _NO)),
    ("休職開始(年月)", "leave_from", 14, True, None),
    ("夜勤で同席不可", "_no_pair", 22, True, None),
    ("夜勤で同席上限", "_max_pair", 22, True, None),
    ("土日どちらか休み希望", "weekend_off_either", 12, True, (_YES, _NO)),
    ("夜勤・深夜を希望", "preferred_night", 12, True, (_YES, _NO)),
    ("早出は避ける", "avoid_early", 12, True, (_YES, _NO)),
    ("曜日おまかせ可", "random_days_allowed", 12, True, (_YES, _NO)),
    ("希望した日のみ勤務", "works_only_on_request", 14, True, (_YES, _NO)),
    ("自動で読めなかった文", "_unparsed", 34, False, None),
    ("確認してほしいこと", "_reasons", 46, False, None),
    ("お客様のご回答", "_customer_answers", 46, False, None),
    ("確認済み", "_reviewed", 10, True, (_YES, _NO)),
]

_LEGEND = [
    ("シフトの記号", ""),
    ("日①〜日⑨", "　".join(f"{c} {DAY_SHIFTS[c][0]}-{DAY_SHIFTS[c][1]}" for c in DAY_SHIFT_CODES)),
    ("○ / △", "夜勤の入り / 明け。夜勤は ○→△→公 の3日セットになります"),
    ("◉ / 公", "深夜の入り / 公休。深夜は ◉→公 の2日セットになります"),
    ("", ""),
    ("この表の使い方", ""),
    ("", "灰色の列は読み取り専用です。白い列を直してください。"),
    ("", "「ID」列は本人を特定するためのものです。絶対に書き換えないでください。"),
    ("", "直したら「確認済み」を「はい」にしてください。それが確認完了の印になります。"),
    ("", "空欄にすれば「条件なし」になります。"),
    ("", ""),
    ("列ごとの書き方", "例"),
    ("週の勤務日数", "5"),
    ("夜勤 最少回数 / 最多回数", "2 と 3 (月2〜3回の意味)。1回だけなら両方に同じ数字"),
    ("毎週の固定休み", "水・土・日"),
    ("月ごとの休み", "金=2 (金曜に月2回休み)。複数なら 金=2・水=1"),
    ("日①〜日⑨の各列", "その番号のシフトに入れるなら「可」、入れないなら「不可」"),
    ("勤務時間(番号を使わない人)", "09:00-17:00 (パートなど、番号ではなく時間を書く人だけ)"),
    ("曜日ごとの勤務時間", "火 09:00-15:00・水 09:00-13:00"),
    ("夜勤に入る曜日", "金"),
    ("勤務する曜日(固定)", "火"),
    ("勤務形態の限定", "夜勤専従 / 深夜のみ / 日勤のみ のどれか。該当なしは空欄"),
    ("夜勤で同席不可", "スタッフE・スタッフW"),
    ("夜勤で同席上限", "スタッフA=1・スタッフE=1 (月1回までの意味)"),
    ("", ""),
    ("師長", "師長の方を「はい」にしてください。師長が出勤している日は、その方が責任者を担います"),
    ("休職開始(年月)", "2026-09 のように入れると、その月以降は勤務表に入りません。復職したら空欄に戻します"),
    ("ユニット", " / ".join(facility().units) + " のどれか。師長と介護補助は空欄"),
    ("介護補助", "環境整備や物品補充の方。日勤帯の人数にも夜勤の輪番にも入りません"),
    ("希望した日のみ勤務", "「はい」にすると、希望出勤が入っている日だけ勤務します。入っていない日は公休です"),
    ("曜日限定で入れないシフト", "火=日①/日②・水=日①/日② (その曜日だけ入れないシフト)"),
    ("", "師長が休みの日は、日勤責任者が「可」の人を1人、責任者(「せ」)として立てます"),
    ("", ""),
    ("注意", "同席の制約は絶対に守るルールとして扱います。名前が正しいか必ず確認してください。"),
]


# --- 書き出し ------------------------------------------------------------------


def export_review_sheet(profiles: Sequence[StaffProfile], path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME

    for index, (header, _, width, editable, _) in enumerate(_COLUMNS, 1):
        cell = sheet.cell(row=1, column=index, value=header)
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = _HEAD_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = _BORDER
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row_index, profile in enumerate(profiles, 2):
        for col_index, (_, attr, _, editable, _) in enumerate(_COLUMNS, 1):
            cell = sheet.cell(row=row_index, column=col_index, value=_cell_value(profile, attr))
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = _BORDER
            if not editable:
                cell.fill = _LOCKED_FILL
        if profile.needs_review:
            sheet.cell(row=row_index, column=_column_index("_reasons")).fill = _ATTENTION_FILL
        if profile.customer_answers:
            sheet.cell(
                row=row_index, column=_column_index("_customer_answers")
            ).fill = _ANSWER_FILL

    _add_validations(sheet, len(profiles))
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(_COLUMNS))}{len(profiles) + 1}"

    _write_legend(workbook)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _write_legend(workbook: Workbook) -> None:
    legend = workbook.create_sheet(LEGEND_SHEET)
    legend.column_dimensions["A"].width = 28
    legend.column_dimensions["B"].width = 70
    for row_index, (label, example) in enumerate(_LEGEND, 1):
        label_cell = legend.cell(row=row_index, column=1, value=label)
        label_cell.font = Font(bold=True)
        legend.cell(row=row_index, column=2, value=example).alignment = Alignment(
            wrap_text=True, vertical="top"
        )


def _add_validations(sheet, row_count: int) -> None:
    if row_count == 0:
        return
    for index, (_, _, _, editable, choices) in enumerate(_COLUMNS, 1):
        if not (editable and choices):
            continue
        validation = DataValidation(
            type="list", formula1='"' + ",".join(choices) + '"', allow_blank=True
        )
        sheet.add_data_validation(validation)
        letter = get_column_letter(index)
        validation.add(f"{letter}2:{letter}{row_count + 1}")


def _column_index(attr: str) -> int:
    for index, (_, name, _, _, _) in enumerate(_COLUMNS, 1):
        if name == attr:
            return index
    raise KeyError(attr)


def _cell_value(profile: StaffProfile, attr: Optional[str]) -> Any:
    if attr == "_raw":
        return "\n".join(part for part in (profile.raw_conditions, profile.raw_notes) if part)
    if attr == "_unparsed":
        return "\n".join(profile.unparsed_notes)
    if attr == "_reasons":
        return "\n".join(profile.review_reasons)
    if attr == "_customer_answers":
        return "\n".join(profile.customer_answers.values())
    if attr == "_reviewed":
        return _NO if profile.needs_review else _YES
    if attr == "_night_min":
        return profile.night_shift_count[0] if profile.night_shift_count else None
    if attr == "_night_max":
        return profile.night_shift_count[1] if profile.night_shift_count else None
    if attr == "_late_night_min":
        return profile.late_night_shift_count[0] if profile.late_night_shift_count else None
    if attr == "_late_night_max":
        return profile.late_night_shift_count[1] if profile.late_night_shift_count else None
    if attr == "_exclusive":
        for name, value in (
            ("夜勤専従", profile.night_shift_exclusive),
            ("深夜のみ", profile.late_night_only),
            ("日勤のみ", profile.day_shift_only),
        ):
            if value:
                return name
        return ""
    if attr == "_no_pair":
        return _SEPARATOR.join(
            c.other_staff for c in profile.pair_constraints if c.kind == "no_pair_night"
        )
    if attr == "_max_pair":
        return _SEPARATOR.join(
            f"{c.other_staff}={c.max_count}"
            for c in profile.pair_constraints
            if c.kind == "max_shared_night"
        )

    if attr.startswith("_shift_"):
        return "可" if attr[len("_shift_") :] in profile.available_day_shifts else "不可"

    value = getattr(profile, attr)
    if attr in ("can_night", "can_late_night"):
        return "可" if value else "不可"
    if attr == "work_hours":
        return f"{value[0]}-{value[1]}" if value else ""
    if attr in (
        "weekend_off_either",
        "preferred_night",
        "avoid_early",
        "random_days_allowed",
        "is_head_nurse",
        "is_support_staff",
        "can_late_night_as_nurse",
        "works_only_on_request",
    ):
        return _YES if value else _NO
    if attr in ("fixed_off_weekdays", "night_weekdays", "fixed_work_weekdays"):
        return _SEPARATOR.join(WEEKDAY_NAMES[w] for w in value)
    if attr == "monthly_off_quota":
        return _SEPARATOR.join(
            f"{WEEKDAY_NAMES[w]}={c}" for w, c in sorted(value.items())
        )
    if attr == "weekday_unavailable_shifts":
        return _SEPARATOR.join(
            f"{WEEKDAY_NAMES[w]}={'/'.join(codes)}"
            for w, codes in sorted(value.items())
            if codes
        )
    if attr == "fixed_time_slots":
        return _SEPARATOR.join(
            f"{WEEKDAY_NAMES[s.weekday]} {s.start}-{s.end}" for s in value
        )
    return value


# --- 読み戻し ------------------------------------------------------------------


def import_review_sheet(
    profiles: Sequence[StaffProfile], path: Path
) -> Tuple[List[StaffProfile], List[str]]:
    """Excelの内容を profiles に反映する。(反映後の一覧, 警告) を返す。

    元の profiles を土台にし、シートにある列だけを上書きするので、
    シートに出していない情報(原文・staff_id など)は失われない。
    """
    workbook = load_workbook(path, data_only=True)
    if SHEET_NAME not in workbook.sheetnames:
        raise ReviewSheetError(
            f"「{SHEET_NAME}」シートが見つかりません。シート名を変えないでください。"
        )
    sheet = workbook[SHEET_NAME]

    headers = [sheet.cell(row=1, column=i).value for i in range(1, len(_COLUMNS) + 1)]
    expected = [header for header, _, _, _, _ in _COLUMNS]
    if headers != expected:
        raise ReviewSheetError(
            "見出し行が想定と違います。列の追加・削除・並べ替えはしないでください。"
        )

    by_id: Dict[str, StaffProfile] = {}
    for profile in profiles:
        if not profile.staff_id:
            continue
        if profile.staff_id in by_id:
            raise ReviewSheetError(
                f"staff_id「{profile.staff_id}」が複数のスタッフで重複しています。"
                "プロフィールのIDを確認してください。"
            )
        by_id[profile.staff_id] = profile

    valid_names = {profile.name for profile in profiles}
    warnings: List[str] = []
    seen = set()

    for row in range(2, sheet.max_row + 1):
        staff_id = _text(sheet.cell(row=row, column=_column_index("staff_id")).value)
        name = _text(sheet.cell(row=row, column=_column_index("name")).value)
        if not staff_id and not name:
            continue
        if not staff_id:
            warnings.append(f"{row}行目「{name}」はID列が空のため飛ばしました")
            continue
        profile = by_id.get(staff_id)
        if profile is None:
            warnings.append(f"{row}行目のID「{staff_id}」はYAMLに無いため飛ばしました")
            continue
        seen.add(staff_id)
        if name and name != profile.name:
            warnings.append(
                f"{row}行目の名前「{name}」がID「{staff_id}」の本来の名前「{profile.name}」"
                "と異なります。名前セルが書き換えられていないか確認してください"
            )
        _apply_row(profile, sheet, row, warnings, valid_names)

    for staff_id, profile in by_id.items():
        if staff_id not in seen:
            warnings.append(f"「{profile.name}」の行がシートにないため、元の内容のままにしました")
    return list(profiles), warnings


def _apply_row(
    profile: StaffProfile, sheet, row: int, warnings: List[str], valid_names: "set[str]"
) -> None:
    def cell(attr: str) -> Any:
        return sheet.cell(row=row, column=_column_index(attr)).value

    def note(column: str, error: str) -> None:
        warnings.append(f"{row}行目「{profile.name}」の{column}: {error}")

    profile.weekly_work_days = _to_int(cell("weekly_work_days"))

    available: List[str] = []
    for code in DAY_SHIFT_CODES:
        was_available = code in profile.available_day_shifts
        if _read_can(_text(cell(f"_shift_{code}")), code, was_available, note):
            available.append(code)
    profile.available_day_shifts = available
    profile.work_hours = _parse_work_hours(_text(cell("work_hours")), note)
    profile.weekday_unavailable_shifts = _parse_weekday_shifts(
        _text(cell("weekday_unavailable_shifts")), note
    )
    profile.unit = _text(cell("unit"))
    profile.works_only_on_request = _read_yes_no(
        _text(cell("works_only_on_request")), "希望した日のみ勤務",
        profile.works_only_on_request, note
    )
    profile.is_support_staff = _read_yes_no(
        _text(cell("is_support_staff")), "介護補助", profile.is_support_staff, note
    )

    profile.can_night = _read_can(_text(cell("can_night")), "夜勤", profile.can_night, note)
    profile.can_late_night = _read_can(
        _text(cell("can_late_night")), "深夜", profile.can_late_night, note
    )

    profile.can_late_night_as_nurse = _read_yes_no(
        _text(cell("can_late_night_as_nurse")),
        "看護職だが深夜可",
        profile.can_late_night_as_nurse,
        note,
    )

    profile.night_shift_count = _range(
        cell("_night_min"), cell("_night_max"), "夜勤の回数", note
    )
    profile.late_night_shift_count = _range(
        cell("_late_night_min"), cell("_late_night_max"), "深夜の回数", note
    )

    for attr, column in (
        ("fixed_off_weekdays", "毎週の固定休み"),
        ("night_weekdays", "夜勤に入る曜日"),
        ("fixed_work_weekdays", "勤務する曜日(固定)"),
    ):
        setattr(profile, attr, _parse_weekdays(_text(cell(attr)), column, note))

    profile.monthly_off_quota = _parse_quota(_text(cell("monthly_off_quota")), note)
    profile.fixed_time_slots = _parse_slots(_text(cell("fixed_time_slots")), note)

    exclusive = _text(cell("_exclusive"))
    if exclusive in ("", "夜勤専従", "深夜のみ", "日勤のみ"):
        profile.night_shift_exclusive = exclusive == "夜勤専従"
        profile.late_night_only = exclusive == "深夜のみ"
        profile.day_shift_only = exclusive == "日勤のみ"
    else:
        note(
            "勤務形態の限定",
            f"「{exclusive}」は「夜勤専従」「深夜のみ」「日勤のみ」のどれでもありません。"
            "値は変更していません",
        )

    profile.day_responsible = _text(cell("day_responsible")) or None
    profile.pair_constraints = _parse_pairs(
        _text(cell("_no_pair")), _text(cell("_max_pair")), note, valid_names
    )

    profile.leave_from = _parse_leave_from(_text(cell("leave_from")), note)
    profile.is_head_nurse = _read_yes_no(
        _text(cell("is_head_nurse")), "師長", profile.is_head_nurse, note
    )
    profile.weekend_off_either = _read_yes_no(
        _text(cell("weekend_off_either")), "土日どちらか休み希望", profile.weekend_off_either, note
    )
    profile.preferred_night = _read_yes_no(
        _text(cell("preferred_night")), "夜勤・深夜を希望", profile.preferred_night, note
    )
    profile.avoid_early = _read_yes_no(
        _text(cell("avoid_early")), "早出は避ける", profile.avoid_early, note
    )
    profile.random_days_allowed = _read_yes_no(
        _text(cell("random_days_allowed")), "曜日おまかせ可", profile.random_days_allowed, note
    )

    if _text(cell("_reviewed")) == _YES:
        profile.review_items = []


# --- 記入内容の解釈 --------------------------------------------------------------


def _split(value: str) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[・,、/\n]+", value) if part.strip()]


def _parse_weekdays(value: str, column: str, note) -> List[int]:
    result: List[int] = []
    for part in _split(value):
        name = part.replace("曜日", "").replace("曜", "")
        if name not in _WEEKDAY_INDEX:
            note(column, f"「{part}」は曜日として読めません(例: 水・土・日)")
            continue
        if _WEEKDAY_INDEX[name] not in result:
            result.append(_WEEKDAY_INDEX[name])
    return sorted(result)


def _parse_quota(value: str, note) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for part in _split(value):
        match = re.match(r"^([月火水木金土日])曜?日?\s*=\s*(\d+)$", part)
        if not match:
            note("月ごとの休み", f"「{part}」が読めません(例: 金=2)")
            continue
        result[_WEEKDAY_INDEX[match.group(1)]] = int(match.group(2))
    return result


def _parse_slots(value: str, note) -> List[FixedTimeSlot]:
    result: List[FixedTimeSlot] = []
    for part in _split(value):
        match = re.match(
            r"^([月火水木金土日])曜?日?\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$", part
        )
        if not match:
            note("曜日ごとの勤務時間", f"「{part}」が読めません(例: 火 09:00-15:00)")
            continue
        result.append(
            FixedTimeSlot(
                weekday=_WEEKDAY_INDEX[match.group(1)],
                start=match.group(2),
                end=match.group(3),
            )
        )
    return result


def _parse_weekday_shifts(value: str, note) -> Dict[int, List[str]]:
    """「火=日①/日②・水=日①/日②」の形。曜日ごとに入れないシフト。"""
    result: Dict[int, List[str]] = {}
    # この列は中で「/」を使うので、曜日どうしの区切り(・改行、)だけで分ける
    parts = [p.strip() for p in re.split(r"[・、,\n]+", value) if p.strip()]
    for part in parts:
        match = re.match(r"^([月火水木金土日])曜?日?\s*=\s*(.+)$", part)
        if not match:
            note("曜日限定で入れないシフト", f"「{part}」が読めません(例: 火=日①/日②)")
            continue
        codes = [c.strip() for c in match.group(2).split("/") if c.strip()]
        unknown = [c for c in codes if c not in DAY_SHIFT_CODES]
        if unknown:
            note("曜日限定で入れないシフト", f"「{'/'.join(unknown)}」はシフトの記号ではありません")
            continue
        result[_WEEKDAY_INDEX[match.group(1)]] = codes
    return result


def _parse_leave_from(value: str, note) -> Optional[str]:
    """「2026-09」の形。空欄なら休職していない。"""
    if not value:
        return None
    match = re.match(r"^(\d{4})[-/年](\d{1,2})月?$", value)
    if not match:
        note("休職開始(年月)", f"「{value}」が読めません(例: 2026-09)")
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"


def _parse_work_hours(value: str, note) -> Optional[Tuple[str, str]]:
    """「09:00-17:00」を読む。空欄なら「番号のシフトで組む人」の意味。"""
    if not value:
        return None
    match = re.match(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$", value)
    if not match:
        note("勤務時間(番号を使わない人)", f"「{value}」が読めません(例: 09:00-17:00)")
        return None
    return (match.group(1), match.group(2))


def _parse_pairs(
    no_pair: str, max_pair: str, note, valid_names: Optional["set[str]"] = None
) -> List[PairConstraint]:
    result: List[PairConstraint] = []
    for name in _split(no_pair):
        if valid_names is not None and name not in valid_names:
            note(
                "夜勤で同席不可",
                f"「{name}」という名前のスタッフが見つかりません。誤字がないか確認してください",
            )
        result.append(PairConstraint(other_staff=name, kind="no_pair_night"))
    for part in _split(max_pair):
        match = re.match(r"^(.+?)\s*=\s*(\d+)$", part)
        if not match:
            note("夜勤で同席上限", f"「{part}」が読めません(例: スタッフA=1)")
            continue
        other_staff = match.group(1).strip()
        if valid_names is not None and other_staff not in valid_names:
            note(
                "夜勤で同席上限",
                f"「{other_staff}」という名前のスタッフが見つかりません。誤字がないか確認してください",
            )
        result.append(
            PairConstraint(
                other_staff=other_staff,
                kind="max_shared_night",
                max_count=int(match.group(2)),
            )
        )
    return result


def _read_can(value: str, column: str, current: bool, note) -> bool:
    if value in ("", "可"):
        return True
    if value == "不可":
        return False
    note(column, f"「{value}」は「可」か「不可」のどちらかにしてください。値は変更していません")
    return current


def _read_yes_no(value: str, column: str, current: bool, note) -> bool:
    if value in ("", _NO):
        return False
    if value == _YES:
        return True
    note(
        column,
        f"「{value}」は「{_YES}」か「{_NO}」のどちらかにしてください。値は変更していません",
    )
    return current


def _range(low: Any, high: Any, column: str, note) -> Optional[Tuple[int, int]]:
    low_value, high_value = _to_int(low), _to_int(high)
    if low_value is None and high_value is None:
        return None
    if low_value is None or high_value is None:
        note(column, "最少・最多のどちらかが空です。両方入れるか両方空にしてください")
        filled = low_value if low_value is not None else high_value
        return (filled, filled)
    if low_value > high_value:
        note(column, f"最少({low_value})が最多({high_value})より大きいので入れ替えました")
        return (high_value, low_value)
    return (low_value, high_value)


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
