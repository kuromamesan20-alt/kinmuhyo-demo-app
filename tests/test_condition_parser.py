"""自由記述の解釈が実データで壊れていないかの回帰テスト。

パーサーを直すときは、まずここに実データの文言でケースを足してから直すこと。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from shift_scheduler.condition_parser import ConditionParser  # noqa: E402
from shift_scheduler.csv_loader import load_staff_rows  # noqa: E402
from shift_scheduler.models import StaffProfile  # noqa: E402
from shift_scheduler.profile_builder import _cross_check, build_profiles  # noqa: E402

CSV_PATH = (
    Path(__file__).parent.parent
    / "data/input/スタッフカード_デモ案件_回答 - スタッフ情報.csv"
)

ROSTER = [
    "スタッフA",
    "スタッフE",
    "スタッフG",
    "スタッフW",
    "スタッフお",
    "スタッフき",
    "スタッフこ",
]


def parse(conditions: str = "", notes: str = "", name: str = "本人") -> StaffProfile:
    profile = StaffProfile(
        staff_id="x", name=name, raw_conditions=conditions, raw_notes=notes
    )
    ConditionParser(ROSTER + [name]).apply(profile)
    return profile


# --- 夜勤/深夜の回数 -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("夜勤4〜5回", (4, 5)),
        ("夜勤は5〜6回", (5, 6)),
        ("夜勤5回〜6回", (5, 6)),
        ("夜勤3回か4回", (3, 4)),
        ("夜勤3回以下。", (0, 3)),
        ("夜勤3回", (3, 3)),
        ("夜勤1回（やむを得ない場合は2回可）", (1, 2)),
    ],
)
def test_night_count(text, expected):
    assert parse(text).night_shift_count == expected


def test_late_night_is_separate_from_night():
    profile = parse("夜勤1〜2回。\n深夜3〜4回。")
    assert profile.night_shift_count == (1, 2)
    assert profile.late_night_shift_count == (3, 4)


def test_exception_on_next_line_extends_max():
    profile = parse("夜勤3回。やむを得ない場合4回")
    assert profile.night_shift_count == (3, 4)


def test_exception_does_not_carry_over_unrelated_sentence():
    """無関係な文(日責可能)が間に挟まったら「やむを得ない場合」は直前文しか参照しない。"""
    profile = parse("夜勤3回。日責可能。やむを得ない場合4回")
    assert profile.night_shift_count == (3, 3)
    assert profile.day_responsible == "可"


# --- 休み --------------------------------------------------------------------


def test_fixed_off_weekdays():
    assert parse("毎週水曜日は休み。").fixed_off_weekdays == [2]
    assert parse("毎週水曜日と、土日休み").fixed_off_weekdays == [2, 5, 6]
    assert parse("毎週土日休み").fixed_off_weekdays == [5, 6]


def test_monthly_quota_is_not_a_weekly_fixed_off():
    profile = parse("毎月2回は金曜日休み。")
    assert profile.monthly_off_quota == {4: 2}
    assert profile.fixed_off_weekdays == []


def test_weekend_off_either_is_soft():
    profile = parse("毎週土曜日か日曜日どちらかは休みたい。")
    assert profile.weekend_off_either is True
    assert profile.fixed_off_weekdays == []


# --- 時間帯 -------------------------------------------------------------------


def test_time_rules_narrow_down_to_shift_codes():
    """時間の条件は、その時間に合う日勤帯コードだけに絞られること。"""
    # 7時前不可 → 6時開始の日①が外れる / 20:30より後は不可 → 21時終わりの日⑦が外れる
    # 日⑨はちょうど20:30終わりなので残る。「日」(9-17)も条件を満たす
    profile = parse("7時より前の早番不可\n20:30以降の遅番不可")
    assert profile.available_day_shifts == ["日②", "日③", "日", "日⑤", "日⑨"]


def test_time_wins_over_the_word_early_or_late():
    """「早番不可」の語だけを見て範囲を広げすぎないこと。"""
    profile = parse("7時より前の早番不可")
    assert "日②" in profile.available_day_shifts, "7時開始の日②は入れるはず"


def test_work_hours_window():
    profile = parse("6時から17時迄の枠で早出、日勤、遅番が可能")
    assert profile.work_hours == ("06:00", "17:00")
    # 「日」(9:00-17:00)も6-17時の枠に収まる
    assert profile.available_day_shifts == ["日①", "日②", "日③", "日"]


def test_part_timer_fixed_slots():
    profile = parse("毎週火曜日は9-15時\n水曜日は9-13時\n金曜日は9-15時")
    assert [(s.weekday, s.start, s.end) for s in profile.fixed_time_slots] == [
        (1, "09:00", "15:00"),
        (2, "09:00", "13:00"),
        (4, "09:00", "15:00"),
    ]


def test_weekday_specific_time_rule_is_flagged():
    """曜日情報を落として取り込むため、必ずレビュー対象になる。"""
    profile = parse("毎週火曜日・水曜日・金曜日は8時より前の早番は不可。")
    assert profile.needs_review


def test_typo_in_time_range_is_flagged():
    """「10-13月」(「時」の誤字)を黙って捨てない。"""
    profile = parse("木曜日10-13月と19-20時")
    assert profile.needs_review


# --- 勤務形態の限定 -------------------------------------------------------------


def test_shift_limits():
    assert parse("夜勤専従").night_shift_exclusive is True
    assert parse("日勤のみ可能。").day_shift_only is True
    assert parse("深夜勤務のみ。").late_night_only is True


def test_shift_ability_from_free_text():
    profile = parse("夜勤不可。深夜可能。")
    assert profile.can_night is False
    assert profile.can_late_night is True


def test_night_restricted_to_weekday():
    assert parse("夜勤は金曜日").night_weekdays == [4]


# --- 同席ペア -------------------------------------------------------------------


def test_pair_no_pair_night():
    profile = parse("夜勤はスタッフWと同席不可。")
    assert [(p.other_staff, p.kind) for p in profile.pair_constraints] == [
        ("スタッフW", "no_pair_night")
    ]
    assert profile.needs_review, "同席制約は絶対厳守なので必ずレビュー対象にする"


def test_pair_second_name_without_prefix():
    """「スタッフEとW」の2人目も拾う。"""
    profile = parse("夜勤スタッフEとWは同席不可。")
    assert {p.other_staff for p in profile.pair_constraints} == {"スタッフE", "スタッフW"}


def test_pair_with_count_limit():
    profile = parse(notes="夜勤はスタッフAとEは一回まで")
    assert {(p.other_staff, p.kind, p.max_count) for p in profile.pair_constraints} == {
        ("スタッフA", "max_shared_night", 1),
        ("スタッフE", "max_shared_night", 1),
    }


def test_pair_exception_on_next_line():
    profile = parse(notes="夜勤はスタッフGと同席不可\n（やむを得ない場合は一回だけ可）")
    assert [(p.kind, p.max_count) for p in profile.pair_constraints] == [
        ("max_shared_night", 1)
    ]


def test_ordinary_japanese_is_not_mistaken_for_a_name():
    """「ないときは」の「と+き」を「スタッフき」と誤認しない。"""
    profile = parse(notes="日責はどうしてもいないときは可能")
    assert profile.pair_constraints == []
    assert profile.day_responsible == "条件付き可"


def test_same_nurse_wording_is_treated_as_spread_partners():
    """「同じナース」の文は相手を特定せず、散らす要望(spread_night_partners)として扱う。"""
    profile = parse(notes="夜勤は同じナースが複数回同席しないようにして下さい。")
    assert profile.pair_constraints == []
    assert profile.spread_night_partners is True
    assert any(item.code == "spread_partners" for item in profile.review_items)
    assert profile.needs_review


def test_unidentifiable_pair_is_flagged_not_guessed():
    """相手を特定できない同席制約は推測せず、確認対象として残す。"""
    profile = parse(notes="夜勤は山田と同席不可")
    assert profile.pair_constraints == []
    assert any(item.code == "pair_unknown" for item in profile.review_items)
    assert profile.needs_review


def test_pair_sentence_with_counts_is_not_silently_dropped():
    """同席文に回数情報が同居していても、黙って握りつぶさず記録を残す。"""
    profile = parse(notes="夜勤はスタッフAと同席不可、夜勤3回まで")
    assert profile.night_shift_count is None
    assert any("夜勤3回まで" in note for note in profile.unparsed_notes) or any(
        "夜勤はスタッフAと同席不可、夜勤3回まで" in note for note in profile.unparsed_notes
    )
    assert profile.needs_review


# --- 未解釈文の保持 --------------------------------------------------------------


def test_unparsed_text_is_kept_and_flagged():
    profile = parse(notes="介護リーダー")
    assert any("介護リーダー" in note for note in profile.unparsed_notes)
    assert profile.needs_review


def test_ambiguous_wording_is_flagged():
    profile = parse("日勤または時短9-16時\n8-15時など")
    assert profile.needs_review


def test_ambiguous_wording_kibou_alone_is_flagged():
    """「希望します」だけでなく「希望」単体も断定できない表現として扱う。"""
    profile = parse("夜勤4〜5回希望")
    assert profile.night_shift_count == (4, 5)
    assert profile.needs_review


# --- profile_builder のクロスチェック ---------------------------------------------


def test_cross_check_flags_late_night_mismatch():
    """深夜不可なのに深夜回数が読み取れている矛盾を検出する。"""
    profile = StaffProfile(
        staff_id="x",
        name="本人",
        can_night=True,
        can_late_night=False,
        late_night_shift_count=(2, 3),
    )
    _cross_check(profile)
    assert profile.needs_review


def test_cross_check_flags_support_staff_without_hours():
    """介護補助なのに固定時間帯も勤務時間も無いと、無警告で全休になるので要確認にする。"""
    profile = StaffProfile(staff_id="x", name="本人", is_support_staff=True)
    _cross_check(profile)
    assert profile.needs_review
    assert any(item.code == "support_no_hours" for item in profile.review_items)


def test_cross_check_does_not_flag_support_staff_with_fixed_time_slots():
    """時間の手がかりがある介護補助は要確認にしない。"""
    from shift_scheduler.models import FixedTimeSlot

    profile = StaffProfile(
        staff_id="x",
        name="本人",
        is_support_staff=True,
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="15:00")],
    )
    _cross_check(profile)
    assert not any(item.code == "support_no_hours" for item in profile.review_items)


# --- 時間条件とCSV早出/遅出列の優先順位(build_profiles) -----------------------------


def _row(name="本人", conditions="", notes="", can_early="", can_late="", can_night="可"):
    return {
        "name": name,
        "conditions": conditions,
        "notes": notes,
        "can_early": can_early,
        "can_late": can_late,
        "can_night": can_night,
        "weekly_work_days": "5",
    }


def test_time_condition_wins_over_csv_early_column():
    """「7時より前の早番不可」があれば、CSVの早出列が「不可」でも日②は残る(時間優先)。"""
    row = _row(conditions="7時より前の早番不可", can_early="不可")
    profile = build_profiles([row])[0]
    assert "日②" in profile.available_day_shifts


def test_csv_early_column_applies_when_no_time_condition():
    """時間の記載が無い人は、CSVの早出列が「不可」なら日①・日②が外れて要確認になる。"""
    row = _row(conditions="", can_early="不可")
    profile = build_profiles([row])[0]
    assert "日①" not in profile.available_day_shifts
    assert "日②" not in profile.available_day_shifts
    assert profile.needs_review


def test_time_condition_wins_over_csv_late_column():
    """「20:30以降の遅番不可」があれば、CSVの遅出列が「不可」でも日⑨は残る(時間優先)。"""
    row = _row(conditions="20:30以降の遅番不可", can_late="不可")
    profile = build_profiles([row])[0]
    assert "日⑨" in profile.available_day_shifts


def test_csv_late_column_applies_when_no_time_condition():
    """時間の記載が無い人は、CSVの遅出列が「不可」なら日⑦・日⑨が外れて要確認になる。"""
    row = _row(conditions="", can_late="不可")
    profile = build_profiles([row])[0]
    assert "日⑦" not in profile.available_day_shifts
    assert "日⑨" not in profile.available_day_shifts
    assert profile.needs_review


def test_weekday_limited_time_rule_does_not_let_csv_column_wipe_other_weekdays():
    """曜日限定の早番制限は time_restricted を立て、他の曜日までCSVの早出列に消されないこと。

    「毎週火・水・金は8時より前の早番は不可」+ CSVの早出列「不可」のケース。
    火・水・金だけ日①・日②が使えず、木・土・日には残っているはず。
    """
    row = _row(conditions="毎週火曜日・水曜日・金曜日は8時より前の早番は不可。", can_early="不可")
    profile = build_profiles([row])[0]

    for weekday in (1, 2, 4):  # 火・水・金
        assert profile.weekday_unavailable_shifts.get(weekday) == ["日①", "日②"]

    # available_day_shifts自体(=木・土・日などの基準)からは消えていない
    assert "日①" in profile.available_day_shifts
    assert "日②" in profile.available_day_shifts
    # 木曜(3)は制限対象外なので日①・日②が入れる
    assert "日①" in profile.shifts_allowed_on(3)
    assert "日②" in profile.shifts_allowed_on(3)
    # 火曜(1)は制限対象なので日①・日②が入れない
    assert "日①" not in profile.shifts_allowed_on(1)
    assert "日②" not in profile.shifts_allowed_on(1)


def test_general_window_condition_wins_over_csv_columns():
    """曜日なしの「9時〜17時の勤務」(work_hours経路)でもCSV列より時間が優先される。"""
    row = _row(conditions="6時から17時迄の枠で早出、日勤、遅番が可能", can_early="不可", can_late="不可")
    profile = build_profiles([row])[0]
    assert profile.work_hours == ("06:00", "17:00")
    # 「日」(9:00-17:00)も6-17時の枠に収まる
    assert profile.available_day_shifts == ["日①", "日②", "日③", "日"]


# --- 実データ全体 ----------------------------------------------------------------


@pytest.mark.skipif(not CSV_PATH.exists(), reason="実データCSVが無い環境ではスキップ")
def test_real_csv_loads_and_keeps_every_condition_line():
    """全31名分を読み込み、条件の各行が構造化か unparsed のどちらかに必ず残ること。"""
    profiles = build_profiles(load_staff_rows(CSV_PATH))
    assert len(profiles) == 31
    assert all(p.name for p in profiles)
    # テスト送信行が除外されていること
    assert not any("テスト" in p.name for p in profiles)


# --- 基本のシフトと「最後の手段」 -------------------------------------------------


def test_base_shift_and_last_resort():
    """「基本は『日』。早出・遅出は他の人を優先し、どうしても難しいときのみ」を読む。

    師長の施設の担当者の条件。入れないのではなく「後回し」という3つ目の状態。
    """
    profile = parse(
        "基本は「日」。早出・遅出は他の人を優先し、どうしても難しいときのみ。夜勤・深夜は不可"
    )
    assert "日" in profile.available_day_shifts, "「日」には普通に入る"
    assert "日" not in profile.last_resort_day_shifts
    assert set(profile.last_resort_day_shifts) == {
        "日①", "日②", "日③", "日⑤", "日⑦", "日⑨"
    }, "「日」以外の日勤帯は最後の手段"
    assert profile.can_night is False
    assert profile.can_late_night is False


def test_last_resort_without_a_base_shift_is_flagged_not_guessed():
    """何を優先するか書かれていなければ、推測せず確認に回す。"""
    profile = parse("早出は他の人を優先してください")
    assert profile.last_resort_day_shifts == []
    assert profile.needs_review


# --- 回数表現の取りこぼし(実データで見つかった漏れ) ------------------------------


def _parse(text: str) -> StaffProfile:
    profile = StaffProfile(staff_id="t", name="テスト", role="介護士", raw_conditions=text)
    ConditionParser([]).apply(profile)
    return profile


def test_毎月をはさんだ回数レンジが読める():
    """「夜勤は毎月2〜3回」— 区分と回数の間に「毎月」が入る書き方。"""
    assert _parse("夜勤は毎月2〜3回希望").night_shift_count == (2, 3)


def test_月にをはさんだ上限が読める():
    assert _parse("深夜は月に3回まで").late_night_shift_count == (0, 3)


def test_専従の文に同居した回数を落とさない():
    """「夜勤専従で毎月2〜3回」で、専従だけ拾って回数を捨てないこと。"""
    profile = _parse("夜勤専従で毎月2〜3回希望してくるのもを検討して確定。")
    assert profile.night_shift_exclusive is True
    assert profile.night_shift_count == (2, 3)


def test_深夜のみの人に夜勤回数があれば確認に回す():
    profile = _parse("深夜勤務のみ。夜勤専従で毎月2〜3回希望。")
    assert any(item.code == "count_conflict" for item in profile.review_items)


# --- 「夜勤」は夜間業務全体を指す(施設の担当者に確認済み) --------------------------


def test_夜勤不可は深夜にも入らない():
    """施設の担当者の言う「夜勤」は ○ と ◉ の両方。片方だけ止めると◉に入ってしまう。"""
    profile = _parse("夜勤不可。")
    assert profile.can_night is False
    assert profile.can_late_night is False


def test_深夜と名指しされていればそちらが優先():
    """「夜勤不可。深夜可能。」という書き方。実物でも◉だけ入っていた。"""
    profile = _parse("夜勤不可。深夜可能。")
    assert profile.can_night is False
    assert profile.can_late_night is True


def test_深夜の可否は後から来た夜勤で上書きされない():
    """順番が逆でも結果が変わらないこと。"""
    profile = _parse("深夜可能。夜勤不可。")
    assert profile.can_night is False
    assert profile.can_late_night is True


def test_深夜のみの人の夜勤回数は深夜の回数として読む():
    profile = _parse("深夜勤務のみ。夜勤専従で毎月2〜3回希望。")
    assert profile.late_night_shift_count == (2, 3)
    assert profile.night_shift_count is None
    assert any(item.code == "count_conflict" for item in profile.review_items)


def test_深夜のみの人に深夜と夜勤の回数が両方書かれていたら既存の深夜回数を保つ():
    """「深夜勤務のみ。深夜2回。夜勤専従で毎月3〜4回希望。」のように、
    深夜の回数が既に書かれているのに別途夜勤の回数も書かれている場合。
    どちらが本来の希望か分からないので、既存の深夜回数を黙って上書きせず、
    両方の回数が書かれている旨のメッセージで確認に回す。
    """
    profile = _parse("深夜勤務のみ。深夜2回。夜勤専従で毎月3〜4回希望。")
    assert profile.late_night_shift_count == (2, 2)
    assert profile.night_shift_count is None
    conflict_items = [item for item in profile.review_items if item.code == "count_conflict"]
    assert conflict_items
    assert any(
        "2〜2回" in item.message and "3〜4回" in item.message and "両方" in item.message
        for item in conflict_items
    )
    # 実際には上書きしていないのに「〜という意味に取りました」と言ってしまうと、
    # 反映値と食い違う情報を人に見せてしまうので、そう言っていないことを確認する。
    assert not any("という意味に取りました" in item.message for item in conflict_items)


# --- 条件文の仮名を苗字に解決する(実データで同席制約が全部落ちていた) ----------------


def test_条件文の仮名が苗字に解決される():
    """条件文は「スタッフW」のような仮名のまま。名簿は苗字なので対応表で解く。

    以前は照合できず、同席制約が6件すべて効いていなかった。
    """
    parser = ConditionParser(["介護A", "看護A"], {"スタッフは": "介護A", "スタッフW": "看護A"})
    profile = StaffProfile(
        staff_id="t", name="介護A", role="介護士", raw_conditions="夜勤はスタッフWと同席不可。"
    )
    parser.apply(profile)

    assert [(c.other_staff, c.kind) for c in profile.pair_constraints] == [
        ("看護A", "no_pair_night")
    ]


def test_接頭辞なしの2人目も苗字に解決される():
    """「スタッフEとW」の「W」も相手として拾う。"""
    parser = ConditionParser(
        ["介護B", "看護B", "看護A"],
        {"スタッフさ": "介護B", "スタッフE": "看護B", "スタッフW": "看護A"},
    )
    profile = StaffProfile(
        staff_id="t", name="介護B", role="介護士", raw_conditions="夜勤スタッフEとWは同席不可。"
    )
    parser.apply(profile)

    assert sorted(c.other_staff for c in profile.pair_constraints) == ["看護A", "看護B"]


def test_自分自身は同席相手にしない():
    parser = ConditionParser(["介護A", "看護A"], {"スタッフは": "介護A", "スタッフW": "看護A"})
    profile = StaffProfile(
        staff_id="t",
        name="介護A",
        role="介護士",
        raw_conditions="夜勤はスタッフはとスタッフWは同席不可。",
    )
    parser.apply(profile)

    assert [c.other_staff for c in profile.pair_constraints] == ["看護A"]


def test_仮名が複数の人に付いていたら確認に回す():
    """同じ仮名が2人に付いていると、同席の相手を取り違える。黙って後勝ちにしない。"""
    from shift_scheduler.profile_builder import build_profiles

    rows = [
        {"name": "介護A", "alias": "スタッフW", "conditions": "", "notes": "",
         "dayshift_responsible": "", "can_night": "可", "early": "", "late": ""},
        {"name": "看護A", "alias": "スタッフW", "conditions": "", "notes": "",
         "dayshift_responsible": "", "can_night": "可", "early": "", "late": ""},
    ]
    profiles = build_profiles(rows)

    assert all(
        any(item.code == "alias_collision" for item in p.review_items) for p in profiles
    )
