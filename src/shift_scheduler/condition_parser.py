"""「固定条件」「備考」の自由記述から、確実に判別できるパターンだけを構造化する。

方針:
- 曖昧なもの・判別できなかった文は絶対に推測で埋めない。unparsed_notes に原文を残し、
  review_reasons を立てて人間のレビュー対象にする。
- 同席ペア制約は絶対厳守のハード制約なので、抽出に成功しても必ずレビュー対象にする。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    WEEKDAY_NAMES,
    FixedTimeSlot,
    PairConstraint,
    StaffProfile,
)
from .shifts import EARLY_SHIFTS, LATE_SHIFTS, shifts_within
from .text_utils import normalize, split_sentences, to_hhmm

_WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAY_NAMES)}

# --- 回数レンジ ---------------------------------------------------------------
# 「夜勤3〜4回」「深夜2回〜4回」「夜勤3回か4回」「夜勤5回から6回」
# 区分と回数の間に挟まる言い回し。「夜勤専従で毎月2〜3回」「深夜は月に3回」など。
# ここを許さないと回数が読めず、しかも文自体は「夜勤専従」で解釈済みになるため
# 回数だけが黙って落ちる。
_COUNT_GAP = r"(?:勤務)?(?:専従)?(?:で|は|を|も)?\s*(?:毎月|月間|月に|月)?\s*"
_RANGE_RE = re.compile(
    rf"(夜勤|深夜){_COUNT_GAP}(\d+)\s*回?\s*(?:〜|-|から|か)\s*(\d+)\s*回"
)
# 「夜勤3回以下」「深夜3回まで」
_UPTO_RE = re.compile(rf"(夜勤|深夜){_COUNT_GAP}(\d+)\s*回?(?:以下|まで)")
# 「夜勤3回」「深夜3回希望」
_EXACT_RE = re.compile(rf"(夜勤|深夜){_COUNT_GAP}(\d+)\s*回")
# 「やむを得ない場合は2回可」— 直前に出た区分の上限を広げる
_EXCEPTION_RE = re.compile(r"やむを得ない場合(?:は)?\s*(\d+)\s*回")

# --- 休み --------------------------------------------------------------------
_MONTHLY_QUOTA_RE = re.compile(r"毎月\s*(\d+)\s*回")
_WEEKEND_EITHER_RE = re.compile(r"土曜日?\s*(?:か|または|and/or)\s*日曜日?.*(?:どちらか|いずれか)")

# --- 時間帯制約 ---------------------------------------------------------------
_NOT_BEFORE_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?時?より前の(早番|早出)(?:は)?不可")
_NOT_AFTER_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?時?以降の(遅番|遅出)(?:は)?不可")
# 「9時〜17時の勤務」「6時から17時迄の枠で」
_GENERAL_WINDOW_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?時\s*(?:〜|-|から)\s*(\d{1,2})(?::(\d{2}))?時"
)
# 「9-15時」「10-13時」(曜日と組で使う)
_SLOT_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(?:-|〜|から)\s*(\d{1,2})(?::(\d{2}))?\s*時")

# 「10-13」のような数値範囲。正規のパターンで拾えた数と突き合わせ、
# 「10-13月」(「時」の誤字)のような取りこぼしを検知するために使う。
_LOOSE_RANGE_RE = re.compile(r"\d{1,2}\s*(?:-|〜|から)\s*\d{1,2}")

# これらの語が入る文は、断定的な制約として扱ってよいか人の確認が要る
_AMBIGUOUS_WORDS = ("など", "または", "程度", "くらい", "なるべく", "少なめ", "希望")

# --- 同席ペア ------------------------------------------------------------------
_MAX_SHARED_RE = re.compile(r"(\d+)\s*回(?:だけ)?(?:まで|可)")
_ONE_TIME_WORDS = ("一回", "1回")

# 「スタッフEとW」のように2人目が「スタッフ」抜きで書かれるケース。
# 拾った1文字は必ず名簿と突き合わせるため、後続文字の制限は掛けない。
_STAFF_TOKEN_RE = re.compile(r"(?:と|、|や|,)\s*([A-Za-zぁ-んァ-ヶ一-龥])")

# 「基本は「日」」「基本日勤」— 普段入るシフト
_BASE_SHIFT_RE = re.compile(r"基本(?:は|的に)?\s*[「『]?(日[①②③⑤⑦⑨]?)[」』]?")

# 「他の人を優先し、どうしても難しいときのみ」— 最後の手段であることを示す言い回し
_LAST_RESORT_WORDS = (
    "他の人を優先", "他のスタッフを優先", "他の方を優先",
    "どうしても難しいとき", "どうしても難しい時", "難しいときのみ",
)

# 「夜勤不可」「深夜可能」のような区分ごとの可否
_SHIFT_KINDS = "夜勤|深夜|早出|早番|遅出|遅番|日勤"
_SHIFT_ABILITY_RE = re.compile(rf"({_SHIFT_KINDS})(?:勤務)?(?:は)?(不可|可能|可)(?![能])")
# 「夜勤・深夜は不可」のように区分を並べる書き方。まとめて同じ可否がかかる。
_SHIFT_ABILITY_LIST_RE = re.compile(
    rf"((?:{_SHIFT_KINDS})(?:\s*[・、と/]\s*(?:{_SHIFT_KINDS}))+)(?:勤務)?(?:は)?(不可|可能|可)(?![能])"
)


class ConditionParser:
    """スタッフ名簿を踏まえて自由文を解釈する。"""

    def __init__(self, roster: Sequence[str], aliases: Optional[Dict[str, str]] = None):
        """roster は表示名(苗字)の一覧。aliases は条件文で使われる別名との対応。

        条件文の中では「スタッフW」のような仮名でスタッフが指されている。
        表示名を苗字に切り替えた後もこの書き方が残っているため、
        照合は「表示名 + 別名」の両方で行い、見つかった相手は必ず表示名で返す。
        """
        lookup: Dict[str, str] = {}
        for name in roster:
            key = normalize(name)
            if key:
                lookup[key] = key
        for alias, display in (aliases or {}).items():
            key = normalize(alias)
            display_key = normalize(display)
            if key and display_key:
                lookup.setdefault(key, display_key)
        self._lookup = lookup
        # 長い名前を先に試すため長さ降順で持つ。
        self._roster = sorted(lookup, key=len, reverse=True)
        self._explicit_late_night = False

    # -- 公開API ---------------------------------------------------------------

    def apply(self, profile: StaffProfile) -> None:
        """profile.raw_conditions / raw_notes を解釈して profile を埋める。"""
        # 「夜勤3回」の次の行に「やむを得ない場合4回」が来るような、
        # 前の文を受ける書き方に対応するため直前の解釈内容を覚えておく。
        context: Dict[str, Any] = {"last_shift": None, "last_pairs": [], "base_shift": None}
        # 「深夜」と名指しで可否が書かれたか。この後に来る「夜勤」(夜間業務全体を指す)
        # でうっかり上書きしないようにする。
        self._explicit_late_night = False
        for source_label, text in (
            ("固定条件", profile.raw_conditions),
            ("備考", profile.raw_notes),
        ):
            for sentence in split_sentences(text):
                if not self._apply_sentence(profile, sentence, context):
                    profile.unparsed_notes.append(f"[{source_label}] {sentence}")
                    profile.flag_review(
                        f"{source_label}に未解釈の記述あり: 「{sentence}」",
                        code="unparsed",
                        sentence=sentence,
                    )
        self._cross_check_counts(profile)

    @staticmethod
    def _cross_check_counts(profile: StaffProfile) -> None:
        """入れない区分に回数が付いている場合の後始末。

        「深夜勤務のみ。夜勤専従で毎月2〜3回」のように、原文が「夜勤」と「深夜」を
        区別せずに書いていることがある。施設の担当者の言う「夜勤」は夜間業務全体の
        ことなので、◉しか入らない人の「夜勤N回」は◉の回数と読める。
        ただし読み替えた事実は必ず確認に回す。
        """
        # 「夜勤」は夜間業務全体を指す言葉なので、◉しか入らない人に書かれた
        # 「夜勤N回」は◉の回数のこと。そう読み替えたうえで、念のため確認に回す。
        if profile.late_night_only and profile.night_shift_count:
            low, high = profile.night_shift_count
            if profile.late_night_shift_count is None:
                profile.late_night_shift_count = profile.night_shift_count
                profile.night_shift_count = None
                profile.flag_review(
                    f"「深夜勤務のみ」の方に夜勤 {low}〜{high}回 と書かれていたので、"
                    f"深夜(◉)を月{low}〜{high}回という意味に取りました",
                    code="count_conflict",
                    label=f"{profile.name}/夜勤回数",
                )
            else:
                # 深夜の回数が既に書かれているのに、別に夜勤の回数も書かれている場合は
                # どちらが本来の希望なのか分からない。既存の深夜回数を黙って上書きせず、
                # 人が見て判断できるように両方の値をメッセージに残す。
                d_low, d_high = profile.late_night_shift_count
                profile.night_shift_count = None
                profile.flag_review(
                    f"「深夜勤務のみ」の方に深夜 {d_low}〜{d_high}回 と、"
                    f"夜勤 {low}〜{high}回 の両方が書かれています。"
                    "どちらが正しいか確認してください",
                    code="count_conflict",
                    label=f"{profile.name}/夜勤回数",
                )
        if profile.day_shift_only and (
            profile.night_shift_count or profile.late_night_shift_count
        ):
            profile.flag_review(
                "「日勤のみ」の方に夜勤・深夜の回数が書かれています",
                code="count_conflict",
                label=f"{profile.name}/日勤のみ",
            )

    # -- 1文の解釈 -------------------------------------------------------------

    def _apply_sentence(
        self, profile: StaffProfile, sentence: str, context: Dict[str, Any]
    ) -> bool:
        """1文を解釈できたかを返す。context には直前の解釈内容が入る。"""
        consumed = False

        # 同席ペアは他の解釈より優先(回数表現を巻き込ませない)
        is_pair_sentence = (
            "同席" in sentence
            or "同じナース" in sentence
            or "同じ人" in sentence
            or bool(self._find_staff_names(sentence, exclude=profile.name))
        )
        if is_pair_sentence:
            context["last_pairs"] = self._parse_pair(profile, sentence)
            context["last_shift"] = None
            # 同席文であっても、同文中に回数などのパターンが同居していれば
            # 黙って握りつぶさず原文を残す(設計原則: 情報を黙って捨てない)。
            if _RANGE_RE.search(sentence) or _UPTO_RE.search(sentence) or _EXACT_RE.search(sentence):
                profile.unparsed_notes.append(f"[同席文中の未解釈情報] {sentence}")
                profile.flag_review(
                    f"同席制約の文に回数などの情報が同居しているため未解釈です。手で確認してください: 「{sentence}」",
                    code="unparsed",
                    sentence=sentence,
                )
            return True

        # 「(やむを得ない場合は一回だけ可)」が同席制約の次の行に来るケース
        is_exception = "やむを得ない" in sentence
        if is_exception and context["last_pairs"]:
            limit = _shared_limit(sentence) or 1
            for pair in context["last_pairs"]:
                pair.kind = "max_shared_night"
                pair.max_count = limit
            return True

        # 「やむを得ない場合」は直前の文のみを参照する。回数解釈・ペア解釈
        # 以外の文を挟んだ場合は、context を明示的にクリアして無関係な文の
        # 巻き込みを防ぐ(context残留による回数の無警告書き換えを防止)。
        references_context = False

        matched_shift = self._parse_counts(profile, sentence)
        if matched_shift:
            consumed = True
            context["last_shift"] = matched_shift
            context["last_pairs"] = []
            references_context = True
        elif is_exception and context["last_shift"]:
            numeric = _EXCEPTION_RE.search(sentence)
            if numeric:
                self._extend_max(profile, context["last_shift"], int(numeric.group(1)))
                consumed = True
                references_context = True

        if self._parse_off_days(profile, sentence):
            consumed = True
        if self._parse_time_rules(profile, sentence):
            consumed = True
        if self._parse_shift_limits(profile, sentence):
            consumed = True
        if self._parse_ability(profile, sentence):
            consumed = True
        if self._parse_flags(profile, sentence):
            consumed = True
        if self._parse_base_shift(profile, sentence, context):
            consumed = True

        if not references_context:
            context["last_shift"] = None
            context["last_pairs"] = []

        if consumed:
            self._flag_if_lossy(profile, sentence)
        return consumed

    @staticmethod
    def _flag_if_lossy(profile: StaffProfile, sentence: str) -> None:
        """解釈できた文でも、取りこぼしや曖昧さが疑われる場合はレビューに回す。"""
        loose_ranges = len(_LOOSE_RANGE_RE.findall(sentence))
        parsed_ranges = len(_SLOT_RE.findall(sentence)) + len(_RANGE_RE.findall(sentence))
        if loose_ranges > parsed_ranges:
            profile.flag_review(
                f"数値の範囲を取りこぼしている可能性があります(誤字の疑い): 「{sentence}」",
                code="typo",
                sentence=sentence,
            )
        if any(word in sentence for word in _AMBIGUOUS_WORDS):
            profile.flag_review(
                f"断定できない表現が含まれます。意図どおりか確認してください: 「{sentence}」",
                code="ambiguous",
                sentence=sentence,
            )

    # -- 回数 -------------------------------------------------------------------

    def _parse_counts(self, profile: StaffProfile, sentence: str) -> Optional[str]:
        """夜勤/深夜の回数レンジを読む。読めた区分名("夜勤"/"深夜")を返す。"""
        found: Optional[str] = None
        for match in _RANGE_RE.finditer(sentence):
            shift, low, high = match.group(1), int(match.group(2)), int(match.group(3))
            self._set_count(profile, shift, (min(low, high), max(low, high)))
            found = shift
        if found:
            return self._apply_inline_exception(profile, sentence, found)

        for match in _UPTO_RE.finditer(sentence):
            shift, high = match.group(1), int(match.group(2))
            self._set_count(profile, shift, (0, high))
            found = shift
        if found:
            return self._apply_inline_exception(profile, sentence, found)

        for match in _EXACT_RE.finditer(sentence):
            shift, count = match.group(1), int(match.group(2))
            self._set_count(profile, shift, (count, count))
            found = shift
        if found:
            return self._apply_inline_exception(profile, sentence, found)
        return None

    def _apply_inline_exception(
        self, profile: StaffProfile, sentence: str, shift: str
    ) -> str:
        """同じ文中の「（やむを得ない場合は2回可）」を上限に反映する。"""
        exception = _EXCEPTION_RE.search(sentence)
        if exception:
            self._extend_max(profile, shift, int(exception.group(1)))
        return shift

    @staticmethod
    def _set_count(profile: StaffProfile, shift: str, value: Tuple[int, int]) -> None:
        if shift == "夜勤":
            profile.night_shift_count = value
        else:
            profile.late_night_shift_count = value

    @staticmethod
    def _extend_max(profile: StaffProfile, shift: str, new_max: int) -> None:
        attr = "night_shift_count" if shift == "夜勤" else "late_night_shift_count"
        current = getattr(profile, attr)
        if current is None:
            setattr(profile, attr, (0, new_max))
        else:
            setattr(profile, attr, (current[0], max(current[1], new_max)))

    # -- 休み -------------------------------------------------------------------

    def _parse_off_days(self, profile: StaffProfile, sentence: str) -> bool:
        if "休" not in sentence:
            return False

        if _WEEKEND_EITHER_RE.search(sentence):
            profile.weekend_off_either = True
            return True

        weekdays = _extract_weekdays(sentence)
        if not weekdays:
            return False

        quota = _MONTHLY_QUOTA_RE.search(sentence)
        if quota:
            count = int(quota.group(1))
            for weekday in weekdays:
                profile.monthly_off_quota[weekday] = count
            return True

        if "毎週" in sentence or "毎" in sentence:
            for weekday in weekdays:
                if weekday not in profile.fixed_off_weekdays:
                    profile.fixed_off_weekdays.append(weekday)
            profile.fixed_off_weekdays.sort()
            return True
        return False

    # -- 時間帯 -----------------------------------------------------------------

    @staticmethod
    def _apply_time_limit(profile: StaffProfile, allowed, limited_to) -> None:
        """時間の条件を反映する。曜日の指定があればその曜日にだけかける。"""
        # 曜日限定であっても、時間の指定を読み取った以上はCSVの早出/遅出列より
        # 条件文を優先させる必要がある(models.time_restricted のコメント参照)。
        # 立てないと、他の曜日にCSV列の「不可」がそのまま効いてしまう。
        profile.time_restricted = True
        if limited_to:
            blocked = [c for c in profile.available_day_shifts if c not in set(allowed)]
            for weekday in limited_to:
                profile.block_shifts_on_weekday(weekday, blocked)
        else:
            profile.restrict_day_shifts(allowed)

    def _parse_time_rules(self, profile: StaffProfile, sentence: str) -> bool:
        matched = False

        # 曜日が書かれていれば、その曜日にだけかける。
        # 「毎週火・水・金は8時より前の早番は不可」は、他の曜日には効かない。
        limited_to = _extract_weekdays(sentence)

        # 時間が書かれている条件は、その時間に合う日勤帯コードだけに絞り込む。
        # 例: 「7時より前の早番不可」→ 6時開始の日①を外す。
        for match in _NOT_BEFORE_RE.finditer(sentence):
            limit = to_hhmm(match.group(1), match.group(2) or "")
            self._apply_time_limit(profile, shifts_within(not_before=limit), limited_to)
            matched = True

        for match in _NOT_AFTER_RE.finditer(sentence):
            limit = to_hhmm(match.group(1), match.group(2) or "")
            self._apply_time_limit(profile, shifts_within(not_after=limit), limited_to)
            matched = True

        if matched:
            if limited_to:
                names = "・".join(WEEKDAY_NAMES[w] for w in limited_to)
                profile.flag_review(
                    f"{names}曜だけにかかる条件として取り込みました。"
                    f"他の曜日は制限していません: 「{sentence}」",
                    code="weekday_time",
                    sentence=sentence,
                )
            return True

        weekdays = _extract_weekdays(sentence)
        slots = list(_SLOT_RE.finditer(sentence)) or list(_GENERAL_WINDOW_RE.finditer(sentence))
        if not slots:
            return False

        if weekdays:
            for weekday in weekdays:
                for slot in slots:
                    profile.fixed_time_slots.append(
                        FixedTimeSlot(
                            weekday=weekday,
                            start=to_hhmm(slot.group(1), slot.group(2) or ""),
                            end=to_hhmm(slot.group(3), slot.group(4) or ""),
                        )
                    )
            return True

        # 曜日が無い「9時〜17時の勤務」は全日共通の勤務時間として扱う。
        # あわせて、その枠に収まる日勤帯だけに絞る。
        slot = slots[0]
        start = to_hhmm(slot.group(1), slot.group(2) or "")
        end = to_hhmm(slot.group(3), slot.group(4) or "")
        profile.work_hours = (start, end)
        profile.restrict_day_shifts(shifts_within(not_before=start, not_after=end))
        profile.time_restricted = True
        return True

    # -- 勤務形態の限定 ----------------------------------------------------------

    def _parse_shift_limits(self, profile: StaffProfile, sentence: str) -> bool:
        matched = False
        if "夜勤専従" in sentence:
            profile.night_shift_exclusive = True
            matched = True
        if "深夜勤務のみ" in sentence or "深夜のみ" in sentence:
            profile.late_night_only = True
            matched = True
        if "日勤のみ" in sentence:
            profile.day_shift_only = True
            matched = True

        # 「夜勤は金曜日」— 夜勤に入る曜日の限定
        if "夜勤" in sentence and "不可" not in sentence and "回" not in sentence:
            weekdays = _extract_weekdays(sentence)
            if weekdays:
                profile.night_weekdays = sorted(set(profile.night_weekdays) | set(weekdays))
                matched = True

        # 「毎週火曜日固定」— 勤務曜日の固定
        if "固定" in sentence:
            weekdays = _extract_weekdays(sentence)
            if weekdays:
                profile.fixed_work_weekdays = sorted(
                    set(profile.fixed_work_weekdays) | set(weekdays)
                )
                matched = True
        return matched

    # -- 基本のシフトと「最後の手段」 ----------------------------------------------

    @staticmethod
    def _parse_base_shift(profile: StaffProfile, sentence: str, context) -> bool:
        """「基本は『日』」「早出・遅出は他の人を優先し、どうしても難しいときのみ」を読む。

        普段入るシフトを決め、それ以外は「他で埋まらないときだけ使う枠」にする。
        入れないのではなく後回し、という3つ目の状態。
        """
        base = _BASE_SHIFT_RE.search(sentence)
        if base:
            context["base_shift"] = base.group(1)
            return True

        if not any(word in sentence for word in _LAST_RESORT_WORDS):
            return False

        primary = context.get("base_shift")
        if not primary:
            # 「基本は◯」が無いと何を優先するか決まらない。推測せず確認に回す。
            profile.flag_review(
                "「他の人を優先」とありますが、普段どの勤務に入るのかが書かれていません: "
                f"「{sentence}」",
                code="last_resort_unknown",
                sentence=sentence,
            )
            return True

        profile.last_resort_day_shifts = [
            code for code in profile.available_day_shifts if code != primary
        ]
        profile.flag_review(
            f"「{primary}」を普段の勤務とし、それ以外の日勤帯は"
            "「他の人で埋まらないときだけ入る」扱いにしました: "
            f"「{sentence}」",
            code="last_resort",
            sentence=sentence,
            primary=primary,
        )
        return True

    # -- 区分ごとの可否 ----------------------------------------------------------

    def _parse_ability(self, profile: StaffProfile, sentence: str) -> bool:
        """「夜勤不可」「深夜可能」のような、区分ごとの可否を反映する。"""
        matched = False
        # 「7時より前の早番不可」のように時間が書かれている文は _parse_time_rules が
        # 正確に処理済み。ここで「早番不可」だけを拾って範囲を広げてしまわないよう外す。
        has_time = bool(_NOT_BEFORE_RE.search(sentence) or _NOT_AFTER_RE.search(sentence))

        # 「夜勤・深夜は不可」のように並べて書かれた場合も、全部に同じ可否をかける
        pairs = []
        for match in _SHIFT_ABILITY_LIST_RE.finditer(sentence):
            for shift in re.split(r"[・、と/]\s*", match.group(1)):
                pairs.append((shift.strip(), match.group(2)))
        for match in _SHIFT_ABILITY_RE.finditer(sentence):
            pairs.append((match.group(1), match.group(2)))

        for shift, verdict in pairs:
            allowed = verdict != "不可"
            if has_time and shift in ("早出", "早番", "遅出", "遅番"):
                matched = True
                continue
            if shift == "夜勤":
                # 施設の担当者の言う「夜勤」は夜間業務のことで、
                # ○(夜勤)と◉(深夜)の両方を指す(施設の担当者に確認済み)。
                # 「夜勤不可」なら夜間はどちらも入らない。
                profile.can_night = allowed
                if not self._explicit_late_night:
                    profile.can_late_night = allowed
            elif shift == "深夜":
                # 「深夜」は◉だけを名指ししている。こちらが優先。
                profile.can_late_night = allowed
                self._explicit_late_night = True
            elif shift in ("早出", "早番") and not allowed:
                # 時間の指定がない「早番不可」は、早い方の日勤帯を外す
                profile.restrict_day_shifts(
                    [c for c in profile.available_day_shifts if c not in EARLY_SHIFTS]
                )
                profile.flag_review(
                    f"「{shift}不可」を{'・'.join(EARLY_SHIFTS)}を外す意味に取りました: 「{sentence}」",
                    code="shift_word",
                    sentence=sentence,
                    removed=list(EARLY_SHIFTS),
                )
            elif shift in ("遅出", "遅番") and not allowed:
                profile.restrict_day_shifts(
                    [c for c in profile.available_day_shifts if c not in LATE_SHIFTS]
                )
                profile.flag_review(
                    f"「{shift}不可」を{'・'.join(LATE_SHIFTS)}を外す意味に取りました: 「{sentence}」",
                    code="shift_word",
                    sentence=sentence,
                    removed=list(LATE_SHIFTS),
                )
            matched = True
        return matched

    # -- 単純フラグ --------------------------------------------------------------

    def _parse_flags(self, profile: StaffProfile, sentence: str) -> bool:
        matched = False
        if "日責" in sentence:
            if "不可" in sentence:
                profile.day_responsible = "不可"
            elif "どうしても" in sentence or "やむを得ない" in sentence:
                profile.day_responsible = "条件付き可"
            else:
                profile.day_responsible = "可"
            matched = True
        if "ランダム" in sentence:
            profile.random_days_allowed = True
            matched = True
        if "希望してくる" in sentence or "希望します" in sentence:
            if "夜勤" in sentence or "深夜" in sentence:
                profile.preferred_night = True
            matched = True
        if "早出" in sentence and ("やらせていない" in sentence or "やらせない" in sentence):
            profile.avoid_early = True
            matched = True
        return matched

    # -- 同席ペア ----------------------------------------------------------------

    def _parse_pair(self, profile: StaffProfile, sentence: str) -> List[PairConstraint]:
        """同席制約を抽出する。抽出できてもできなくても必ずレビュー対象にする。"""
        # 「同じナースが複数回同席しないように」は相手を特定しない要望。
        # 特定の相手との制約ではないので、別のフラグとして扱う。
        if "同じナース" in sentence or "同じ人" in sentence:
            profile.spread_night_partners = True
            profile.flag_review(
                "夜間に組む相手が特定の人に偏らないようにする、という要望として"
                f"扱いました(絶対厳守ではなく、できるだけ散らす): 「{sentence}」",
                code="spread_partners",
                label=f"{profile.name}/同席の散らし",
            )
            return []

        others = self._find_staff_names(sentence, exclude=profile.name)
        if not others:
            profile.unparsed_notes.append(f"[同席制約] {sentence}")
            profile.flag_review(
                f"同席制約の相手を特定できませんでした。手で設定してください: 「{sentence}」",
                code="pair_unknown",
                sentence=sentence,
            )
            return []

        limit = _shared_limit(sentence)
        created: List[PairConstraint] = []
        for other in others:
            if limit is None:
                constraint = PairConstraint(other_staff=other, kind="no_pair_night")
            else:
                constraint = PairConstraint(
                    other_staff=other, kind="max_shared_night", max_count=limit
                )
            profile.pair_constraints.append(constraint)
            created.append(constraint)
        profile.flag_review(
            "同席制約(絶対厳守)を自動抽出しました。相手と回数が正しいか必ず確認してください: "
            f"「{sentence}」→ {', '.join(others)}",
            code="pair",
            sentence=sentence,
            others=others,
            limit=limit,
        )
        return created

    def _find_staff_names(self, sentence: str, exclude: str) -> List[str]:
        """名簿と突き合わせて、文中のスタッフ名を拾う。返すのは表示名(苗字)。"""
        text = normalize(sentence)
        exclude_norm = normalize(exclude)
        found: List[str] = []

        def add(token: str) -> None:
            display = self._lookup.get(token)
            if display and display != exclude_norm and display not in found:
                found.append(display)

        for name in self._roster:
            if name not in text:
                continue
            # 自分の名前も潰しておく。残すと、その一部が他の人の名前として
            # 拾われることがある。
            if self._lookup.get(name) != exclude_norm:
                add(name)
            text = text.replace(name, "＿" * len(name))

        # 「スタッフEとW」のように2人目が接頭辞なしで書かれるケースを拾う。
        # 「ところに」「ないときは」のような普通の日本語を人名と誤認しないよう、
        # 明示的な氏名が1人以上見つかった文でのみ実行する。
        if not found:
            return found
        for match in _STAFF_TOKEN_RE.finditer(text):
            add(normalize(f"スタッフ{match.group(1)}"))
        return found


def _extract_weekdays(sentence: str) -> List[int]:
    """文中の曜日を拾う。「土日」のような略記にも対応する。"""
    found: List[int] = []
    for match in re.finditer(r"([月火水木金土日])曜", sentence):
        found.append(_WEEKDAY_INDEX[match.group(1)])
    if not found and "土日" in sentence:
        found = [_WEEKDAY_INDEX["土"], _WEEKDAY_INDEX["日"]]
    elif "土日" in sentence:
        for name in ("土", "日"):
            if _WEEKDAY_INDEX[name] not in found:
                found.append(_WEEKDAY_INDEX[name])
    return sorted(set(found))


def _shared_limit(sentence: str) -> Optional[int]:
    """「一回まで」「一回だけ可」なら上限回数、単なる同席不可なら None。"""
    if any(word in sentence for word in _ONE_TIME_WORDS):
        return 1
    match = _MAX_SHARED_RE.search(sentence)
    if match:
        return int(match.group(1))
    return None
