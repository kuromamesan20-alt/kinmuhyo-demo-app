"""Excel確認シートの書き出し・読み戻しのテスト。

一番大事なのは「往復しても情報が失われない」こと。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from shift_scheduler.csv_loader import load_staff_rows  # noqa: E402
from shift_scheduler.excel_review import (  # noqa: E402
    SHEET_NAME,
    ReviewSheetError,
    export_review_sheet,
    import_review_sheet,
)
from shift_scheduler.models import (  # noqa: E402
    FixedTimeSlot,
    PairConstraint,
    ReviewItem,
    StaffProfile,
)
from shift_scheduler.profile_builder import build_profiles  # noqa: E402
from shift_scheduler.shifts import DAY_SHIFT_CODES  # noqa: E402

CSV_PATH = (
    Path(__file__).parent.parent
    / "data/input/スタッフカード_デモ案件_回答 - スタッフ情報.csv"
)


def sample_profile() -> StaffProfile:
    return StaffProfile(
        staff_id="id-1",
        name="スタッフさ",
        role="介護士",
        employment_type="正社員",
        weekly_work_days=5,
        can_night=True,
        can_late_night=True,
        available_day_shifts=["日③", "日⑤", "日⑨"],
        night_shift_count=(2, 3),
        late_night_shift_count=(3, 3),
        fixed_off_weekdays=[2, 5],
        monthly_off_quota={4: 2},
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="15:00")],
        work_hours=("09:00", "17:00"),
        night_weekdays=[4],
        fixed_work_weekdays=[1],
        night_shift_exclusive=True,
        day_responsible="条件付き可",
        pair_constraints=[
            PairConstraint(other_staff="スタッフE", kind="no_pair_night"),
            PairConstraint(other_staff="スタッフA", kind="max_shared_night", max_count=1),
        ],
        weekend_off_either=True,
        preferred_night=True,
        raw_conditions="毎月2回は金曜日休み。",
        raw_notes="日責可能",
        unparsed_notes=["[備考] 介護リーダー"],
        review_items=[ReviewItem(message="同席制約を確認してください", code="pair")],
    )


def with_companions(profile: StaffProfile):
    """同席相手(スタッフE・スタッフA)も名簿に含めた一覧を返す。

    同席相手の氏名は名簿と照合されるため、本人だけでは警告が出てしまう。
    """
    return [
        profile,
        StaffProfile(staff_id="id-2", name="スタッフE"),
        StaffProfile(staff_id="id-3", name="スタッフA"),
    ]


def shift_column(headers, code: str) -> int:
    """日勤帯コードの列番号(1始まり)を返す。

    見出しは「日③\n08:00-16:00」の形。startswith だと「日」が「日①」に
    当たってしまうため、1行目を取り出して完全一致で探す。
    """
    for index, header in enumerate(headers, 1):
        if header and str(header).split("\n")[0] == code:
            return index
    raise AssertionError(f"{code} の列が見つかりません")


def test_round_trip_keeps_every_field(tmp_path):
    """書き出し→読み戻しで、シートに出した項目が変わらないこと。"""
    original = sample_profile()
    # 同席制約(スタッフE・スタッフA)の名寄せ検証に通るよう、名簿にも加えておく
    companion_e = StaffProfile(staff_id="id-2", name="スタッフE")
    companion_a = StaffProfile(staff_id="id-3", name="スタッフA")
    path = tmp_path / "review.xlsx"
    export_review_sheet([original, companion_e, companion_a], path)

    reloaded = sample_profile()
    reloaded_e = StaffProfile(staff_id="id-2", name="スタッフE")
    reloaded_a = StaffProfile(staff_id="id-3", name="スタッフA")
    updated, warnings = import_review_sheet([reloaded, reloaded_e, reloaded_a], path)
    result = updated[0]

    assert warnings == []
    for attr in (
        "weekly_work_days",
        "can_night",
        "can_late_night",
        "available_day_shifts",
        "work_hours",
        "night_shift_count",
        "late_night_shift_count",
        "fixed_off_weekdays",
        "monthly_off_quota",
        "night_weekdays",
        "fixed_work_weekdays",
        "night_shift_exclusive",
        "late_night_only",
        "day_shift_only",
        "day_responsible",
        "weekend_off_either",
        "preferred_night",
        "avoid_early",
        "random_days_allowed",
    ):
        assert getattr(result, attr) == getattr(original, attr), attr

    assert [(s.weekday, s.start, s.end) for s in result.fixed_time_slots] == [
        (1, "09:00", "15:00")
    ]
    assert {(p.other_staff, p.kind, p.max_count) for p in result.pair_constraints} == {
        ("スタッフE", "no_pair_night", None),
        ("スタッフA", "max_shared_night", 1),
    }


def test_round_trip_keeps_fields_not_on_the_sheet(tmp_path):
    """原文やstaff_idなど、シートに出していない情報が消えないこと。"""
    path = tmp_path / "review.xlsx"
    export_review_sheet([sample_profile()], path)

    updated, _ = import_review_sheet([sample_profile()], path)
    result = updated[0]
    assert result.staff_id == "id-1"
    assert result.raw_conditions == "毎月2回は金曜日休み。"
    assert result.raw_notes == "日責可能"
    assert result.unparsed_notes == ["[備考] 介護リーダー"]


def test_reviewed_yes_clears_review_reasons(tmp_path):
    """「確認済み」を「はい」にすると要確認が外れること。"""
    profile = sample_profile()
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    reviewed_column = [c.value for c in sheet[1]].index("確認済み") + 1
    sheet.cell(row=2, column=reviewed_column, value="はい")
    workbook.save(path)

    updated, _ = import_review_sheet([sample_profile()], path)
    assert updated[0].needs_review is False


def test_edit_is_reflected(tmp_path):
    profile = sample_profile()
    # 同席制約(スタッフA・スタッフW)の名寄せ検証に通るよう、名簿にも加えておく
    companion_a = StaffProfile(staff_id="id-3", name="スタッフA")
    companion_w = StaffProfile(staff_id="id-4", name="スタッフW")
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile, companion_a, companion_w], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    sheet.cell(row=2, column=headers.index("夜勤 最多回数") + 1, value=5)
    sheet.cell(row=2, column=headers.index("毎週の固定休み") + 1, value="月・火")
    sheet.cell(row=2, column=headers.index("夜勤で同席不可") + 1, value="スタッフW")
    workbook.save(path)

    updated, warnings = import_review_sheet(
        [
            sample_profile(),
            StaffProfile(staff_id="id-3", name="スタッフA"),
            StaffProfile(staff_id="id-4", name="スタッフW"),
        ],
        path,
    )
    result = updated[0]
    assert warnings == []
    assert result.night_shift_count == (2, 5)
    assert result.fixed_off_weekdays == [0, 1]
    assert [p.other_staff for p in result.pair_constraints if p.kind == "no_pair_night"] == [
        "スタッフW"
    ]


def test_weekday_unavailable_shifts_column_round_trips(tmp_path):
    """「曜日限定で入れないシフト」列(火=日①/日②・水=日①/日②)が往復すること。"""
    profile = StaffProfile(
        staff_id="id-9",
        name="スタッフ曜",
        weekday_unavailable_shifts={1: ["日①", "日②"], 2: ["日①", "日②"]},
    )
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    value = sheet.cell(
        row=2, column=headers.index("曜日限定で入れないシフト") + 1
    ).value
    assert value == "火=日①/日②・水=日①/日②"

    updated, warnings = import_review_sheet(
        [StaffProfile(staff_id="id-9", name="スタッフ曜")], path
    )
    assert warnings == []
    assert updated[0].weekday_unavailable_shifts == {
        1: ["日①", "日②"],
        2: ["日①", "日②"],
    }


def test_day_shift_columns_round_trip(tmp_path):
    """日①〜日⑨の可/不可がそのまま往復すること。"""
    path = tmp_path / "review.xlsx"
    export_review_sheet(with_companions(sample_profile()), path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    marks = {
        code: sheet.cell(row=2, column=shift_column(headers, code)).value
        for code in DAY_SHIFT_CODES
    }
    assert marks == {
        "日①": "不可",
        "日②": "不可",
        "日③": "可",
        "日": "不可",
        "日⑤": "可",
        "日⑦": "不可",
        "日⑨": "可",
    }

    updated, warnings = import_review_sheet(with_companions(sample_profile()), path)
    assert warnings == []
    assert updated[0].available_day_shifts == ["日③", "日⑤", "日⑨"]


def test_day_shift_edit_is_reflected(tmp_path):
    path = tmp_path / "review.xlsx"
    export_review_sheet(with_companions(sample_profile()), path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    column = shift_column(headers, "日①")
    sheet.cell(row=2, column=column, value="可")
    workbook.save(path)

    updated, warnings = import_review_sheet(with_companions(sample_profile()), path)
    assert warnings == []
    assert updated[0].available_day_shifts == ["日①", "日③", "日⑤", "日⑨"]


def test_unreadable_entry_is_warned_not_guessed(tmp_path):
    """読めない記入は推測せず、警告して元の値を変えないこと。"""
    path = tmp_path / "review.xlsx"
    export_review_sheet([sample_profile()], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    sheet.cell(row=2, column=headers.index("毎週の固定休み") + 1, value="げつ・か")
    sheet.cell(row=2, column=headers.index("月ごとの休み") + 1, value="金2回")
    workbook.save(path)

    updated, warnings = import_review_sheet([sample_profile()], path)
    assert len(warnings) >= 2
    assert any("毎週の固定休み" in w for w in warnings)
    assert any("月ごとの休み" in w for w in warnings)
    assert updated[0].monthly_off_quota == {}


def test_swapped_min_max_is_corrected_with_warning(tmp_path):
    path = tmp_path / "review.xlsx"
    export_review_sheet([sample_profile()], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    sheet.cell(row=2, column=headers.index("夜勤 最少回数") + 1, value=6)
    sheet.cell(row=2, column=headers.index("夜勤 最多回数") + 1, value=2)
    workbook.save(path)

    updated, warnings = import_review_sheet([sample_profile()], path)
    assert updated[0].night_shift_count == (2, 6)
    assert any("入れ替え" in w for w in warnings)


def test_matches_by_staff_id_even_if_name_cell_is_rewritten(tmp_path):
    """名前セルを書き換えても、staff_idで正しい本人に反映されること。"""
    profile = sample_profile()
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    sheet.cell(row=2, column=headers.index("名前") + 1, value="別の名前")
    sheet.cell(row=2, column=headers.index("週の勤務日数") + 1, value=3)
    workbook.save(path)

    reloaded = sample_profile()
    updated, warnings = import_review_sheet([reloaded], path)
    result = updated[0]

    assert result.staff_id == "id-1"
    assert result.weekly_work_days == 3
    assert any("名前" in w and "id-1" in w for w in warnings)


def test_duplicate_staff_id_raises_error(tmp_path):
    """名簿内でstaff_idが重複していたらエラーにすること。"""
    profile_a = sample_profile()
    profile_b = sample_profile()
    profile_b.name = "スタッフべ"  # staff_idは"id-1"のまま重複させる
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile_a, profile_b], path)

    with pytest.raises(ReviewSheetError):
        import_review_sheet([profile_a, profile_b], path)


def test_pair_constraint_with_unknown_name_is_warned(tmp_path):
    """同席制約の相手名が名簿にいない(誤字など)場合は警告すること。"""
    profile = sample_profile()
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    sheet.cell(row=2, column=headers.index("夜勤で同席不可") + 1, value="スタフE")
    workbook.save(path)

    updated, warnings = import_review_sheet([sample_profile()], path)

    assert any("スタフE" in w for w in warnings)
    # 黙って消さず、値自体は残す
    assert any(p.other_staff == "スタフE" for p in updated[0].pair_constraints)


def test_unexpected_choice_values_are_warned_and_kept(tmp_path):
    """選択肢列に想定外の文字列が来たら警告し、元の値を変えないこと。"""
    profile = sample_profile()
    path = tmp_path / "review.xlsx"
    export_review_sheet([profile], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    headers = [c.value for c in sheet[1]]
    sheet.cell(row=2, column=headers.index("○ 夜勤") + 1, value="ふか")
    sheet.cell(row=2, column=headers.index("勤務形態の限定") + 1, value="よふかし")
    sheet.cell(row=2, column=headers.index("土日どちらか休み希望") + 1, value="たぶん")
    workbook.save(path)

    updated, warnings = import_review_sheet([sample_profile()], path)
    result = updated[0]

    assert result.can_night == profile.can_night
    assert result.night_shift_exclusive == profile.night_shift_exclusive
    assert result.weekend_off_either == profile.weekend_off_either
    assert any("夜勤" in w and "ふか" in w for w in warnings)
    assert any("勤務形態の限定" in w for w in warnings)
    assert any("土日どちらか休み希望" in w for w in warnings)


def test_broken_header_is_rejected(tmp_path):
    """列を並べ替えられたら、黙って誤読せずエラーにすること。"""
    path = tmp_path / "review.xlsx"
    export_review_sheet([sample_profile()], path)

    workbook = load_workbook(path)
    sheet = workbook[SHEET_NAME]
    sheet.cell(row=1, column=2, value="勝手に変えた見出し")
    workbook.save(path)

    with pytest.raises(ReviewSheetError):
        import_review_sheet([sample_profile()], path)


@pytest.mark.skipif(not CSV_PATH.exists(), reason="実データCSVが無い環境ではスキップ")
def test_real_data_round_trip(tmp_path):
    """実データ31名でも往復で内容が変わらないこと。"""
    path = tmp_path / "review.xlsx"
    export_review_sheet(build_profiles(load_staff_rows(CSV_PATH)), path)

    updated, warnings = import_review_sheet(build_profiles(load_staff_rows(CSV_PATH)), path)
    assert warnings == []

    for before, after in zip(build_profiles(load_staff_rows(CSV_PATH)), updated):
        assert before.name == after.name
        assert before.night_shift_count == after.night_shift_count
        assert before.fixed_off_weekdays == after.fixed_off_weekdays
        assert before.monthly_off_quota == after.monthly_off_quota
        assert len(before.pair_constraints) == len(after.pair_constraints)
        assert before.raw_conditions == after.raw_conditions
