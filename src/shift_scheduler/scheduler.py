"""月次勤務表を組む。OR-Tools CP-SAT を使う。

絶対に守るルール(ハード制約)は model.Add で直接表し、
できるだけ守りたいこと(ソフト制約)は目的関数のペナルティにする。
組めない場合は INFEASIBLE を返すので、「この条件では組めない」を機械的に検知できる。

勤務表の記号:
  日①②③⑤⑦⑨   毎日ちょうど1人ずつ
  日             人数の決まりなし。残りの勤務者が入る枠
  ○ → △ → 公     夜勤の3日セット(毎日○が2人)
  ◉ → 公         深夜の2日セット(毎日◉が1人)
  公             公休
  せ             責任者(師長が休みの日だけ、日責ができる人を1人)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

from .calendar_utils import Day, month_days
from .models import StaffProfile
from .request_sheet import StaffRequests
from .shifts import (
    ABSENCE_MARKS,
    DEFAULT_MONTHLY_OFF_DAYS,
    MAX_CONSECUTIVE_WORK_DAYS,
    DAILY_REQUIREMENT,
    DAY_SHIFT_CODES,
    EARLY_SHIFTS,
    HOUR_CHOICES,
    LATE_NIGHT_IN,
    LATE_SHIFTS,
    NIGHT_AFTER,
    NIGHT_IN,
    OFF,
    REQUIRED_DAY_SHIFTS,
    SELF_WRITTEN_MARKS,
)

# 割り当てうる記号(時間直書きの人を除く)
ALL_MARKS: Tuple[str, ...] = (
    tuple(DAY_SHIFT_CODES) + (NIGHT_IN, NIGHT_AFTER, LATE_NIGHT_IN, OFF) + ABSENCE_MARKS
)

# ソフト制約の重み。大きいほど優先して守る。
WEIGHT_WISH_WORK = 40  # 希望出勤に応える
WEIGHT_NIGHT_COUNT = 20  # 夜勤・深夜の希望回数の範囲
WEIGHT_WORK_DAYS = 8  # 週の勤務日数の目安
WEIGHT_WEEKEND_OFF = 5  # 土日どちらかは休みたい
WEIGHT_AVOID_EARLY = 5  # 早出には入れていない
# 「他の人で埋まらないときだけ使う」枠。他のどのソフト制約より重くして、
# 本当に最後の手段にする(これを破るくらいなら他の希望を諦める、という強さ)。
# 夜勤明けの翌日にまた夜勤に入る形。実物に1件あり許容されるが、
# 体への負担が大きいので、他に手が無いときだけになるよう強めに抑える。
WEIGHT_NIGHT_AFTER_NIGHT = 200

WEIGHT_LAST_RESORT = 300

# 「できるだけ守る」に回した2つ。
# このアプリが作るのは叩き台で、9割できた状態から人が手で直す前提。
# 絶対に守る条件にすると、守れない月に勤務表が1枚も出てこない。
# それより、出したうえで守れなかった所を知らせる方がよい、と
# 施設の担当者と決めた。
#
# 公休の日数は労務に直結するので重くする。希望出勤(40)より優先する。
WEIGHT_OFF_DAYS = 60
# 遅番の翌日の早出。体への負担の話なので軽くはしないが、
# 公休の日数ほどではない。
WEIGHT_REST_AFTER_LATE = 45

# 同じ相手と夜間に2回以上組むことへのペナルティ(1回超過あたり)。
# 「夜勤は同じナースが複数回同席しないようにして下さい」への対応。
# 一緒に入ると負担が増えるため、特定の人に偏ると不満が出る。
# 絶対厳守ではないので、希望出勤(40)より軽く、必要人数より優先はしない。
WEIGHT_PARTNER_REPEAT = 25

# 深夜(◉)は介護職が8〜9割。看護職で入るのは決まった人だけ。
# 深夜(◉)に入れる看護職は、profile.can_late_night_as_nurse で指定する。
# 誰がその人かは施設ごとに違うので、コードに名前は書かない。

# 遅番の翌日に入れたくない勤務のうち、早出(日①②)以外のもの。
# 施設の担当者のご要望「⑤⑦⑨遅番の次の日は①②③早出と日は付けない」の
# 「③」と「日」にあたる。早出の定義(日①②)は変えないこと。
REST_AFTER_LATE_EXTRA: Tuple[str, ...] = ("日③", "日")

# 見落とすと勤務表がおかしいまま渡ってしまう知らせに付ける印。
# 画面側はこの印で並べ替えず、必ず目に入る場所に出す。
ALERT_MARK = "★要確認"

# 希望が無い人を表す空の入れ物。毎回 None を確かめなくて済むようにする。
_NO_REQUEST = StaffRequests(staff_id="", name="")


@dataclass
class ScheduleResult:
    """組んだ結果。"""

    status: str  # "最適" / "実行可能" / "組めません" / "時間切れ"
    year: int
    month: int
    days: List[Day] = field(default_factory=list)
    # staff_id -> {日にち: 記号}。時間直書きの人は記号の代わりに "9:00-15:00" など。
    assignments: Dict[str, Dict[int, str]] = field(default_factory=dict)
    # 責任者「せ」。{日にち: staff_id}。師長が出勤している日は入らない。
    responsible: Dict[int, str] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("最適", "実行可能")


def build_schedule(
    profiles: Sequence[StaffProfile],
    requests: Sequence[StaffRequests],
    year: int,
    month: int,
    time_limit_seconds: float = 60.0,
    monthly_off_days: int = DEFAULT_MONTHLY_OFF_DAYS,
) -> ScheduleResult:
    """monthly_off_days は正職員の公休の日数。月ごとに変わるので毎回指定する。"""
    days = month_days(year, month)
    result = ScheduleResult(status="組めません", year=year, month=month, days=days)

    by_staff_request = {request.staff_id: request for request in requests}

    # 番号のシフトに入らない人(曜日ごとに勤務時間が決まっているパート)は
    # 解く前に決めてしまう。ソルバーの対象からは外す。
    solver_profiles: List[StaffProfile] = []
    for profile in profiles:
        if profile.is_on_leave(year, month):
            result.messages.append(
                f"{profile.name}: {profile.leave_from} から休職のため、勤務表に入れていません"
            )
            continue
        if profile.is_support_staff or profile.fixed_time_slots:
            # 介護補助は環境整備などが仕事で、日勤帯の人数にも夜勤の輪番にも入らない。
            # 曜日ごとに勤務時間が決まっている人も同じく、時間をそのまま書く。
            result.assignments[profile.staff_id] = _fixed_hours_assignment(
                profile, days, by_staff_request.get(profile.staff_id)
            )
            reason = (
                "介護補助のため、決まった時間をそのまま入れました(人数計算には数えません)"
                if profile.is_support_staff
                else "曜日ごとの勤務時間が決まっているため、時間をそのまま入れました"
            )
            result.messages.append(f"{profile.name}: {reason}")
        else:
            solver_profiles.append(profile)

    solver_profiles = _move_out_unworkable(solver_profiles, days, result, by_staff_request)
    _report_unusable_wishes(profiles, solver_profiles, days, result, by_staff_request)
    _report_night_sequence_conflicts(profiles, days, result, by_staff_request)

    if not solver_profiles:
        result.status = "実行可能"
        return result

    model = cp_model.CpModel()
    x = _create_variables(model, solver_profiles, days)

    _add_one_mark_per_day(model, x, solver_profiles, days)
    _add_availability(model, x, solver_profiles, days)
    carry_night_ids = {
        profile.staff_id
        for profile in solver_profiles
        if (by_staff_request.get(profile.staff_id) or _NO_REQUEST).carry_over == NIGHT_IN
    }
    _add_night_sequences(model, x, solver_profiles, days, carry_night_ids)
    _add_coverage(model, x, solver_profiles, days)
    _add_night_composition(model, x, solver_profiles, days)
    _add_carry_over(model, x, solver_profiles, days, by_staff_request, result)
    _add_fixed_days_off(model, x, solver_profiles, days, by_staff_request)
    _add_monthly_off_quota(model, x, solver_profiles, days)
    off_day_terms = _add_monthly_off_days(
        model, x, solver_profiles, days, monthly_off_days
    )
    _add_weekly_days_off(model, x, solver_profiles, days, by_staff_request)
    _add_max_consecutive_work(model, x, solver_profiles, days, by_staff_request)
    late_shift_terms = _add_rest_after_late_shift(model, x, solver_profiles, days)
    _add_request_only_days(model, x, solver_profiles, days, by_staff_request)
    pair_terms = _add_pair_constraints(model, x, solver_profiles, days)
    se = _add_responsible(model, x, solver_profiles, days, result)

    penalties = _soft_constraints(model, x, solver_profiles, days, by_staff_request)
    penalties += pair_terms + off_day_terms + late_shift_terms
    model.Minimize(sum(penalties) if penalties else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status == cp_model.OPTIMAL:
        result.status = "最適"
    elif status == cp_model.FEASIBLE:
        result.status = "実行可能"
        result.messages.append(
            "時間内に最良とは限らない解を返しました。時間を延ばすと改善する場合があります。"
        )
    elif status == cp_model.INFEASIBLE:
        result.status = "組めません"
        result.messages.append(
            "条件を全部同時に満たす組み方が存在しません。"
            "毎日の必要人数に対して、入れる人が足りていない可能性があります。"
        )
        result.messages.extend(
            _diagnose_shortage(solver_profiles, days, by_staff_request)
        )
        return result
    else:
        result.status = "時間切れ"
        result.messages.append("制限時間内に解が見つかりませんでした。時間を延ばしてください。")
        return result

    for profile in solver_profiles:
        assignment: Dict[int, str] = {}
        for day in days:
            for mark in ALL_MARKS:
                if solver.Value(x[profile.staff_id, day.day, mark]):
                    assignment[day.day] = mark
                    break
        result.assignments[profile.staff_id] = assignment

    for (staff_id, day_number), variable in se.items():
        if solver.Value(variable):
            result.responsible[day_number] = staff_id

    _report_soft_misses(result, solver_profiles, monthly_off_days)
    return result


def _report_soft_misses(result, profiles, off_days: int) -> None:
    """「できるだけ守る」にした条件のうち、守れなかった所を知らせる。

    このアプリが作るのは叩き台で、9割できた状態から人が手で直す。
    守れなかった所が分からないと直せないので、必ず目に入る場所に出す。
    施設の担当者にお伝えしている文言と同じ形にする。
    """
    if off_days:
        missed = []
        for profile in _off_days_targets(profiles):
            assignment = result.assignments.get(profile.staff_id, {})
            actual = sum(1 for mark in assignment.values() if mark == OFF)
            if actual != off_days:
                missed.append(f"{profile.name}さんは公休{actual}日です")
        if missed:
            result.messages.append(
                f"{ALERT_MARK} 公休を{off_days}日にできなかった方が"
                f"{len(missed)}名いらっしゃいます … " + " ／ ".join(missed)
            )

    forbidden_next = tuple(EARLY_SHIFTS) + REST_AFTER_LATE_EXTRA
    day_numbers = [day.day for day in result.days]
    broken = []
    for profile in profiles:
        assignment = result.assignments.get(profile.staff_id, {})
        for index, day in enumerate(day_numbers[:-1]):
            tomorrow = day_numbers[index + 1]
            if (
                assignment.get(day) in LATE_SHIFTS
                and assignment.get(tomorrow) in forbidden_next
            ):
                broken.append(
                    f"{result.month}月{tomorrow}日 {profile.name}さん"
                    f"（{assignment[day]}の翌日に{assignment[tomorrow]}）"
                )
    if broken:
        result.messages.append(
            f"{ALERT_MARK} 遅番の翌日に早出と日が入っている所が"
            f"{len(broken)}件あります … " + " ／ ".join(broken)
        )


# --- 変数 ---------------------------------------------------------------------


def _create_variables(model, profiles, days):
    return {
        (profile.staff_id, day.day, mark): model.NewBoolVar(
            f"{profile.staff_id}_{day.day}_{mark}"
        )
        for profile in profiles
        for day in days
        for mark in ALL_MARKS
    }


# --- ハード制約 ----------------------------------------------------------------


def _add_one_mark_per_day(model, x, profiles, days) -> None:
    """1人1日につき、必ず1つの記号。"""
    for profile in profiles:
        for day in days:
            model.AddExactlyOne(x[profile.staff_id, day.day, mark] for mark in ALL_MARKS)


def allowed_entry_marks(profile, day) -> set:
    """その人がその日に「入れる」勤務の記号。

    △(夜勤明け)と公(公休)は入りから決まるので含めない。
    可否の判定はここ1か所に集約する。制約を足す側と事前チェックとで
    判定が食い違うと、誰も勤務できない人が黙って全公休になってしまう。
    """
    marks = set(profile.shifts_allowed_on(day.weekday))
    if profile.can_night:
        marks.add(NIGHT_IN)
    if profile.can_late_night:
        marks.add(LATE_NIGHT_IN)

    # 夜勤に入る曜日が決まっている人
    if profile.night_weekdays and day.weekday not in profile.night_weekdays:
        marks.discard(NIGHT_IN)

    # 勤務形態の限定。
    # 施設の担当者の言う「夜勤」は夜間業務のことで、○(夜勤)と◉(深夜)の両方を指す。
    # だから「夜勤専従」は「夜間専従(日勤なし)」の意味で、○も◉も入る。
    # 「深夜勤務のみ」はその中の◉だけを名指ししているので、両方書かれていても
    # 矛盾ではなく、狭い方の「深夜のみ」が効く。
    # 実物の2026年7月でも、両方書かれているこの人は◉だけで○は1回も無かった。
    if profile.late_night_only:
        marks &= {LATE_NIGHT_IN}
    elif profile.night_shift_exclusive:
        marks &= {NIGHT_IN, LATE_NIGHT_IN}
    elif profile.day_shift_only:
        marks -= {NIGHT_IN, LATE_NIGHT_IN}
    return marks


def _add_availability(model, x, profiles, days) -> None:
    """入れない勤務を禁止する。"""
    entry_marks = tuple(DAY_SHIFT_CODES) + (NIGHT_IN, LATE_NIGHT_IN)
    for profile in profiles:
        for day in days:
            allowed = allowed_entry_marks(profile, day)
            for mark in entry_marks:
                if mark not in allowed:
                    model.Add(x[profile.staff_id, day.day, mark] == 0)


def _add_night_sequences(model, x, profiles, days, carry_night_ids=()) -> None:
    """夜勤は ○→△→公、深夜は ◉→公 の並びを守る。

    carry_night_ids は、前月の最終日が○だった人。この人たちは1日が明け(△)に
    なるので、「月初に明けは付けない」という決めを外す。
    """
    day_numbers = [day.day for day in days]
    last = day_numbers[-1]

    for profile in profiles:
        staff = profile.staff_id
        for day in day_numbers:
            night = x[staff, day, NIGHT_IN]
            after = x[staff, day, NIGHT_AFTER]

            # 明け(△)は、前日が夜勤(○)のときだけ
            if day == day_numbers[0]:
                # 月初の明けは前月の夜勤しだい。前月末が○だと分かっている人は
                # _add_carry_over が明けに固定するので、ここでは縛らない。
                if staff not in carry_night_ids:
                    model.Add(after == 0)
            else:
                model.Add(after == x[staff, day - 1, NIGHT_IN])

            # 夜勤の翌々日は原則として公休。
            # ただし実物には「明けの翌日に次の夜勤」が1件あり、許容されるとのこと。
            # そこで「公休 または 次の夜勤」を認め、公休から外れた分はペナルティで抑える。
            if day + 2 <= last:
                model.AddBoolOr(
                    [
                        night.Not(),
                        x[staff, day + 2, OFF],
                        x[staff, day + 2, NIGHT_IN],
                    ]
                )

            # 深夜(◉)の翌日は公休
            if day + 1 <= last:
                model.AddImplication(x[staff, day, LATE_NIGHT_IN], x[staff, day + 1, OFF])


def _add_coverage(model, x, profiles, days) -> None:
    """毎日の必要人数を満たす。人数は shifts.DAILY_REQUIREMENT に従う。

    番号付きの日勤帯は「ちょうど1人」。実物の勤務表に6種がそろっているかを
    判定する行があり、毎日1人ずつで組まれているため。
    「日」は人数の決まりが無いので、ここでは縛らない。
    """
    for day in days:
        for code in REQUIRED_DAY_SHIFTS:
            model.Add(
                sum(x[profile.staff_id, day.day, code] for profile in profiles)
                == DAILY_REQUIREMENT[code]
            )
        for mark in (NIGHT_IN, LATE_NIGHT_IN):
            model.Add(
                sum(x[profile.staff_id, day.day, mark] for profile in profiles)
                == DAILY_REQUIREMENT[mark]
            )


def _add_night_composition(model, x, profiles, days) -> None:
    """夜間の顔ぶれを決める。

      ○(夜勤) 2人 … 看護職1人 + 介護職1人
      ◉(深夜) 1人 … 原則は介護職。看護職で入れるのは
                     can_late_night_as_nurse が立っている人だけ

    どちらも実物の2026年7月で確かめた(夜勤は31日中30日がこの組み合わせ、
    深夜は介護職90%・看護職10%でその全部が同じ1人)。
    """
    nurses = [p for p in profiles if p.is_nurse]
    caregivers = [p for p in profiles if p.is_caregiver]

    for day in days:
        if nurses:
            model.Add(
                sum(x[p.staff_id, day.day, NIGHT_IN] for p in nurses) == 1
            )
        if caregivers:
            model.Add(
                sum(x[p.staff_id, day.day, NIGHT_IN] for p in caregivers) == 1
            )

    # 深夜に入れない看護職を止める
    for profile in profiles:
        if not profile.is_nurse or profile.can_late_night_as_nurse:
            continue
        for day in days:
            model.Add(x[profile.staff_id, day.day, LATE_NIGHT_IN] == 0)


def _add_carry_over(model, x, profiles, days, by_staff_request, result) -> None:
    """前月から続く夜勤・深夜を、今月1日・2日に反映する。

    夜勤は ○→△→公 の3日、深夜は ◉→公 の2日にまたがる。
    前月の最終日が分からないと、前月末に○だった人を今月1日にまた○に
    入れてしまい、夜勤明けなしの2晩連続になる。勤務表を見ても気づけない。

      前月最終日が ○ → 今月1日は △、2日は 公
      前月最終日が △ → 今月1日は 公
      前月最終日が ◉ → 今月1日は 公

    実物の勤務表でも、毎月1日には前月から続く △ が2人いる。
    """
    if not days:
        return
    first = days[0].day
    second = days[1].day if len(days) > 1 else None

    applied = Counter()
    for profile in profiles:
        request = by_staff_request.get(profile.staff_id)
        mark = request.carry_over if request else ""
        if not mark:
            continue
        if mark == NIGHT_IN:
            model.Add(x[profile.staff_id, first, NIGHT_AFTER] == 1)
            if second is not None:
                model.Add(x[profile.staff_id, second, OFF] == 1)
        elif mark in (NIGHT_AFTER, LATE_NIGHT_IN):
            model.Add(x[profile.staff_id, first, OFF] == 1)
        else:
            result.messages.append(
                f"{ALERT_MARK} {profile.name}: 前月末の記号「{mark}」は"
                "○ / △ / ◉ のどれかにしてください。今回は使っていません"
            )
            continue
        applied[mark] += 1

    if not applied:
        result.messages.append(
            f"{ALERT_MARK} 前月末の夜勤が入っていません。"
            "入れないと、前月末に夜勤だった方を1日にまた夜勤に入れてしまうことがあります"
        )
        return

    result.messages.append(
        "前月から続く勤務を反映しました("
        + "、".join(f"{mark}{count}人" for mark, count in applied.items())
        + ")"
    )
    if applied[NIGHT_IN] > DAILY_REQUIREMENT[NIGHT_IN]:
        result.messages.append(
            f"{ALERT_MARK} 前月末の○が{applied[NIGHT_IN]}人います"
            f"(毎日{DAILY_REQUIREMENT[NIGHT_IN]}人のはずです)。入力をご確認ください"
        )
    if applied[LATE_NIGHT_IN] > DAILY_REQUIREMENT[LATE_NIGHT_IN]:
        result.messages.append(
            f"{ALERT_MARK} 前月末の◉が{applied[LATE_NIGHT_IN]}人います"
            f"(毎日{DAILY_REQUIREMENT[LATE_NIGHT_IN]}人のはずです)。入力をご確認ください"
        )


def _add_fixed_days_off(model, x, profiles, days, by_staff_request) -> None:
    """毎週の固定休み・希望休・有給などを固定する。

    有給・夏休・研修・健診は、希望で指定された日にだけ入れる。
    こちらで勝手に割り当てるものではないので、指定が無い日は使わない。
    """
    first_day = {days[0].day} if days else set()
    first_two = {day.day for day in days[:2]}
    for profile in profiles:
        request = by_staff_request.get(profile.staff_id)
        wish_off = set(request.wish_off_days()) if request else set()
        absences = request.absence_days() if request else {}

        # 前月から続く勤務は「もう起きたこと」なので、希望より優先する。
        # ここで外さないと、希望休とぶつかって組めなくなる。
        # ただし拘束される日数は前月末の記号によって違う。
        # ○(NIGHT_IN)なら1日目=△・2日目=公の両日、
        # △(NIGHT_AFTER)・◉(LATE_NIGHT_IN)なら1日目=公のみで、2日目は普通に組む対象。
        if request and request.carry_over:
            fixed_days = first_two if request.carry_over == NIGHT_IN else first_day
            wish_off -= fixed_days
            absences = {d: m for d, m in absences.items() if d not in fixed_days}

        for day in days:
            if day.day in absences:
                # 有給などが指定された日は、その記号で確定
                model.Add(x[profile.staff_id, day.day, absences[day.day]] == 1)
                continue
            if day.weekday in profile.fixed_off_weekdays or day.day in wish_off:
                model.Add(x[profile.staff_id, day.day, OFF] == 1)

        # 指定の無い日に有給などを勝手に入れない
        for mark in ABSENCE_MARKS:
            for day in days:
                if absences.get(day.day) != mark:
                    model.Add(x[profile.staff_id, day.day, mark] == 0)


def _add_weekly_days_off(model, x, profiles, days, by_staff_request) -> None:
    """週休2日にする。月に何日と数えるのではなく、週ごとに休みを確保する。

    夜勤明けの次の「公」も、この休みに含める(実物がそうなっている)。
    月をまたぐ週は日数が足りないので、その週にある日数に応じて緩める。

    「週◯日勤務」と決まっているパート職員は、休みの数え方が違うので対象にしない。
    """
    for profile in profiles:
        if _has_own_schedule(profile):
            continue
        # 週5日勤務なら休みは2日。それ以外は 7 - 週の勤務日数。
        weekly_work = profile.weekly_work_days or 5
        needed = max(0, 7 - weekly_work)
        if needed == 0:
            continue

        for week in _weeks(days):
            # 月初・月末の半端な週は、その週にある日数に応じて減らす
            quota = needed if len(week) == 7 else (needed * len(week)) // 7
            if quota == 0:
                continue
            model.Add(
                sum(x[profile.staff_id, day.day, OFF] for day in week) >= quota
            )


def _weeks(days):
    """月曜始まりで週に区切る。月初・月末は半端な週になる。"""
    weeks, current = [], []
    for day in days:
        if day.weekday == 0 and current:
            weeks.append(current)
            current = []
        current.append(day)
    if current:
        weeks.append(current)
    return weeks


def _has_own_schedule(profile) -> bool:
    """勤務の形が個別に決まっていて、週休2日や連続勤務の上限を当てはめない人か。

    対象は「曜日ごとに勤務時間が決まっているパート」と「介護補助」。
    work_hours(全日共通の時間枠)だけの人は、番号シフトで普通に働く常勤なので
    含めない。ここを writes_own_hours で判定すると、条件文に
    「6時から17時迄の枠」と書かれた常勤まで外れてしまう。
    """
    return bool(profile.fixed_time_slots or profile.is_support_staff)


def _add_rest_after_late_shift(model, x, profiles, days) -> List:
    """遅番の翌日に早出と「日」を入れない。

    施設の担当者のご要望:
      「⑤⑦⑨遅番の次の日は①②③早出と日は付けない」

    日⑤(11-19) 日⑦(13-21) 日⑨(12:30-20:30) の翌日に
    日①(6:00開始) 日②(7:00) 日③(8:00) 日(9:00) を割り当てない。
    遅くまで働いた翌朝に早い勤務が来ないようにする、勤務間の間隔の確保。

    翌日に残るのは 遅番・夜勤・深夜・公休・有給など。

    絶対に守る条件にはしない。守れない月に勤務表が1枚も出てこないより、
    出したうえで「〇月〇日は遅番の翌日に早出が入っています」と
    知らせる方がよい、と施設の担当者と決めた。ペナルティを返す。
    """
    forbidden_next = tuple(EARLY_SHIFTS) + REST_AFTER_LATE_EXTRA
    day_numbers = [day.day for day in days]

    penalties = []
    for profile in profiles:
        if _has_own_schedule(profile):
            continue
        for index, day in enumerate(day_numbers[:-1]):
            tomorrow = day_numbers[index + 1]
            # 「今日は遅番」かつ「翌日は早い勤務」のときだけ1になる印。
            # 遅番と早い勤務はそれぞれ1日1つなので、2つの和が2になる形。
            broke = model.NewBoolVar(f"late_then_early_{profile.staff_id}_{day}")
            model.Add(
                sum(x[profile.staff_id, day, late] for late in LATE_SHIFTS)
                + sum(x[profile.staff_id, tomorrow, early] for early in forbidden_next)
                - 1
                <= broke
            )
            penalties.append(WEIGHT_REST_AFTER_LATE * broke)
    return penalties


def _add_request_only_days(model, x, profiles, days, by_staff_request) -> None:
    """「希望した日のみ、勤務します」の方を、希望の入っている日だけ勤務にする。

    希望出勤が入っていない日は公休にする。
    この方たちの勤務日は、毎月の希望入力がそのまま決める。
    """
    for profile in profiles:
        if not profile.works_only_on_request or _has_own_schedule(profile):
            continue
        request = by_staff_request.get(profile.staff_id) or _NO_REQUEST
        wanted = set(request.wish_work_days())
        # 有給・研修などもその日を占めるので、勤務日とは別に残す
        occupied = wanted | set(request.absence_days())
        for day in days:
            if day.day in occupied:
                continue
            # 夜勤の翌日の明け(△)だけは、前日の夜勤から必ず続くので許す
            for mark in ALL_MARKS:
                if mark not in (OFF, NIGHT_AFTER):
                    model.Add(x[profile.staff_id, day.day, mark] == 0)


def _add_max_consecutive_work(model, x, profiles, days, by_staff_request) -> None:
    """連続勤務を5日までにする。

    6日ぶんのどの並びを見ても、必ず1日は休みが入るようにする。
    休みとは「公」と、有給・夏休・研修・健診のこと。
    夜勤明け(△)は勤務が続いているものとして数える(実物がそうなっている)。

    深夜(◉)の翌日の「公」は、休みとして数えない。
    深夜は22時から翌朝6時までの勤務で、日付をまたぐ。
    勤務表の上では翌日が「公」でも、その日の朝まで職場にいるので
    休んだ日ではない。施設の担当者から次の例で説明を受けた:

        ◉ 公 ◉ 公 日⑦ 日⑨ ○ △

    表の上では休みが2回あるように見えるが、実際は8日続けて職場にいる。
    これを休みと数えると、職員から苦情が出る。

    月の1日目については、前日が今月の中に無いのでこの月のデータだけでは
    判定できない。前月末が深夜(◉)だった人は by_staff_request の carry_over で
    分かるので、その人だけは1日目の「公」も休みに数えない(_add_carry_over が
    この日を公に固定しているので、常に非休日として扱ってよい)。
    """
    rest_marks = (OFF,) + ABSENCE_MARKS
    window = MAX_CONSECUTIVE_WORK_DAYS + 1
    day_numbers = [day.day for day in days]
    first_day = day_numbers[0] if day_numbers else None

    for profile in profiles:
        if _has_own_schedule(profile):
            continue
        request = by_staff_request.get(profile.staff_id) or _NO_REQUEST
        carried_late_night_day = (
            first_day if request.carry_over == LATE_NIGHT_IN else None
        )
        for start in range(len(day_numbers) - window + 1):
            chunk = day_numbers[start : start + window]
            terms = []
            for day in chunk:
                for mark in rest_marks:
                    if mark != OFF:
                        terms.append(x[profile.staff_id, day, mark])
                        continue
                    if day == carried_late_night_day:
                        # 前月末が深夜だった人の1日目。前日はこの月に無いが、
                        # 前月末の深夜から続く「公」だと分かっているので、
                        # 項を足さず休みには数えない。
                        continue
                    if _may_follow_late_night(day, day_numbers):
                        # 前日が深夜なら、この「公」は朝まで働いた日なので数えない。
                        # 「公 かつ 前日が深夜でない」ときだけ休みとみなす。
                        rest = model.NewBoolVar(f"rest_{profile.staff_id}_{day}")
                        model.Add(rest <= x[profile.staff_id, day, OFF])
                        model.Add(
                            rest <= 1 - x[profile.staff_id, day - 1, LATE_NIGHT_IN]
                        )
                        terms.append(rest)
                    else:
                        terms.append(x[profile.staff_id, day, OFF])
            model.Add(sum(terms) >= 1)


def _may_follow_late_night(day: int, day_numbers) -> bool:
    """その日の前日が、この月の中にあるか。月初は前日が無いので判定できない。"""
    return day - 1 in day_numbers


def _off_days_targets(profiles) -> List[StaffProfile]:
    """公休の日数をそろえる対象の方。

    そろえるのは正職員だけ。パートの方に公休9日の方はいない(確認済み)。
    一律に縛ると、週に2〜3日しか出ない方の勤務表が成り立たなくなる。

    「希望した日のみ勤務」の方も、正職員であっても対象から外す。
    希望を出していない日がすべて公休になるので、日数をそろえる話と
    かみ合わない。今のところ該当する正職員はいないが、あとから
    設定できてしまう。
    """
    return [
        profile
        for profile in profiles
        if not _has_own_schedule(profile)
        and not profile.works_only_on_request
        and _is_full_time(profile)
    ]


def _add_monthly_off_days(model, x, profiles, days, off_days: int) -> List:
    """正職員の公休を、その月に決められた日数に近づける。ペナルティを返す。

    「通常は9日。特例で8日や10日の月もある」と伺っており、
    何日にするかは月ごとに勤務表を作る画面で決めてもらう。

    数えるのは「公」だけ。有給・夏休・研修・健診はその日を占めるが、
    公休とは別に数える(施設の担当者に確認済み)。

    ちょうどの日数で縛ると、噛み合わない月に勤務表が1枚も出てこない。
    それより、出したうえで「〇〇さんは公休8日です」と知らせる方がよい、
    と施設の担当者と決めた。ずれた日数ぶんだけペナルティを付ける。
    """
    if not off_days:
        return []

    penalties = []
    for profile in _off_days_targets(profiles):
        actual = sum(x[profile.staff_id, day.day, OFF] for day in days)
        gap = model.NewIntVar(0, len(days), f"off_gap_{profile.staff_id}")
        model.Add(gap >= actual - off_days)
        model.Add(gap >= off_days - actual)
        penalties.append(WEIGHT_OFF_DAYS * gap)
    return penalties


def _is_full_time(profile: StaffProfile) -> bool:
    """正職員か。雇用形態の欄が「正社員」で始まる方。"""
    return "正社員" in (profile.employment_type or "")


def _add_monthly_off_quota(model, x, profiles, days) -> None:
    """「毎月2回は金曜日休み」のような、月内の回数指定。"""
    for profile in profiles:
        for weekday, count in profile.monthly_off_quota.items():
            matching = [day for day in days if day.weekday == weekday]
            if not matching:
                continue
            model.Add(
                sum(x[profile.staff_id, day.day, OFF] for day in matching)
                >= min(count, len(matching))
            )


def _add_pair_constraints(model, x, profiles, days) -> List:
    """同席(同じ夜に2人とも夜間の勤務)の制約。絶対厳守。

    相手を特定した同席制約(no_pair_night/max_shared_night)は model.Add で
    絶対厳守として組み込む。「同じナースが複数回同席しないように」という
    相手を特定しない要望は _spread_night_partners に任せ、そちらのペナルティ項
    (ソフト制約)を戻り値に含めて返す。
    """
    by_name = {profile.name: profile for profile in profiles}
    penalties: List = []
    # 同じ(profile, other, day)の組について同席変数を2度作らないよう、
    # ペア単位(順序を問わない)でキャッシュを共有する。
    pair_cache: Dict[frozenset, List] = {}

    for profile in profiles:
        for constraint in profile.pair_constraints:
            other = by_name.get(constraint.other_staff)
            if other is None:
                continue  # 名簿にいない相手。確認事項として別途出している

            together = _together_vars(model, x, profile, other, days, pair_cache)

            if constraint.kind == "no_pair_night":
                for both in together:
                    model.Add(both == 0)
            elif constraint.max_count is not None:
                model.Add(sum(together) <= constraint.max_count)

    penalties += _spread_night_partners(model, x, profiles, days, pair_cache)
    return penalties


def _together_vars(model, x, profile, other, days, cache: Dict[frozenset, List] = None) -> List:
    """2人が同じ夜に「同席」しているかの 0/1 変数を、日ごとに作る。

    同じ夜に2人とも夜間の勤務に入っていれば同席。
    ○が毎晩2人いるので、○と○の組み合わせも同席になる。
    施設の担当者も「夜勤(深夜含む)」と言っており、○◉の別は問わない。

    cache を渡すと、同じペア(順序は問わない)については一度作った変数を
    再利用する。呼び出し元をまたいで同じ(profile, other)の組が来ても、
    同名のBoolVarを重複生成しないようにするため。
    """
    key = frozenset((profile.staff_id, other.staff_id))
    if cache is not None and key in cache:
        return cache[key]

    together = []
    for day in days:
        both = model.NewBoolVar(f"pair_{profile.staff_id}_{other.staff_id}_{day.day}")
        a_on = (
            x[profile.staff_id, day.day, NIGHT_IN]
            + x[profile.staff_id, day.day, LATE_NIGHT_IN]
        )
        b_on = (
            x[other.staff_id, day.day, NIGHT_IN]
            + x[other.staff_id, day.day, LATE_NIGHT_IN]
        )
        model.Add(both >= a_on + b_on - 1)
        model.Add(both <= a_on)
        model.Add(both <= b_on)
        together.append(both)

    if cache is not None:
        cache[key] = together
    return together


def _spread_night_partners(model, x, profiles, days, pair_cache: Dict[frozenset, List] = None) -> List:
    """夜間に組む相手が特定の人に偏らないようにする。

    「夜勤は同じナースが複数回同席しないようにして下さい」への対応。
    同じ相手と2回目以降に組むたびにペナルティを付ける。
    禁止ではないので、他に組みようが無ければ2回目も入る。
    """
    penalties: List = []
    targets = [p for p in profiles if p.spread_night_partners]
    if not targets:
        return penalties

    if pair_cache is None:
        pair_cache = {}
    # A→B、B→A の両方向で同じペアに二重にペナルティを付けないよう、
    # 既にペナルティを付けたペアを記録する。
    penalized_pairs: set = set()

    for profile in targets:
        for other in profiles:
            if other.staff_id == profile.staff_id:
                continue
            if not (other.can_night or other.can_late_night):
                continue
            pair_key = frozenset((profile.staff_id, other.staff_id))
            if pair_key in penalized_pairs:
                continue
            penalized_pairs.add(pair_key)

            together = _together_vars(model, x, profile, other, days, pair_cache)
            # 2回目以降の回数 = max(0, 同席回数 - 1)
            excess = model.NewIntVar(
                0, len(days), f"repeat_{profile.staff_id}_{other.staff_id}"
            )
            model.Add(excess >= sum(together) - 1)
            penalties.append(WEIGHT_PARTNER_REPEAT * excess)
    return penalties


def _add_responsible(model, x, profiles, days, result) -> Dict[Tuple[str, int], object]:
    """責任者「せ」を決める。

    実物の勤務表で確かめたルール:
      - 師長が出勤している日は「せ」を付けない(師長が担うため)。31日中、例外なし
      - 師長が休みの日は、日責ができる人を1人「せ」にする
      - 「せ」になれるのは、その日に出勤している人だけ
    """
    se: Dict[Tuple[str, int], object] = {}
    head = next((p for p in profiles if p.is_head_nurse), None)
    if head is None:
        result.messages.append(
            "師長が設定されていないため、責任者「せ」は決めていません。"
            "確認用Excelの「師長」列で指定してください。"
        )
        return se

    # 日責ができる人。「可」を優先し、いなければ「条件付き可」も使う。
    able = [p for p in profiles if p.day_responsible in ("可", "条件付き可")]
    if not able:
        result.messages.append("日責ができる人がいないため、責任者「せ」は決めていません。")
        return se

    for profile in able:
        for day in days:
            se[profile.staff_id, day.day] = model.NewBoolVar(
                f"se_{profile.staff_id}_{day.day}"
            )

    for day in days:
        # 師長が出勤しているか(公休でなければ出勤とみなす)
        head_working = model.NewBoolVar(f"head_working_{day.day}")
        model.Add(head_working == 1 - x[head.staff_id, day.day, OFF])

        todays = [se[p.staff_id, day.day] for p in able]
        # 師長が休みの日はちょうど1人、出勤の日は0人
        model.Add(sum(todays) == 1 - head_working)

        for profile in able:
            # 休みの人は責任者になれない
            model.Add(se[profile.staff_id, day.day] <= 1 - x[profile.staff_id, day.day, OFF])
    return se


# --- ソフト制約 ----------------------------------------------------------------


def _soft_constraints(model, x, profiles, days, by_staff_request) -> List:
    penalties: List = []
    for profile in profiles:
        staff = profile.staff_id
        request = by_staff_request.get(staff)

        # 希望出勤に応える
        if request:
            for day_number, mark in request.wish_work_days().items():
                if mark not in ALL_MARKS:
                    continue
                if (staff, day_number, mark) not in x:
                    continue
                missed = model.NewBoolVar(f"missed_wish_{staff}_{day_number}")
                model.Add(missed == 1 - x[staff, day_number, mark])
                penalties.append(WEIGHT_WISH_WORK * missed)

        # 夜勤・深夜の希望回数
        for count_range, mark in (
            (profile.night_shift_count, NIGHT_IN),
            (profile.late_night_shift_count, LATE_NIGHT_IN),
        ):
            if not count_range:
                continue
            low, high = count_range
            total = sum(x[staff, day.day, mark] for day in days)
            penalties.extend(
                _range_penalty(model, total, low, high, WEIGHT_NIGHT_COUNT, f"{staff}_{mark}")
            )

        # 週の勤務日数の目安(月あたりに引き伸ばす)
        if profile.weekly_work_days:
            target = round(profile.weekly_work_days * len(days) / 7)
            worked = sum(
                x[staff, day.day, mark]
                for day in days
                for mark in ALL_MARKS
                if mark not in (OFF, NIGHT_AFTER) and mark not in ABSENCE_MARKS
            )
            penalties.extend(
                _range_penalty(model, worked, target, target, WEIGHT_WORK_DAYS, f"{staff}_days")
            )

        # 土日どちらかは休みたい
        if profile.weekend_off_either:
            penalties.extend(_weekend_penalty(model, x, staff, days))

        # 早出には入れていない
        if profile.avoid_early:
            for day in days:
                for code in EARLY_SHIFTS:
                    penalties.append(WEIGHT_AVOID_EARLY * x[staff, day.day, code])

        # 夜勤明けの翌日にまた夜勤、という形をできるだけ避ける
        day_numbers = [day.day for day in days]
        for day in day_numbers:
            if day + 2 > day_numbers[-1]:
                continue
            back_to_back = model.NewBoolVar(f"night_chain_{staff}_{day}")
            model.Add(
                back_to_back
                >= x[staff, day, NIGHT_IN] + x[staff, day + 2, NIGHT_IN] - 1
            )
            penalties.append(WEIGHT_NIGHT_AFTER_NIGHT * back_to_back)

        # 他の人で埋まらないときだけ使う枠(師長の早出・遅出など)
        for code in profile.last_resort_day_shifts:
            if code not in profile.available_day_shifts:
                continue
            for day in days:
                penalties.append(WEIGHT_LAST_RESORT * x[staff, day.day, code])

    return penalties


def _range_penalty(model, total, low: int, high: int, weight: int, name: str) -> List:
    """total が [low, high] から外れた分だけペナルティを付ける。"""
    under = model.NewIntVar(0, 1000, f"under_{name}")
    over = model.NewIntVar(0, 1000, f"over_{name}")
    model.Add(under >= low - total)
    model.Add(under >= 0)
    model.Add(over >= total - high)
    model.Add(over >= 0)
    return [weight * under, weight * over]


def _weekend_penalty(model, x, staff: str, days) -> List:
    """土日が両方とも勤務になっている週にペナルティ。"""
    penalties = []
    saturdays = [day for day in days if day.weekday == 5]
    for saturday in saturdays:
        sunday = next((d for d in days if d.day == saturday.day + 1 and d.weekday == 6), None)
        if sunday is None:
            continue
        both_worked = model.NewBoolVar(f"weekend_{staff}_{saturday.day}")
        model.Add(
            both_worked
            >= 1 - x[staff, saturday.day, OFF] + 1 - x[staff, sunday.day, OFF] - 1
        )
        penalties.append(WEIGHT_WEEKEND_OFF * both_worked)
    return penalties


def _diagnose_shortage(profiles, days, by_staff_request) -> List[str]:
    """組めなかったときに、どの日が足りないのかを日付で示す。

    「組めません」だけでは何を直せばよいか分からない。
    希望休を入れすぎた日を見つけられるよう、日ごとの人手を数えて報告する。
    ここでは連続勤務や夜勤の並びは見ていないので、ここに出ない日が
    原因のこともある。あくまで当たりを付けるためのもの。
    """
    notes: List[str] = []
    needed = sum(DAILY_REQUIREMENT.values())  # 日勤帯6 + ○2 + ◉1

    short_days: List[str] = []
    no_nurse: List[int] = []
    no_late: List[int] = []
    for day in days:
        available = []
        for profile in profiles:
            request = by_staff_request.get(profile.staff_id)
            if request and (
                day.day in request.wish_off_days() or day.day in request.absence_days()
            ):
                continue
            # その日固有の制限(曜日限定・日勤専従など)を反映した記号だけを見る。
            # 常時フラグ(can_night等)だけで判定すると、曜日限定の夜勤者しか
            # いない日でも「入れる人がいる」と誤診断してしまう。
            entry = allowed_entry_marks(profile, day)
            if entry:
                available.append((profile, entry))

        if len(available) < needed:
            short_days.append(f"{day.day}日({len(available)}人/{needed}人)")
        if not any(p.is_nurse and NIGHT_IN in entry for p, entry in available):
            no_nurse.append(day.day)
        if not any(
            LATE_NIGHT_IN in entry and (not p.is_nurse or p.can_late_night_as_nurse)
            for p, entry in available
        ):
            no_late.append(day.day)

    if short_days:
        notes.append(
            f"{ALERT_MARK} 人手が足りない日: {'、'.join(short_days)}"
            "(毎日この人数が要ります)。この日の希望休を減らすと組めるようになります"
        )
    if no_nurse:
        notes.append(
            f"{ALERT_MARK} ○夜勤に入れる看護職がいない日: "
            f"{'、'.join(f'{d}日' for d in no_nurse)}"
        )
    if no_late:
        notes.append(
            f"{ALERT_MARK} ◉深夜に入れる人がいない日: "
            f"{'、'.join(f'{d}日' for d in no_late)}"
        )
    if not notes:
        notes.append(
            "日ごとの人数だけを見ると足りています。"
            "連続勤務の上限や夜勤の並び、同席の条件が重なって組めないようです。"
            "希望休を少し減らすか、作成をお願いしている方にご相談ください"
        )
    return notes


def _report_unusable_wishes(
    all_profiles, solver_profiles, days, result: "ScheduleResult", by_staff_request
) -> None:
    """反映できない希望出勤を、黙って落とさずに知らせる。

    次の2つがある。どちらも記入した本人には分からないので必ず報告する。
      - 番号のシフトで組む人に、時短の時間が選ばれた
      - 時間が決まっている人に、その人が入れない記号が選ばれた
    """
    solver_ids = {p.staff_id for p in solver_profiles}
    day_numbers = {day.day for day in days}

    for profile in all_profiles:
        request = by_staff_request.get(profile.staff_id)
        if not request or profile.is_on_leave(result.year, result.month):
            continue
        wishes = {
            day: mark
            for day, mark in request.wish_work_days().items()
            if day in day_numbers
        }
        if not wishes:
            continue

        if profile.staff_id in solver_ids:
            unusable = {d: m for d, m in wishes.items() if m in HOUR_CHOICES}
            advice = "番号のシフトか「日」を選んでください"
            why = "この方は番号のシフトで組む方なので"
        else:
            # 時間をセルに直接書く人。実際に入った内容と突き合わせる。
            assignment = result.assignments.get(profile.staff_id, {})
            unusable = {d: m for d, m in wishes.items() if assignment.get(d) != m}
            advice = "「日」か時短の時間を選んでください"
            why = "この方は勤務時間が決まっている方なので"

        if unusable:
            detail = "、".join(f"{d}日「{m}」" for d, m in sorted(unusable.items()))
            result.messages.append(
                f"{ALERT_MARK} {profile.name}: {detail} は、{why}そのままは反映できません。"
                f"{advice}"
            )


def _report_night_sequence_conflicts(
    all_profiles, days, result: "ScheduleResult", by_staff_request
) -> None:
    """○(夜勤)・◉(深夜)の翌日に、続きと矛盾する希望が残っていないか確認する。

    ○の翌日は必ず△、◉の翌日は必ず公休になる(_add_night_sequences のハード制約)。
    そこに手入力で別の希望が残っていると、scheduler はハード制約を優先し、
    ○/◉ の希望そのものをソフト制約(missed のペナルティ)として黙って落としてしまう。
    記入した本人には分からないので、ここで必ず報告する。
    """
    day_numbers = {day.day for day in days}
    # 記号 -> (翌日に入っているべき記号, 呼び方, 続き方の説明)
    followups = {
        NIGHT_IN: (NIGHT_AFTER, "夜勤", "○→△→公 と3日"),
        LATE_NIGHT_IN: (OFF, "深夜", "◉→公 と2日"),
    }

    for profile in all_profiles:
        request = by_staff_request.get(profile.staff_id)
        if not request or profile.is_on_leave(result.year, result.month):
            continue
        for day, mark in sorted(request.entries.items()):
            if day < 1 or day not in day_numbers or mark not in followups:
                continue
            expected, label, sequence = followups[mark]
            next_day = day + 1
            if next_day not in day_numbers:
                continue  # 月をまたぐ場合はここでは扱わない(前月末欄の役目)
            next_mark = request.entries.get(next_day)
            if next_mark and next_mark != expected:
                result.messages.append(
                    f"{ALERT_MARK} {profile.name}: {day}日の{label}希望は、"
                    f"翌{next_day}日に「{next_mark}」が入っているため反映できません。"
                    f"{label}は {sequence}続くので、{next_day}日の希望を外してください"
                )


# --- 入れる勤務が1つも無い人 ------------------------------------------------------


def _move_out_unworkable(
    profiles: List[StaffProfile],
    days: Sequence[Day],
    result: "ScheduleResult",
    by_staff_request,
) -> List[StaffProfile]:
    """その月に入れる勤務が1つも無い人を、ソルバーから外して必ず報告する。

    そのままソルバーに入れると全部公休になるだけで、何も知らせずに
    「1か月まるごと休み」の勤務表が出てしまう。条件の書き方が矛盾していても
    気づけないので、ここで必ず messages に出す。
    """
    remaining: List[StaffProfile] = []
    for profile in profiles:
        if any(allowed_entry_marks(profile, day) for day in days):
            remaining.append(profile)
            continue

        result.assignments[profile.staff_id] = _fixed_hours_assignment(
            profile, days, by_staff_request.get(profile.staff_id)
        )
        # 有給・夏休・研修・健診は「実際に勤務した日」ではないので、
        # 公休と同様に worked から除く。これらだけ入っている月を
        # 「勤務できている」と誤判定すると、公休だらけの月を見逃してしまう。
        worked = sum(
            1
            for mark in result.assignments[profile.staff_id].values()
            if mark != OFF and mark not in ABSENCE_MARKS
        )
        absence = sum(
            1
            for mark in result.assignments[profile.staff_id].values()
            if mark in ABSENCE_MARKS
        )
        if profile.work_hours and worked:
            absence_note = f"・有休など{absence}日" if absence else ""
            result.messages.append(
                f"{profile.name}: 番号のシフトに当てはまる時間帯が無いため、"
                f"勤務時間({profile.work_hours[0]}-{profile.work_hours[1]})を"
                f"そのまま入れました(勤務{worked}日{absence_note})"
            )
        elif profile.works_only_on_request:
            # 「希望した日のみ勤務」の方は、希望が無ければ全部公休が正しい姿。
            # 予定を出せる時期が不定期な方もいるので、知らせにはしない。
            result.messages.append(
                f"{profile.name}: 希望した日のみ勤務される方で、"
                "この月の希望出勤が入っていないため、すべて公休にしました"
            )
        elif profile.work_hours:
            result.messages.append(
                f"{ALERT_MARK} {profile.name}: 勤務時間"
                f"({profile.work_hours[0]}-{profile.work_hours[1]})が"
                "番号のシフトに当てはまらず、希望出勤も入っていないため、"
                "1か月すべて公休になっています。希望出勤を入れてください"
            )
        else:
            result.messages.append(
                f"{ALERT_MARK} {profile.name}: 条件からは入れる勤務が1つも無く、"
                "1か月すべて公休になっています。勤務条件を見直してください"
            )
    return remaining


# --- 時間直書きの人 --------------------------------------------------------------


def _fixed_hours_assignment(
    profile: StaffProfile, days: Sequence[Day], request: Optional[StaffRequests]
) -> Dict[int, str]:
    """勤務時間をセルに直接書く人の割り当てを、そのまま作る。

    曜日ごとに時間が決まっている人はその曜日に入れる。
    曜日は決まっておらず勤務時間だけ決まっている人(「出勤可能な日を希望します」の
    パート)は、希望出勤として挙がった日にだけ入れる。挙がっていなければ公休。
    """
    by_weekday: Dict[int, List[str]] = {}
    for slot in profile.fixed_time_slots:
        by_weekday.setdefault(slot.weekday, []).append(f"{slot.start}-{slot.end}")

    hours = f"{profile.work_hours[0]}-{profile.work_hours[1]}" if profile.work_hours else ""
    wish_off = set(request.wish_off_days()) if request else set()
    wish_work = request.wish_work_days() if request else {}
    # 有給・夏休・研修・健診。公休とは別に数えるので、公で塗りつぶしてはいけない。
    absences = request.absence_days() if request else {}

    assignment: Dict[int, str] = {}
    for day in days:
        if day.day in absences:
            assignment[day.day] = absences[day.day]
        elif day.day in wish_off or day.weekday in profile.fixed_off_weekdays:
            assignment[day.day] = OFF
        elif day.weekday in by_weekday:
            assignment[day.day] = "・".join(by_weekday[day.weekday])
        elif day.day in wish_work and wish_work[day.day] in SELF_WRITTEN_MARKS:
            # 「日」や時短は、選ばれたものをそのままセルに書く。
            # 「日勤または時短9-16時、8-15時」のように勤務の形が複数ある人は、
            # その日どれで入るかを希望として選んでもらう。
            assignment[day.day] = wish_work[day.day]
        elif hours and (
            day.day in wish_work or day.weekday in profile.fixed_work_weekdays
        ):
            assignment[day.day] = hours
        else:
            assignment[day.day] = OFF
    return assignment
