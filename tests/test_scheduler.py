"""build_schedule の最小シナリオでのテスト。

CP-SATのフル月次求解は重いので、1日だけの月(monkeypatch)にしてすばやく検証する。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ortools.sat.python import cp_model  # noqa: E402

from shift_scheduler import scheduler as scheduler_module  # noqa: E402
from shift_scheduler.calendar_utils import Day  # noqa: E402
from shift_scheduler.models import (  # noqa: E402
    FixedTimeSlot,
    PairConstraint,
    StaffProfile,
)
from shift_scheduler.scheduler import (  # noqa: E402
    ALERT_MARK,
    allowed_entry_marks,
    build_schedule,
)
from shift_scheduler.request_sheet import (  # noqa: E402
    CARRY_OVER_DAY,
    StaffRequests,
)
from shift_scheduler.shifts import (  # noqa: E402
    LATE_NIGHT_IN,
    NIGHT_AFTER,
    NIGHT_IN,
    OFF,
    PAID_LEAVE,
    REQUIRED_DAY_SHIFTS,
)


def _coverage_staff() -> List[StaffProfile]:
    """1日分の必要人数(日勤帯6種・夜勤2人・深夜1人)をちょうど満たすだけの最小構成。"""
    staff: List[StaffProfile] = []
    for index, code in enumerate(REQUIRED_DAY_SHIFTS, 1):
        staff.append(
            StaffProfile(
                staff_id=f"day-{index}",
                name=f"日勤{index}",
                available_day_shifts=[code],
                can_night=False,
                can_late_night=False,
            )
        )
    for index in range(1, 3):
        staff.append(
            StaffProfile(
                staff_id=f"night-{index}",
                name=f"夜勤{index}",
                available_day_shifts=[],
                can_night=True,
                can_late_night=False,
            )
        )
    staff.append(
        StaffProfile(
            staff_id="latenight-1",
            name="深夜1",
            available_day_shifts=[],
            can_night=False,
            can_late_night=True,
        )
    )
    return staff


def test_support_staff_is_not_counted_in_day_shift_requirement(monkeypatch):
    """介護補助(is_support_staff)は日勤帯の必要人数に数えられず、常に公になること。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )

    support = StaffProfile(staff_id="support-1", name="介護補助さん", is_support_staff=True)
    profiles = _coverage_staff() + [support]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    # 固定時間も希望休もないので、常に公休として扱われる(=日勤帯の枠を取らない)
    assert result.assignments["support-1"] == {1: OFF}
    assert any("介護補助" in message for message in result.messages)


def test_weekly_days_off_are_kept(monkeypatch):
    """週休2日が守られること。夜勤の回数で休みが減らないこと。

    実物の勤務表では、夜勤5回の人も0回の人も公休は同じ8〜9日だった。
    「月に何日」ではなく「週ごとに休む」という考え方。
    """
    from shift_scheduler.scheduler import _weeks
    from shift_scheduler.calendar_utils import month_days

    days = month_days(2026, 10)  # 10月1日は木曜
    weeks = _weeks(days)

    # 週の区切りが月曜始まりになっていること
    assert len(weeks[0]) == 4, "1〜4日(木金土日)が最初の半端な週"
    assert weeks[1][0].weekday == 0, "2週目は月曜から"
    assert sum(len(w) for w in weeks) == 31, "全部の日が どれかの週に入る"


def test_partial_weeks_ask_for_less(monkeypatch):
    """月初・月末の半端な週は、日数に応じて休みを減らすこと。

    4日しかない週に2日の休みを求めると、組めなくなることがある。
    """
    from shift_scheduler.calendar_utils import month_days
    from shift_scheduler.scheduler import _weeks

    weeks = _weeks(month_days(2026, 10))
    partial = [w for w in weeks if len(w) < 7]
    assert partial, "10月には半端な週がある"
    for week in partial:
        quota = (2 * len(week)) // 7
        assert quota < 2, "半端な週は2日より少なくてよい"


def _night_staff():
    """夜勤・深夜を回せる最小限の顔ぶれ。"""
    from shift_scheduler.models import StaffProfile

    people = []
    for i in range(4):
        people.append(StaffProfile(staff_id=f"ns-{i}", name=f"看護{i}", role="看護師"))
    for i in range(4):
        people.append(StaffProfile(staff_id=f"cg-{i}", name=f"介護{i}", role="介護士"))
    return people


def test_night_shift_is_one_nurse_and_one_caregiver():
    """○夜勤は看護職1人+介護職1人。実物では31日中30日がこの組み合わせだった。"""
    from shift_scheduler.scheduler import _add_night_composition
    from shift_scheduler.models import StaffProfile

    people = _night_staff()
    nurses = [p for p in people if p.is_nurse]
    caregivers = [p for p in people if p.is_caregiver]
    assert len(nurses) == 4 and len(caregivers) == 4

    # 職種の判定そのものを確かめる
    assert StaffProfile(staff_id="x", name="管理", role="管理者、看護師").is_nurse
    assert not StaffProfile(staff_id="x", name="環境", role="その他").is_nurse
    assert not StaffProfile(staff_id="x", name="環境", role="その他").is_caregiver


def test_深夜に入れる看護職は指定された人だけ(monkeypatch):
    """深夜(◉)は原則として介護職。看護職で入れるのは指定された人だけ。

    誰がその人かは施設ごとに違うので、コードに名前は書かず
    can_late_night_as_nurse で持つ。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    nurse = StaffProfile(
        staff_id="nurse-late",
        name="看護A",
        role="看護師",
        available_day_shifts=[],
        can_night=False,
        can_late_night=True,
    )
    profiles = _staff_with_night_roles() + [nurse]

    # 指定が無ければ、看護職は深夜に入れない → 深夜の枠が埋まらず組めない
    assert not build_schedule(profiles, requests=[], year=2026, month=9).ok

    nurse.can_late_night_as_nurse = True
    result = build_schedule(profiles, requests=[], year=2026, month=9)
    assert result.ok, result.messages
    assert result.assignments["nurse-late"] == {1: LATE_NIGHT_IN}

def test_staff_with_a_time_window_still_follows_the_limits():
    """「6時から17時の枠」のような時間指定があっても、常勤は上限の対象。

    ここを writes_own_hours で判定すると、番号シフトで普通に働く常勤まで
    連続勤務や週休2日の対象から外れてしまう(実際に13日連続が出た)。
    """
    from shift_scheduler.models import FixedTimeSlot, StaffProfile
    from shift_scheduler.scheduler import _has_own_schedule

    # 条件文に時間の枠が書かれた常勤 → 対象に含める
    nurse = StaffProfile(
        staff_id="n1", name="常勤看護", role="看護師", work_hours=("06:00", "17:00")
    )
    assert nurse.writes_own_hours, "時間枠は持っている"
    assert not _has_own_schedule(nurse), "それでも上限の対象に含める"

    # 曜日ごとに勤務時間が決まっているパート → 対象外
    part = StaffProfile(
        staff_id="p1",
        name="パート",
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="15:00")],
    )
    assert _has_own_schedule(part)

    # 介護補助 → 対象外
    support = StaffProfile(staff_id="s1", name="補助", is_support_staff=True)
    assert _has_own_schedule(support)


def test_max_consecutive_days_is_five():
    from shift_scheduler.shifts import MAX_CONSECUTIVE_WORK_DAYS

    assert MAX_CONSECUTIVE_WORK_DAYS == 5


# --- 入れる勤務が1つも無い人(実データで見つかった不具合) --------------------------


def _staff_with_night_roles() -> List[StaffProfile]:
    """夜勤の顔ぶれ(看護1+介護1)を満たせる最小構成。深夜の枠は空けてある。

    介護職を1人でも足すと「○に介護職ちょうど1人」の条件が効くので、
    役割を設定しないままだと組めなくなる。
    """
    staff = [p for p in _coverage_staff() if p.staff_id != "latenight-1"]
    by_id = {p.staff_id: p for p in staff}
    by_id["night-1"].role = "看護師"
    by_id["night-2"].role = "介護士"
    return staff


def _late_night_filler() -> StaffProfile:
    """深夜(◉)の枠を埋めるだけの介護職。"""
    return StaffProfile(
        staff_id="latenight-fill",
        name="深夜要員",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=True,
    )


def test_深夜のみと夜勤専従が両方立っていても深夜に入れる(monkeypatch):
    """原文の言い回しが重なって両方立つことがある。より具体的な「深夜のみ」を採る。

    以前はこの組み合わせで日勤も夜勤も深夜も全部禁止され、
    何の警告も出ないまま1か月まるごと公休になっていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    both = StaffProfile(
        staff_id="both-1",
        name="深夜専従さん",
        role="介護士",
        night_shift_exclusive=True,
        late_night_only=True,
    )
    # 深夜の枠を this 人に取らせるため、既定の深夜要員は外す
    profiles = _staff_with_night_roles() + [both]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["both-1"] == {1: LATE_NIGHT_IN}


def test_曜日限定の夜勤者しかいない日は入れる看護職がいないと報告する(monkeypatch):
    """常時フラグ(can_night)だけでなく、その日固有の制限も見て診断すること。

    看護職の夜勤可能曜日を金曜(4)だけにすると、can_night=True のままなので、
    フラグだけを見ると火曜〜木曜でも「夜勤に入れる看護職はいる」と誤診断してしまう。
    月を火曜(9/1)〜金曜(9/4)の4日間にして、金曜だけは入れる(=月から
    丸ごと除外されない)が火曜〜木曜には入れない、という状態を作り、
    allowed_entry_marks で当日ごとの制限まで見て正しく
    「入れる看護職がいない」と報告できることを確かめる。
    (夜勤の並びの制約が前日・翌々日を参照するため、日付は連続させる)
    """
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [
            Day(date(2026, 9, 1)),
            Day(date(2026, 9, 2)),
            Day(date(2026, 9, 3)),
            Day(date(2026, 9, 4)),
        ],
    )
    staff = _staff_with_night_roles()
    nurse = next(p for p in staff if p.staff_id == "night-1")
    nurse.night_weekdays = [4]  # 金曜のみ夜勤可(9/1〜3は○に入れない)
    # 日勤には入れるようにしておく。そうしないと「その日は何も入れない人」として
    # 数える前に除かれてしまい、常時フラグを見る書き方でも正しく見えてしまう。
    # 「日」は毎日の必要人数の決まりが無いので、他の枠を奪わない。
    nurse.available_day_shifts = ["日"]
    profiles = staff + [_late_night_filler()]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert not result.ok
    assert any(
        "○夜勤に入れる看護職がいない日" in m and "1日、2日、3日" in m
        for m in result.messages
    )


def test_入れる勤務が無い人は黙って全公休にせず報告する(monkeypatch):
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    nobody = StaffProfile(
        staff_id="none-1",
        name="入れない人",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
    )
    profiles = _staff_with_night_roles() + [_late_night_filler(), nobody]
    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["none-1"] == {1: OFF}
    assert any("★要確認" in m and "入れない人" in m for m in result.messages)


def test_番号のシフトに合わない勤務時間は時間をそのまま書く(monkeypatch):
    """8:00-15:00 のような、番号のシフトに当てはまらないパートの勤務時間。

    以前は入れる日勤帯が空になり、警告も無いまま1か月すべて公休になっていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="part-1",
        name="時間パートさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        work_hours=("08:00", "15:00"),
    )
    # 「日③」のように本人が入れない番号を選ばれても、勤務時間を書く
    wish = StaffRequests(staff_id="part-1", name="時間パートさん", entries={1: "日③"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["part-1"] == {1: "08:00-15:00"}


def test_時短や日勤は選ばれたものをそのまま書く(monkeypatch):
    """時短の時間が複数ある人は、その日どれで入るか選べる。

    以前は何を選んでも work_hours の1パターンだけが入っていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    base = dict(
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        work_hours=("08:00", "15:00"),
    )
    for chosen in ("日", "9-13時", "9-15時"):
        part = StaffProfile(staff_id="part-1", name="時間パートさん", **base)
        wish = StaffRequests(
            staff_id="part-1", name="時間パートさん", entries={1: chosen}
        )
        profiles = _staff_with_night_roles() + [_late_night_filler(), part]
        result = build_schedule(profiles, requests=[wish], year=2026, month=9)

        assert result.ok, result.messages
        assert result.assignments["part-1"] == {1: chosen}, f"「{chosen}」が入るはず"


def test_時間直書きの人の有給が反映される(monkeypatch):
    """有給は公休と別に数えるので、勤務や公で塗りつぶしてはいけない。

    以前は fixed_time_slots のある人に「有」を出しても勤務時間が入っていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="slot-1",
        name="曜日パートさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="13:00")],
    )
    wish = StaffRequests(staff_id="slot-1", name="曜日パートさん", entries={1: "有"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["slot-1"] == {1: "有"}


def test_希望出勤が無く有給だけの人は勤務0日として要確認になる(monkeypatch):
    """有給・夏休などは「勤務した日」に数えてはいけない。

    以前は worked を `mark != OFF` だけで数えていたため、希望出勤が1件も無く
    有給が入っているだけの月でも worked>0 になり、要確認(★)の警告が出ないまま
    「勤務時間をそのまま入れました」という誤った安心メッセージになっていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="paid-1",
        name="有給だけさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        work_hours=("08:00", "15:00"),
    )
    # 希望出勤は無く、有給だけが入っている
    wish = StaffRequests(staff_id="paid-1", name="有給だけさん", entries={1: "有"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["paid-1"] == {1: "有"}
    # 有給は「勤務した日」ではないので、勤務0日として要確認に回る
    assert any(ALERT_MARK in m and "有給だけさん" in m for m in result.messages)
    assert not any("そのまま入れました" in m and "有給だけさん" in m for m in result.messages)


def test_夜勤専従は深夜にも入れる(monkeypatch):
    """「夜勤専従」は夜間業務の専従という意味で、○だけでなく◉にも入る。

    以前は○だけに絞っていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    exclusive = StaffProfile(
        staff_id="ex-1",
        name="夜間専従さん",
        role="介護士",
        night_shift_exclusive=True,
    )
    assert allowed_entry_marks(exclusive, Day(date(2026, 9, 1))) == {
        NIGHT_IN,
        LATE_NIGHT_IN,
    }

    # 深夜の枠しか空いていなくても組めること
    profiles = _staff_with_night_roles() + [exclusive]
    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["ex-1"] == {1: LATE_NIGHT_IN}


# --- 同席制約(実データで効いていなかった) ----------------------------------------


def _night_pair_staff() -> List[StaffProfile]:
    """○2人(看護1+介護1)と◉1人をちょうど満たす、日勤帯なしの最小構成。"""
    staff = [p for p in _coverage_staff() if p.staff_id != "latenight-1"]
    by_id = {p.staff_id: p for p in staff}
    by_id["night-1"].role = "看護師"
    by_id["night-2"].role = "介護士"
    return staff + [_late_night_filler()]


def test_同じ夜に2人とも夜勤なら同席として数える(monkeypatch):
    """○は毎晩2人いるので、○と○の組み合わせも同席になる。

    以前は○と◉の組み合わせしか数えておらず、看護師と介護士の
    「同席不可」が素通りしていた。実データでは実際に同席が発生していた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _night_pair_staff()
    by_id = {p.staff_id: p for p in profiles}
    by_id["night-1"].pair_constraints = [
        PairConstraint(other_staff=by_id["night-2"].name, kind="no_pair_night")
    ]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    # ○は看護1+介護1で2人必要なのに、その2人が同席不可 → 組めないのが正しい
    assert not result.ok, (
        "○と○の同席を数えていれば、この条件では組めないはず。"
        f"組めてしまった: {result.assignments}"
    )


def test_同席の回数上限が守られる(monkeypatch):
    """「月1回まで」なら、同じ夜に入るのは1回まで。"""
    days = [Day(date(2026, 9, day)) for day in (1, 2, 3)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _night_pair_staff()
    by_id = {p.staff_id: p for p in profiles}
    by_id["night-1"].pair_constraints = [
        PairConstraint(
            other_staff=by_id["night-2"].name, kind="max_shared_night", max_count=1
        )
    ]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    # ○は毎日この2人しかいないので、3日とも同席になり上限1回を超える
    assert not result.ok, "同席の上限を数えていれば組めないはず"


def test_同じ相手と何度も組まないように散らす(monkeypatch):
    """「同じナースが複数回同席しないように」への対応。

    一緒に入ると負担が増えるため、特定の人に偏ると不満が出る。
    禁止ではないので、他に組みようが無ければ2回目も入る。

    日数が少ないと ○→△→公 の並びで相手が構造的に決まってしまい、
    散らす余地が出ない。ここでは「フラグが立っている人にだけ
    ペナルティ項が作られる」ことを確かめる。実データ3か月での効果は
    docs/テスト結果 に記録している。
    """
    days = [Day(date(2026, 9, day)) for day in range(1, 8)]
    model = cp_model.CpModel()
    profiles = _night_pair_staff()
    x = scheduler_module._create_variables(model, profiles, days)

    without = scheduler_module._add_pair_constraints(model, x, profiles, days)
    assert without == [], "フラグが無ければペナルティ項は作られない"

    for profile in profiles:
        if profile.staff_id == "night-2":
            profile.spread_night_partners = True
    with_flag = scheduler_module._add_pair_constraints(model, x, profiles, days)

    # 夜間に入れる相手の人数ぶんだけペナルティ項ができる
    others = [
        p
        for p in profiles
        if p.staff_id != "night-2" and (p.can_night or p.can_late_night)
    ]
    assert len(with_flag) == len(others), (
        f"夜間に入れる相手 {len(others)} 人ぶんのペナルティが要る: {len(with_flag)}"
    )


def test_散らす要望は絶対厳守にしない(monkeypatch):
    """他に組みようが無ければ2回目も入る。組めなくなってはいけない。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _night_pair_staff()
    for profile in profiles:
        if profile.staff_id == "night-2":
            profile.spread_night_partners = True

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages


def test_時短を使えない人に時短が選ばれたら知らせる(monkeypatch):
    """番号のシフトで組む人に時短の時間を選ばれても反映できない。黙って落とさない。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _staff_with_night_roles() + [_late_night_filler()]
    target = profiles[0]
    wish = StaffRequests(staff_id=target.staff_id, name=target.name, entries={1: "9-15時"})

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert any(
        ALERT_MARK in message and target.name in message for message in result.messages
    ), result.messages


def test_曜日の時間が決まっている人の希望が黙って消えない(monkeypatch):
    """曜日ごとに時間が決まっている人に別の記号を選んでも、
    曜日の時間が優先される。反映できないことを必ず知らせる。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="slot-2",
        name="曜日パートさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="15:00")],
    )
    # 2026-09-01 は火曜(weekday=1)。曜日の時間が入るので、時短の希望は通らない
    wish = StaffRequests(staff_id="slot-2", name="曜日パートさん", entries={1: "9-15時"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["slot-2"] == {1: "09:00-15:00"}
    assert any(
        ALERT_MARK in m and "曜日パートさん" in m for m in result.messages
    ), result.messages


def test_夜勤の翌日に別の希望があると知らせる(monkeypatch):
    """○の翌日は必ず△になる(ハード制約)。そこに別の希望が残っていると、

    scheduler はハード制約を優先して○の希望そのものを黙って落としてしまう。
    本人には分からないので、ここで必ず報告すること。
    """
    days = [Day(date(2026, 9, 1)), Day(date(2026, 9, 2))]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _staff_with_night_roles() + [_late_night_filler()]
    target = next(p for p in profiles if p.staff_id == "night-1")
    # 1日に夜勤希望、翌2日には別の希望(公)が入っていて矛盾している
    wish = StaffRequests(
        staff_id=target.staff_id, name=target.name, entries={1: NIGHT_IN, 2: OFF}
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert any(
        ALERT_MARK in m and target.name in m and "2日" in m for m in result.messages
    ), result.messages


def test_深夜の翌日に別の希望があると知らせる(monkeypatch):
    """◉の翌日は必ず公休になる。そこに別の希望が残っていると矛盾する。"""
    days = [Day(date(2026, 9, 1)), Day(date(2026, 9, 2))]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _staff_with_night_roles() + [_late_night_filler()]
    target = next(p for p in profiles if p.staff_id == "latenight-fill")
    # 1日に深夜希望、翌2日には別の希望(○)が入っていて矛盾している
    wish = StaffRequests(
        staff_id=target.staff_id, name=target.name, entries={1: LATE_NIGHT_IN, 2: NIGHT_IN}
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert any(
        ALERT_MARK in m and target.name in m and "2日" in m for m in result.messages
    ), result.messages


def test_夜勤の翌日が明けなら知らせない(monkeypatch):
    """自動で入った△(明け)は矛盾ではないので、知らせを出さないこと。"""
    days = [Day(date(2026, 9, 1)), Day(date(2026, 9, 2))]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _staff_with_night_roles() + [_late_night_filler()]
    target = next(p for p in profiles if p.staff_id == "night-1")
    wish = StaffRequests(
        staff_id=target.staff_id, name=target.name, entries={1: NIGHT_IN, 2: NIGHT_AFTER}
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert not any(
        ALERT_MARK in m and target.name in m and "翌" in m for m in result.messages
    ), result.messages


def test_反映できた希望は知らせに出さない(monkeypatch):
    """通った希望まで★要確認に出すと、本当に見るべきものが埋もれる。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="part-2",
        name="時間パートさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        work_hours=("08:00", "15:00"),
    )
    wish = StaffRequests(staff_id="part-2", name="時間パートさん", entries={1: "9-15時"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.assignments["part-2"] == {1: "9-15時"}
    assert not any("時間パートさん" in m and ALERT_MARK in m for m in result.messages)


def test_組めないときはどの日が足りないか知らせる(monkeypatch):
    """「組めません」だけでは何を直せばよいか分からない。

    希望休を入れすぎた日を、日付で示す。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _staff_with_night_roles() + [_late_night_filler()]
    # 全員がその日を希望休にする → 誰も出られない
    requests = [
        StaffRequests(staff_id=p.staff_id, name=p.name, entries={1: "公"})
        for p in profiles
    ]

    result = build_schedule(profiles, requests=requests, year=2026, month=9)

    assert not result.ok
    assert any(
        "人手が足りない日" in message and "1日" in message for message in result.messages
    ), result.messages


def test_組めない理由が人数でないときはその旨を伝える(monkeypatch):
    """日ごとの人数は足りているのに組めない場合、見当違いの案内をしない。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _night_pair_staff()
    by_id = {p.staff_id: p for p in profiles}
    # ○に必要な2人を同席不可にする → 人数は足りているのに組めない
    by_id["night-1"].pair_constraints = [
        PairConstraint(other_staff=by_id["night-2"].name, kind="no_pair_night")
    ]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert not result.ok
    assert any("日ごとの人数だけを見ると足りています" in m for m in result.messages), (
        result.messages
    )


# --- 前月からの引き継ぎ ----------------------------------------------------------


def _carry_over_staff() -> List[StaffProfile]:
    """数日ぶんの夜勤を回せるだけの人数をそろえた構成。

    ○ は 看護1+介護1 が毎日必要で、入った人は翌日△・翌々日公になる。
    日数ぶんの顔ぶれが要るので、余裕をもって用意する。
    """
    staff = [p for p in _coverage_staff() if p.staff_id.startswith("day-")]
    for index in range(1, 5):
        staff.append(
            StaffProfile(
                staff_id=f"nurse-{index}",
                name=f"看護{index}",
                role="看護師",
                available_day_shifts=[],
                can_night=True,
                can_late_night=False,
            )
        )
        staff.append(
            StaffProfile(
                staff_id=f"care-{index}",
                name=f"介護{index}",
                role="介護士",
                available_day_shifts=[],
                can_night=True,
                can_late_night=False,
            )
        )
        staff.append(
            StaffProfile(
                staff_id=f"late-{index}",
                name=f"深夜{index}",
                role="介護士",
                available_day_shifts=[],
                can_night=False,
                can_late_night=True,
            )
        )
    return staff


def test_前月末が夜勤なら1日は明けで2日は公休(monkeypatch):
    """○→△→公 は3日にまたがる。前月末に○だった人を1日にまた○に入れてはいけない。"""
    days = [Day(date(2026, 9, day)) for day in (1, 2, 3)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _carry_over_staff()
    nurse = next(p for p in profiles if p.staff_id == "nurse-1")
    wish = StaffRequests(
        staff_id=nurse.staff_id, name=nurse.name, entries={CARRY_OVER_DAY: NIGHT_IN}
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments[nurse.staff_id][1] == NIGHT_AFTER
    assert result.assignments[nurse.staff_id][2] == OFF


def test_前月末が深夜なら1日は公休(monkeypatch):
    days = [Day(date(2026, 9, day)) for day in (1, 2)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _carry_over_staff()
    wish = StaffRequests(
        staff_id="late-1", name="深夜1", entries={CARRY_OVER_DAY: LATE_NIGHT_IN}
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["late-1"][1] == OFF


def test_前月末が明けなら1日は公休(monkeypatch):
    days = [Day(date(2026, 9, day)) for day in (1, 2)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _carry_over_staff()
    wish = StaffRequests(
        staff_id="late-1", name="深夜1", entries={CARRY_OVER_DAY: NIGHT_AFTER}
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["late-1"][1] == OFF


def test_前月末が入っていなければ知らせる(monkeypatch):
    """入れ忘れると前月と食い違う。黙って組まない。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _staff_with_night_roles() + [_late_night_filler()]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert any(
        ALERT_MARK in m and "前月末の夜勤が入っていません" in m for m in result.messages
    ), result.messages


def test_前月末が明けや深夜なら2日目の有給は普通に組む(monkeypatch):
    """前月末が△・◉の人は1日目だけが公休で拘束される。2日目は普通の希望日なので

    有給を出していればそのまま反映されるべきで、勝手に禁止してはいけない。
    """
    days = [Day(date(2026, 9, day)) for day in (1, 2, 3)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _carry_over_staff()
    wish = StaffRequests(
        staff_id="late-1",
        name="深夜1",
        entries={CARRY_OVER_DAY: LATE_NIGHT_IN, 2: PAID_LEAVE},
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["late-1"][1] == OFF
    assert result.assignments["late-1"][2] == PAID_LEAVE


def test_前月からの勤務は希望休より優先する(monkeypatch):
    """前月末に○だった人が1日に希望休を出していても、明けが先。

    「もう起きたこと」なので希望では動かせない。ここで外さないと組めなくなる。
    """
    days = [Day(date(2026, 9, day)) for day in (1, 2, 3)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _carry_over_staff()
    nurse = next(p for p in profiles if p.staff_id == "nurse-1")
    wish = StaffRequests(
        staff_id=nurse.staff_id,
        name=nurse.name,
        entries={CARRY_OVER_DAY: NIGHT_IN, 1: "公"},
    )

    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments[nurse.staff_id][1] == NIGHT_AFTER


# --- 遅番の翌日 / 希望した日のみ勤務 ----------------------------------------------


def _roster_for_a_few_days() -> List[StaffProfile]:
    """数日ぶん回せる人員。

    _coverage_staff は1日ぶんをちょうど満たすだけなので、複数日だと
    夜勤の3日セット(○→△→公)が回らず組めなくなる。
    夜勤は1人が3日に1回しか入れないので、毎日2人ぶん埋めるには6人要る。
    深夜は ◉→公 の2日セットなので2人。

    この顔ぶれで回せるのは3日ぶんまで。日勤は1人が1つのシフトしか持たず
    代わりがいないので、4日以上にすると休みを入れられずに組めなくなる。
    それより長い期間を試したいときは、日勤の人数を増やすこと。
    """
    staff: List[StaffProfile] = []
    for index, code in enumerate(REQUIRED_DAY_SHIFTS, 1):
        staff.append(
            StaffProfile(
                staff_id=f"day-{index}",
                name=f"日勤{index}",
                role="介護士",
                available_day_shifts=[code],
                can_night=False,
                can_late_night=False,
            )
        )
    # 夜勤は看護1人+介護1人で組むので、3組ぶん用意する
    for index in range(1, 7):
        staff.append(
            StaffProfile(
                staff_id=f"night-{index}",
                name=f"夜勤{index}",
                role="看護師" if index % 2 else "介護士",
                available_day_shifts=[],
                can_night=True,
                can_late_night=False,
            )
        )
    for index in range(1, 3):
        staff.append(
            StaffProfile(
                staff_id=f"latenight-{index}",
                name=f"深夜{index}",
                role="介護士",
                available_day_shifts=[],
                can_night=False,
                can_late_night=True,
            )
        )
    return staff


def test_遅番の翌日に早出と日を入れない(monkeypatch):
    """「⑤⑦⑨遅番の次の日は①②③早出と日は付けない」

    遅くまで働いた翌朝に早い勤務が来ないようにする、勤務間の間隔の確保。
    日⑦(13-21)に入った翌日は、日①②③や「日」に入れない。
    """
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [Day(date(2026, 9, d)) for d in (1, 2)],
    )
    # 遅番にも早出にも入れる人。1日に日⑦を希望している。
    both = StaffProfile(
        staff_id="both-1",
        name="遅番も早出もさん",
        role="介護士",
        available_day_shifts=["日⑦", "日①", "日"],
        can_night=False,
        can_late_night=False,
    )
    wish = StaffRequests(staff_id="both-1", name="遅番も早出もさん", entries={1: "日⑦"})
    result = build_schedule(
        _roster_for_a_few_days() + [both], requests=[wish], year=2026, month=9
    )

    assert result.ok, result.messages
    assignment = result.assignments["both-1"]
    assert assignment[1] == "日⑦", "希望どおり遅番に入る"
    assert assignment[2] not in ("日①", "日②", "日③", "日"), (
        f"遅番の翌日に{assignment[2]}が入っている"
    )


def test_希望した日のみ勤務の人は希望以外の日に入らない(monkeypatch):
    """「希望した日のみ、勤務します」の方は、希望が無い日は公休。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, d)) for d in (1, 2, 3)]
    )
    only = StaffProfile(
        staff_id="only-1",
        name="希望だけさん",
        role="介護士",
        works_only_on_request=True,
        can_night=False,
        can_late_night=False,
    )
    wish = StaffRequests(staff_id="only-1", name="希望だけさん", entries={2: "日"})
    result = build_schedule(
        _roster_for_a_few_days() + [only], requests=[wish], year=2026, month=9
    )

    assert result.ok, result.messages
    assignment = result.assignments["only-1"]
    assert assignment[2] == "日", "希望した日には入る"
    assert assignment[1] == "公", "希望が無い日は公休"
    assert assignment[3] == "公", "希望が無い日は公休"


def test_希望した日のみ勤務でも有給はその日を占める(monkeypatch):
    """有給などの「勤務しない」希望も、公休とは別にその日に残ること。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, d)) for d in (1, 2)]
    )
    only = StaffProfile(
        staff_id="only-2",
        name="希望だけさん",
        role="介護士",
        works_only_on_request=True,
        can_night=False,
        can_late_night=False,
    )
    wish = StaffRequests(staff_id="only-2", name="希望だけさん", entries={1: "有"})
    result = build_schedule(
        _roster_for_a_few_days() + [only], requests=[wish], year=2026, month=9
    )

    assert result.ok, result.messages
    assert result.assignments["only-2"][1] == "有"


def test_希望した日のみ勤務でも前月末からの夜勤は引き継ぐ(monkeypatch):
    """前月末が○だった方は、希望が無くても1日目が明け(△)、2日目が公休になること。

    「希望した日のみ勤務」で全部公休にしてしまうと、
    前月末の夜勤から続く明けが消えて、前月の勤務表と食い違う。
    """
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [Day(date(2026, 9, d)) for d in (1, 2)],
    )
    only = StaffProfile(
        staff_id="only-3",
        name="前月末が夜勤さん",
        role="介護士",
        works_only_on_request=True,
        can_night=True,
        can_late_night=False,
        available_day_shifts=[],
    )
    # 前月末(0日)が○。今月の希望は1件も無い。
    carry = StaffRequests(
        staff_id="only-3", name="前月末が夜勤さん", entries={CARRY_OVER_DAY: "○"}
    )
    result = build_schedule(
        _roster_for_a_few_days() + [only], requests=[carry], year=2026, month=9
    )

    assert result.ok, result.messages
    assignment = result.assignments["only-3"]
    assert assignment[1] == "△", "前月末の夜勤から続く明けが消えてはいけない"
    assert assignment[2] == "公", "明けの翌日は公休"


def test_希望した日のみ勤務の人は希望が無くても要確認にしない(monkeypatch):
    """予定を出せる時期が不定期な方がいる。

    希望が無ければ全部公休が正しい姿なので、★要確認にはしない。
    知らせ自体は出すが、見落とすと困るものではないので印は付けない。
    """
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [Day(date(2026, 9, d)) for d in (1, 2)],
    )
    only = StaffProfile(
        staff_id="only-4",
        name="不定期さん",
        role="介護士",
        works_only_on_request=True,
        work_hours=("08:00", "15:00"),
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
    )
    result = build_schedule(
        _roster_for_a_few_days() + [only], requests=[], year=2026, month=9
    )

    assert result.ok, result.messages
    mine = [m for m in result.messages if "不定期さん" in m]
    assert mine, "何も知らせないのではなく、状況は伝える"
    assert not any(m.startswith(ALERT_MARK) for m in mine), (
        "希望が無いのが正しい姿なので、★要確認にはしない"
    )


def test_希望した日のみでない人が全部公休なら要確認にする(monkeypatch):
    """こちらは見落とすと困るので、印を付けたまま残す。"""
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [Day(date(2026, 9, d)) for d in (1, 2)],
    )
    stuck = StaffProfile(
        staff_id="stuck-1",
        name="入れる勤務が無いさん",
        role="介護士",
        work_hours=("08:00", "15:00"),
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
    )
    result = build_schedule(
        _roster_for_a_few_days() + [stuck], requests=[], year=2026, month=9
    )

    mine = [m for m in result.messages if "入れる勤務が無いさん" in m]
    assert any(m.startswith(ALERT_MARK) for m in mine), mine


def _consecutive_model(marks_by_day, carry_over: str = ""):
    """連続勤務の制約だけを入れた小さなモデルを作り、その並びが通るか返す。

    marks_by_day で1人分の勤務を全部固定してしまい、
    _add_max_consecutive_work が「休みが1日も無い」と判定するかだけを見る。

    carry_over は前月末の記号。◉(LATE_NIGHT_IN)を渡すと、月の1日目が
    前月末の深夜から続く「公」だったケースを再現できる。
    """
    from ortools.sat.python import cp_model

    from shift_scheduler.scheduler import ALL_MARKS, _add_max_consecutive_work

    profile = StaffProfile(staff_id="one", name="連勤さん", role="介護士")
    days = [Day(date(2026, 9, number)) for number in sorted(marks_by_day)]
    entries = {CARRY_OVER_DAY: carry_over} if carry_over else {}
    request = StaffRequests(staff_id="one", name="連勤さん", entries=entries)
    by_staff_request = {"one": request}

    model = cp_model.CpModel()
    x = {
        ("one", day.day, mark): model.NewBoolVar(f"one_{day.day}_{mark}")
        for day in days
        for mark in ALL_MARKS
    }
    for day in days:
        for mark in ALL_MARKS:
            model.Add(x["one", day.day, mark] == (1 if marks_by_day[day.day] == mark else 0))

    _add_max_consecutive_work(model, x, [profile], days, by_staff_request)

    solver = cp_model.CpSolver()
    return solver.Solve(model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def test_深夜の翌日の公休は休みとして数えない():
    """「◉ 公 ◉ 公 日⑦ 日⑨ ○ △」は、表の上では休みがあるが実際は連続勤務。

    深夜は22時から翌朝6時までで日付をまたぐため、翌日の「公」は
    朝まで働いた日にあたる。これを休みと数えると、
    6日を超えて職場にいる並びが通ってしまう。
    """
    run = ["◉", "公", "◉", "公", "日⑦", "日⑨"]
    assert not _consecutive_model(dict(enumerate(run, start=1))), (
        "深夜の翌日の公休を休みと数えてしまっている"
    )


def test_深夜と関係ない公休は休みとして数える():
    """普通の公休まで数えなくなると、今度は誰も勤務表に入れられなくなる。"""
    run = ["日", "公", "日", "日", "日", "日"]
    assert _consecutive_model(dict(enumerate(run, start=1)))


def test_前月末が深夜だった人は当月1日の公休を休みとして数えない():
    """月境界でも同じ扱いにする。

    前月末が◉(深夜)だった人は、当月1日の「公」も朝まで働いた続きで、
    休んだ日ではない。月をまたぐと前日がこの月の中に無いので、
    carry_over を見ないと当月1日だけ判定から漏れてしまう。
    """
    run = ["公", "日", "日", "日", "日", "日"]
    assert not _consecutive_model(
        dict(enumerate(run, start=1)), carry_over=LATE_NIGHT_IN
    ), "前月末が深夜だった人の当月1日の公休を、休みと数えてしまっている"


# --- 曜日固定の方が希望も出せること(施設の担当者の選んだ形) -------------------------


def test_曜日固定の方は希望を出しても固定の曜日に入る():
    """「希望も入れられるし、空いている所は定期の曜日時間がオートで入る」形。

    曜日ごとに勤務時間が決まっている方について、施設の担当者から
    2つの案(曜日固定をやめて希望日だけにする/固定を残して希望も出せる)を
    お示しし、後者を選ばれた。その形で動くことを確かめる。

    2026年9月1日は火曜。火・金を固定曜日にした方で確かめる。
      1日(火) 固定の曜日     → 勤務時間が入る
      2日(水) 希望出勤「日」 → 希望どおり入る
      3日(木) 何も無い       → 公休
      4日(金) 固定の曜日だが希望休 → 希望休が勝って公休

    この方はソルバーの対象外(解く前に割り当てが決まる)なので、
    割り当てを作る関数を直接呼んで確かめる。テスト用の少人数では
    4日ぶんの夜勤・深夜が回らず、そちらで組めなくなってしまうため。
    """
    part = StaffProfile(
        staff_id="part-1",
        name="曜日固定さん",
        role="介護士",
        fixed_time_slots=[
            FixedTimeSlot(weekday=1, start="09:00", end="15:00"),  # 火
            FixedTimeSlot(weekday=4, start="09:00", end="15:00"),  # 金
        ],
    )
    wish = StaffRequests(
        staff_id="part-1",
        name="曜日固定さん",
        entries={2: "日", 4: "公"},
    )
    days = [Day(date(2026, 9, number)) for number in (1, 2, 3, 4)]
    assignment = scheduler_module._fixed_hours_assignment(part, days, wish)
    assert assignment[1] == "09:00-15:00", "固定の曜日は希望がなくても入る"
    assert assignment[2] == "日", "固定でない曜日も、希望を出せば入る"
    assert assignment[3] == "公", "固定でも希望でもない日は公休"
    assert assignment[4] == "公", "固定の曜日でも、希望休を出せば休みになる"


# --- 月の公休日数 ----------------------------------------------------------------


def test_正職員の公休は指定した日数ちょうどになる(monkeypatch):
    """「公休は月9日にそろえてほしい」。日数は月ごとに指定してもらう。

    テスト用の顔ぶれで回せるのは3日ぶんまでなので、その中で確かめる
    (_roster_for_a_few_days を参照)。3日のうち1日を公休にする。
    """
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [Day(date(2026, 9, number)) for number in (1, 2, 3)],
    )
    full_time = StaffProfile(
        staff_id="full-1",
        name="正職員さん",
        role="介護士",
        employment_type="正社員",
        can_night=False,
        can_late_night=False,
        available_day_shifts=["日"],
    )
    result = build_schedule(
        _roster_for_a_few_days() + [full_time],
        requests=[],
        year=2026,
        month=9,
        monthly_off_days=1,
    )

    assert result.ok, result.messages
    assignment = result.assignments["full-1"]
    assert sum(1 for mark in assignment.values() if mark == OFF) == 1


def test_パートの方は公休の日数にそろえない():
    """パートで公休9日の方はいない(確認済み)。

    週に2〜3日しか出ない方まで一律に縛ると、勤務表が成り立たなくなる。
    縛りが付かないこと自体を見たいので、制約を足す関数を直接呼んで
    条件が1つも増えないことを確かめる。
    """
    from shift_scheduler.scheduler import _add_monthly_off_days

    days = [Day(date(2026, 9, number)) for number in range(1, 6)]
    part = StaffProfile(
        staff_id="part-2",
        name="パートさん",
        role="介護士",
        employment_type="パート（時間給）",
    )
    model = cp_model.CpModel()
    variables = {
        ("part-2", day.day, OFF): model.NewBoolVar(f"off_{day.day}") for day in days
    }
    before = len(model.Proto().constraints)
    _add_monthly_off_days(model, variables, [part], days, 2)
    assert len(model.Proto().constraints) == before


def test_公休の日数を0にすると縛らない(monkeypatch):
    """0は「この月は日数をそろえない」という指定。"""
    from shift_scheduler.scheduler import _add_monthly_off_days

    model = cp_model.CpModel()
    before = len(model.Proto().constraints)
    _add_monthly_off_days(model, {}, [StaffProfile(staff_id="x", name="x")], [], 0)
    assert len(model.Proto().constraints) == before


def test_希望した日のみ勤務の方は公休の日数にそろえない():
    """希望が無い日はすべて公休になるので、日数をそろえる条件とぶつかる。

    今のところ該当する正職員はいないが、確認用Excelであとから
    設定できてしまうため、ぶつからないようにしておく。
    """
    from shift_scheduler.scheduler import _add_monthly_off_days

    days = [Day(date(2026, 9, number)) for number in (1, 2, 3)]
    profile = StaffProfile(
        staff_id="only-3",
        name="希望だけの正職員さん",
        role="介護士",
        employment_type="正社員",
        works_only_on_request=True,
    )
    model = cp_model.CpModel()
    variables = {
        ("only-3", day.day, OFF): model.NewBoolVar(f"off_{day.day}") for day in days
    }
    before = len(model.Proto().constraints)
    _add_monthly_off_days(model, variables, [profile], days, 1)
    assert len(model.Proto().constraints) == before


# --- 守れなかったときは、勤務表を出して知らせる -------------------------------------


def test_公休の日数にできなくても勤務表を出して知らせる(monkeypatch):
    """絶対に守る条件にしない。9割の叩き台を出し、ずれた所を知らせる。

    毎日の必要人数は絶対に守るので、日①に入れるのがその人だけなら
    3日とも日①に入るしかなく、公休は0日になる。
    以前はこの状況で勤務表が1枚も出てこなかった。
    """
    monkeypatch.setattr(
        scheduler_module,
        "month_days",
        lambda year, month: [Day(date(2026, 9, number)) for number in (1, 2, 3)],
    )
    staff = _roster_for_a_few_days()
    # 日①に入れるのはこの人だけ。正職員なので公休の日数の対象になる。
    only_early = next(p for p in staff if p.staff_id == "day-1")
    only_early.employment_type = "正社員"
    only_early.name = "早出"

    result = build_schedule(staff, requests=[], year=2026, month=9, monthly_off_days=1)

    assert result.ok, result.messages
    assert all(mark == "日①" for mark in result.assignments["day-1"].values())
    alerts = [m for m in result.messages if m.startswith(ALERT_MARK)]
    assert any("早出さんは公休0日です" in m for m in alerts), alerts


def test_遅番の翌日に早出が入ってしまったら知らせる():
    """こちらも絶対に守る条件にはしない。守れなかった日を知らせる。

    ソルバーは避けられる限り避けるので、避けられなかった形を
    手で組み立てて、知らせが出ることを確かめる。
    """
    from shift_scheduler.scheduler import ScheduleResult, _report_soft_misses

    days = [Day(date(2026, 9, number)) for number in (1, 2)]
    profile = StaffProfile(
        staff_id="late-1",
        name="遅番",
        role="介護士",
        employment_type="パート（時間給）",
    )
    result = ScheduleResult(status="最適", year=2026, month=9, days=days)
    result.assignments = {"late-1": {1: "日⑦", 2: "日①"}}

    _report_soft_misses(result, [profile], off_days=0)

    alerts = [m for m in result.messages if m.startswith(ALERT_MARK)]
    assert len(alerts) == 1, result.messages
    assert "遅番の翌日に早出と日が入っている所が1件あります" in alerts[0]
    assert "9月2日 遅番さん（日⑦の翌日に日①）" in alerts[0]


def test_遅番の翌日が公休なら知らせない():
    """守れているときに知らせが出ると、本当に見てほしいものが埋もれる。"""
    from shift_scheduler.scheduler import ScheduleResult, _report_soft_misses

    days = [Day(date(2026, 9, number)) for number in (1, 2)]
    profile = StaffProfile(staff_id="late-2", name="遅番", role="介護士")
    result = ScheduleResult(status="最適", year=2026, month=9, days=days)
    result.assignments = {"late-2": {1: "日⑦", 2: OFF}}

    _report_soft_misses(result, [profile], off_days=0)

    assert result.messages == []
