"""勤務条件の構造化データモデル。

CSVの1行(スタッフ1人分)を、勤務表生成ロジックが扱える形に変換した結果を表す。
自由記述の「固定条件」「備考」から抽出しきれなかった文は必ず unparsed_notes に残し、
needs_review を立てて人間のレビュー対象とする。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .shifts import DAY_SHIFT_CODES

# 曜日: 0=月 ... 6=日 (Python の date.weekday() と同じ並び)
WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


@dataclass
class FixedTimeSlot:
    """パート職員の「毎週火曜日は9-15時」のような固定曜日・固定時間。"""

    weekday: int  # 0=月 ... 6=日
    start: str  # "09:00"
    end: str  # "15:00"


@dataclass
class PairConstraint:
    """特定スタッフとの同席に関する制約。いずれもハード制約として扱う。"""

    other_staff: str
    kind: str  # "no_pair_night"(同席不可) | "max_shared_night"(同席N回まで)
    max_count: Optional[int] = None  # max_shared_night のときの上限回数


@dataclass
class ReviewItem:
    """人に確認してもらいたい事柄。

    message は人が読む文。code はお客様への質問を組み立てるための種別で、
    payload には質問に必要な材料(該当する原文、対象のシフトコードなど)を入れる。
    """

    message: str
    code: str = "other"
    payload: Dict[str, object] = field(default_factory=dict)
    # 位置に依存しない安定したID。同じCSVから読み直せば同じIDになる。
    # ランダムだと、お客様にシートをお渡しした後に読み取りをやり直しただけで
    # IDが変わり、回答を戻せなくなるため、内容から決める。
    item_id: str = ""

    def __post_init__(self) -> None:
        if not self.item_id:
            self.item_id = self.stable_id()

    def stable_id(self, salt: str = "") -> str:
        """種別と対象(原文や列名)から決まるID。文言を直しても変わらないようにする。"""
        target = (
            self.payload.get("sentence")
            or self.payload.get("label")
            or ""
        )
        seed = f"{self.code}|{target}|{salt}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


@dataclass
class StaffProfile:
    """スタッフ1人分の勤務条件。"""

    staff_id: str
    name: str
    role: str = ""  # 介護士 / 看護師 / 管理者 / その他
    employment_type: str = ""  # 正社員 / パート（時間給）
    sheet_label: str = ""  # 勤務表に書く記号(番号「2」「①」など)
    unit: str = ""  # 所属ユニット(config/facility.yaml の units)。師長と介護補助は空
    # 介護補助。環境整備や物品補充が仕事で、対人ケアには入らない。
    # 日勤帯の必要人数にも夜勤・深夜の輪番にも入れない、独立した枠。
    is_support_staff: bool = False
    social_insurance: str = ""
    weekly_work_days: Optional[int] = None
    # 勤務表の記号としては ○(夜勤)と◉(深夜)は別区分だが、
    # 施設の担当者が条件欄に書く「夜勤」は夜間業務のことで両方を指す。
    # そのため「夜勤不可」は can_night と can_late_night の両方が False になる。
    can_night: bool = True  # ○(夜勤入り)に入れるか
    can_late_night: bool = True  # ◉(深夜入り)に入れるか
    # 入れる日勤帯のコード。既定は全部(日①②③⑤⑦⑨)。
    available_day_shifts: List[str] = field(default_factory=lambda: list(DAY_SHIFT_CODES))
    # 曜日限定で入れない日勤帯。{曜日: [コード]}。
    # 「毎週火・水・金は8時より前の早番は不可」のように、特定の曜日にだけかかる条件。
    # ここに無い曜日は available_day_shifts のとおり。
    weekday_unavailable_shifts: Dict[int, List[str]] = field(default_factory=dict)
    qualifications: List[str] = field(default_factory=list)

    # --- 以下は「固定条件」「備考」の自由文から抽出 ---
    night_shift_count: Optional[Tuple[int, int]] = None  # 夜勤 月間 (min, max)
    late_night_shift_count: Optional[Tuple[int, int]] = None  # 深夜 月間 (min, max)
    fixed_off_weekdays: List[int] = field(default_factory=list)  # 毎週◯曜日は休み
    weekend_off_either: bool = False  # 土日どちらかは休みたい(ソフト)
    monthly_off_quota: Dict[int, int] = field(default_factory=dict)  # {4: 2} = 金曜に月2回休み
    # 番号のシフトではなく、決まった時間だけ出る人の勤務時間。
    # work_hours は全日共通(例: 9:00-17:00)、fixed_time_slots は曜日ごと。
    # どちらかが入っている人は、勤務表のセルに時間をそのまま書く。
    work_hours: Optional[Tuple[str, str]] = None
    fixed_time_slots: List[FixedTimeSlot] = field(default_factory=list)
    night_weekdays: List[int] = field(default_factory=list)  # 夜勤は◯曜日のみ
    fixed_work_weekdays: List[int] = field(default_factory=list)  # 毎週◯曜日固定で勤務
    night_shift_exclusive: bool = False  # 夜勤専従
    late_night_only: bool = False  # 深夜勤務のみ
    day_shift_only: bool = False  # 日勤のみ
    day_responsible: Optional[str] = None  # 日責 "可" / "不可" / "条件付き可"
    # 看護職だが深夜(◉)に入れる人。
    # 深夜は原則として介護職が入る。実物の2026年7月でも、看護職の深夜は
    # 1人だけで、その人が全部担っていた。誰がその人かは施設によって違うので、
    # コードに名前を書かず、ここで持つ。
    can_late_night_as_nurse: bool = False
    # 師長。この人が出勤している日は、その人が責任者を担うので「せ」は付けない。
    # 休みの日だけ、日責が「可」の人を1人「せ」にする。
    is_head_nurse: bool = False
    pair_constraints: List[PairConstraint] = field(default_factory=list)
    # 「夜勤は同じナースが複数回同席しないようにして下さい」
    # 相手を特定しない要望。この人と夜間に組む相手が特定の人に偏らないようにする。
    # 一緒に入ると負担が増えるため、同じ人に何度も当たると不満が出る、という趣旨。
    # 絶対厳守ではないのでソフト制約(ペナルティ)として扱う。
    spread_night_partners: bool = False
    random_days_allowed: bool = False  # 勤務曜日は作成側に一任してよい
    # 「希望した日のみ、勤務します」の方。
    # 希望出勤に入っていない日は勤務に入れない(公休にする)。
    # 逆に言えば、この方の勤務日は毎月の希望入力がそのまま決める。
    # 希望が1件も無い月は1か月すべて公休になるが、それが正しい姿なので
    # 「1か月すべて公休」の知らせは出さない。
    works_only_on_request: bool = False
    preferred_night: bool = False  # 夜勤・深夜を希望している(ソフト優先度up)
    avoid_early: bool = False  # 早出は本人にやらせていない(ソフト回避)
    # 「できるだけ避けたい日勤帯」。入れないわけではなく、他の人で埋まらないときだけ使う。
    # 師長の施設の担当者は基本「日」で、早出・遅出は他のスタッフを優先し、
    # どうしても難しいときだけ入る。入れる/入れないの2択では表せないため別に持つ。
    last_resort_day_shifts: List[str] = field(default_factory=list)
    time_restricted: bool = False  # 条件文の時間指定で日勤帯を絞り込み済みか(CSV早出/遅出列より優先)
    # 休職。"2026-09" のように年月で入れると、その月以降は勤務表に入れない。
    # records は消さずに残すので、復職したら空欄に戻せばよい。
    leave_from: Optional[str] = None

    # --- レビュー用 ---
    raw_conditions: str = ""  # 「固定条件」欄の原文
    raw_notes: str = ""  # 「備考」欄の原文
    unparsed_notes: List[str] = field(default_factory=list)  # 構造化できなかった文
    review_items: List[ReviewItem] = field(default_factory=list)  # 人が確認すべき事柄
    # お客様に確認した結果。質問ID -> 回答。自動では反映せず、記録だけして人が判断する。
    customer_answers: Dict[str, str] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_items)

    @property
    def review_reasons(self) -> List[str]:
        """表示用。確認してほしい理由の文だけを取り出す。"""
        return [item.message for item in self.review_items]

    @property
    def is_nurse(self) -> bool:
        """看護職か。夜勤を看護1人+介護1人で組むための判定。"""
        return "看護" in self.role

    @property
    def is_caregiver(self) -> bool:
        """介護職か。"""
        return "介護" in self.role

    def is_on_leave(self, year: int, month: int) -> bool:
        """その年月に休職しているか。leave_from は "2026-09" の形。"""
        if not self.leave_from:
            return False
        try:
            leave_year, leave_month = (int(part) for part in str(self.leave_from).split("-")[:2])
        except (ValueError, TypeError):
            return False  # 読めない値で勝手に外さない。Excel読み込み側で警告する
        return (year, month) >= (leave_year, leave_month)

    @property
    def writes_own_hours(self) -> bool:
        """番号のシフトではなく、セルに時間を直接書く人か。"""
        return bool(self.fixed_time_slots or self.work_hours)

    def shifts_allowed_on(self, weekday: int) -> List[str]:
        """その曜日に入れる日勤帯コード。曜日限定の制限を反映する。"""
        blocked = set(self.weekday_unavailable_shifts.get(weekday, []))
        return [code for code in self.available_day_shifts if code not in blocked]

    def block_shifts_on_weekday(self, weekday: int, codes) -> None:
        """その曜日だけ入れないシフトを足す。"""
        current = self.weekday_unavailable_shifts.setdefault(weekday, [])
        for code in codes:
            if code not in current:
                current.append(code)

    def restrict_day_shifts(self, allowed) -> None:
        """入れる日勤帯を allowed との共通部分に絞る。順番は定義順を保つ。"""
        allowed = set(allowed)
        self.available_day_shifts = [
            code for code in self.available_day_shifts if code in allowed
        ]

    def flag_review(self, reason: str, code: str = "other", **payload) -> None:
        if reason in self.review_reasons:
            return
        item = ReviewItem(message=reason, code=code, payload=payload)
        # 同じ種別・同じ原文の項目が並ぶとIDがぶつかるので、その時だけずらす
        existing = {other.item_id for other in self.review_items}
        salt = 0
        while item.item_id in existing:
            salt += 1
            item.item_id = item.stable_id(salt=str(salt))
        self.review_items.append(item)
