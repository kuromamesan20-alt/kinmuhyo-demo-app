"""お客様(施設)に確認していただくための質問を組み立てる。

要確認の事柄(ReviewItem)を、施設の方がそのまま答えられる質問文に変換する。
内部用語(日勤帯コードの一覧、YAML、CSVの列名など)は質問文に出さない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from .models import ReviewItem, StaffProfile
from .shifts import DAY_SHIFTS

YES = "はい（合っています）"
NO = "いいえ（違います）"


@dataclass
class Question:
    """お客様に1つ答えていただく単位。"""

    question_id: str  # 回答を元のスタッフ・項目に戻すための印
    staff_name: str
    original_text: str  # 施設が最初に書いた文
    text: str  # 確認したいこと
    choices: List[str] = field(default_factory=list)  # 空なら自由記入
    free_text_hint: str = ""  # 回答欄の書き方の案内


def build_questions(profiles: Sequence[StaffProfile]) -> List[Question]:
    questions: List[Question] = []
    for profile in profiles:
        for item in profile.review_items:
            question = _to_question(profile, item)
            if question:
                questions.append(question)
    return questions


def _to_question(profile: StaffProfile, item: ReviewItem) -> "Question | None":
    question_id = f"{profile.staff_id}#{item.item_id}"
    sentence = str(item.payload.get("sentence", ""))
    original = sentence or _original_text(profile)

    builder = _BUILDERS.get(item.code)
    if builder is None:
        # 種別が分からないものは、こちらの確認事項をそのまま見ていただく
        return Question(
            question_id=question_id,
            staff_name=profile.name,
            original_text=original,
            text=f"{item.message}\nこの点について教えてください。",
            free_text_hint="ご記入ください",
        )
    text, choices, hint = builder(profile, item, sentence)
    return Question(
        question_id=question_id,
        staff_name=profile.name,
        original_text=original,
        text=text,
        choices=choices,
        free_text_hint=hint,
    )


def _original_text(profile: StaffProfile) -> str:
    return "\n".join(
        part for part in (profile.raw_conditions, profile.raw_notes) if part
    )


# --- 種別ごとの質問文 -----------------------------------------------------------


def _pair(profile, item, sentence):
    others = item.payload.get("others") or []
    limit = item.payload.get("limit")
    if limit:
        detail = f"{'・'.join(others)} とは、夜勤で同席するのは月{limit}回まで"
    else:
        detail = f"{'・'.join(others)} とは、夜勤で同席させない"
    return (
        f"{profile.name}さんについて「{detail}」という理解で合っていますか。\n"
        "※このルールは必ず守る前提で勤務表を組みます。お相手のお名前もご確認ください。",
        [YES, NO],
        "違う場合は、正しいお相手のお名前と回数をご記入ください",
    )


def _pair_unknown(profile, item, sentence):
    return (
        f"{profile.name}さんの「{sentence}」について、"
        "どなたとどなたを同席させないようにすればよいか、お名前を教えてください。",
        [],
        "例: スタッフA と スタッフB は同じ夜勤に入れない",
    )


def _ambiguous(profile, item, sentence):
    return (
        f"{profile.name}さんの「{sentence}」は、"
        "必ず守るルールですか、それともできれば叶えたいご希望ですか。",
        ["必ず守る", "できれば叶えたい希望", "この記載は無視してよい"],
        "補足があればご記入ください",
    )


def _unparsed(profile, item, sentence):
    return (
        f"{profile.name}さんの「{sentence}」は、"
        "勤務表を組むうえでどう扱えばよいでしょうか。",
        [],
        "勤務表に反映すべき内容であれば、具体的に教えてください（不要なら「特になし」）",
    )


def _typo(profile, item, sentence):
    return (
        f"{profile.name}さんの「{sentence}」について、"
        "正しい時間・回数を教えてください。（書き間違いの可能性があります）",
        [],
        "正しい内容をご記入ください",
    )


def _weekday_time(profile, item, sentence):
    return (
        f"{profile.name}さんの「{sentence}」は、"
        "その曜日だけの制限ですか、それとも毎日そうですか。",
        ["その曜日だけ", "毎日そう"],
        "補足があればご記入ください",
    )


def _shift_word(profile, item, sentence):
    removed = item.payload.get("removed") or []
    return (
        f"{profile.name}さんの「{sentence}」について、"
        f"{_shift_list(removed)} には入れない、という理解で合っていますか。",
        [YES, NO],
        "違う場合は、入れない時間帯を教えてください",
    )


def _early_late_column(profile, item, sentence):
    label = item.payload.get("label", "")
    removed = item.payload.get("removed") or []
    # お客様が答えたのは「{label}: 不可」という選択肢だけ。
    # それがどのシフトを指すかはこちらの解釈なので、事実と解釈を分けて書く。
    return (
        f"{profile.name}さんについて、いただいた回答では「{label}」が不可となっていました。"
        f"これは {_shift_list(removed)} には入れない、という意味で合っていますか。",
        [YES, NO],
        "違う場合は、入れる時間帯・入れない時間帯を教えてください",
    )


def _night_mismatch(profile, item, sentence):
    return (
        f"{profile.name}さんは夜勤・深夜に入れますか。"
        "（いただいた回答の中で、入れないという欄と回数の記載が食い違っています）",
        ["夜勤も深夜も入れる", "夜勤だけ入れる", "深夜だけ入れる", "どちらも入れない"],
        "月に何回くらいかもご記入ください",
    )


def _exclusive_conflict(profile, item, sentence):
    candidates = item.payload.get("candidates") or []
    return (
        f"{profile.name}さんの働き方は、次のどれにあたりますか。",
        list(candidates) + ["どれでもない"],
        "補足があればご記入ください",
    )


def _weekly_days(profile, item, sentence):
    return (
        f"{profile.name}さんは、週に何日勤務されますか。",
        [],
        "例: 5",
    )


def _no_shift_left(profile, item, sentence):
    return (
        f"{profile.name}さんが入れる時間帯が1つもなくなってしまいました。"
        "実際に入れる時間帯を教えてください。",
        [],
        f"日勤帯は {_shift_list(list(DAY_SHIFTS))} です",
    )


def _shift_list(codes: Sequence[str]) -> str:
    return "・".join(
        f"{code}({DAY_SHIFTS[code][0]}-{DAY_SHIFTS[code][1]})"
        for code in codes
        if code in DAY_SHIFTS
    )


_BUILDERS = {
    "pair": _pair,
    "pair_unknown": _pair_unknown,
    "ambiguous": _ambiguous,
    "unparsed": _unparsed,
    "typo": _typo,
    "weekday_time": _weekday_time,
    "shift_word": _shift_word,
    "early_late_column": _early_late_column,
    "night_mismatch": _night_mismatch,
    "exclusive_conflict": _exclusive_conflict,
    "weekly_days": _weekly_days,
    "no_shift_left": _no_shift_left,
}


# --- 回答の取り込み --------------------------------------------------------------


def apply_answers(
    profiles: Sequence[StaffProfile], answers: Dict[str, Dict[str, str]]
) -> List[str]:
    """お客様の回答を profile に記録する。(要約メッセージ) を返す。

    「はい（合っています）」だけは、その確認事項を解消済みとして外す。
    それ以外の回答は**自動では反映しない**。内容を記録し、確認事項は残したまま
    作成者がExcelで直す。読み取りを推測で書き換えないための決まり。
    """
    summary: List[str] = []
    by_id = {profile.staff_id: profile for profile in profiles}
    # 「はい」で確定してよいのは、選択肢に「はい」がある質問だけ。
    # 自由記入の欄にたまたま同じ文字が入っていても確定させない。
    yes_no_questions = {
        question.question_id
        for question in build_questions(profiles)
        if YES in question.choices
    }
    # 解消できた項目は item_id で覚えておき、最後にまとめて外す。
    resolved_item_ids: Dict[str, List[str]] = {}

    for question_id, answer in sorted(answers.items()):
        staff_id, _, item_id = question_id.partition("#")
        profile = by_id.get(staff_id)
        if profile is None:
            summary.append(f"{question_id}: 該当するスタッフが見つかりませんでした")
            continue

        choice = (answer.get("choice") or "").strip()
        note = (answer.get("note") or "").strip()
        if not choice and not note:
            continue

        recorded = " / ".join(part for part in (choice, note) if part)
        profile.customer_answers[question_id] = recorded

        item = _find_item(profile, item_id)
        if item is None:
            summary.append(
                f"{profile.name}: 「{question_id}」は既に対応済み、"
                "または古いシートの可能性があるため、反映しませんでした"
            )
            continue

        if choice == YES and not note and question_id in yes_no_questions:
            resolved_item_ids.setdefault(staff_id, []).append(item_id)
            summary.append(f"{profile.name}: 「{item.message}」→ 確認が取れました")
        else:
            summary.append(f"{profile.name}: 回答を記録しました（{recorded}）")

    for staff_id, item_ids in resolved_item_ids.items():
        profile = by_id[staff_id]
        ids_to_remove = set(item_ids)
        profile.review_items = [
            item for item in profile.review_items if item.item_id not in ids_to_remove
        ]
    return summary


def _find_item(profile: StaffProfile, item_id: str) -> "ReviewItem | None":
    for item in profile.review_items:
        if item.item_id == item_id:
            return item
    return None
