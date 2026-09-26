"""この施設のシフト区分の定義。

勤務表に実際に書く記号がそのまま区分名になる。

  日勤帯   日①②③⑤⑦⑨ (④⑥⑧は使っていない)
  夜勤     ○(入り) → △(明け) → 公(公休)  の3日セット
  深夜     ◉(入り) → 公(公休)            の2日セット
  公休     公

パート職員など、決まった時間だけ出る人は番号を使わず「9-15」のように
時間をそのままセルに書く。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# コード -> (開始, 終了)。並び順は勤務表に並べる順。
DAY_SHIFTS: Dict[str, Tuple[str, str]] = {
    "日①": ("06:00", "14:00"),
    "日②": ("07:00", "15:00"),
    "日③": ("08:00", "16:00"),
    "日": ("09:00", "17:00"),
    "日⑤": ("11:00", "19:00"),
    "日⑦": ("13:00", "21:00"),
    "日⑨": ("12:30", "20:30"),
}
DAY_SHIFT_CODES: List[str] = list(DAY_SHIFTS)

# 毎日必ず埋めなければならない日勤帯(各ちょうど1人)。
# 番号なしの「日」は人数の決まりが無く、残りの勤務者が入る枠。
# 実物では1日平均5.1人と最も多く、日勤の主力になっている。
REQUIRED_DAY_SHIFTS: Tuple[str, ...] = ("日①", "日②", "日③", "日⑤", "日⑦", "日⑨")
OPTIONAL_DAY_SHIFTS: Tuple[str, ...] = ("日",)

# 勤務表に書く記号
NIGHT_IN = "○"  # 夜勤入り
NIGHT_AFTER = "△"  # 夜勤明け
LATE_NIGHT_IN = "◉"  # 深夜入り(実物の勤務表で使われている文字。二重丸の ◎ とは別)
OFF = "公"  # 公休

# 勤務の代わりにその日を占める記号。日勤帯や夜勤と同じ場所(勤務欄)に書く。
# いつ誰が取るかはこちらでは決められないので、希望休と同じく事前に聞く。
# 公休(月9日)とは別に数える。
PAID_LEAVE = "有"  # 有給休暇
SUMMER_LEAVE = "夏"  # 夏季休暇
TRAINING = "研"  # 研修
HEALTH_CHECK = "健"  # 健康診断

# 公休以外の「その日は勤務に入れない」記号
ABSENCE_MARKS: Tuple[str, ...] = (PAID_LEAVE, SUMMER_LEAVE, TRAINING, HEALTH_CHECK)

# 夜勤・深夜は複数日にまたがる。1日目から順に、この記号を並べて埋める。
NIGHT_SEQUENCE = (NIGHT_IN, NIGHT_AFTER, OFF)
LATE_NIGHT_SEQUENCE = (LATE_NIGHT_IN, OFF)

# 「同席」とは、同じ夜に2人とも夜間の勤務に入っていること。
# 夜間は ○ が2人・◉ が1人の計3人なので、○と○、○と◉のどちらも同席になる。
# 施設の担当者も「夜勤(深夜含む)」という言い方をしており、○◉の別は問わない。
# 「同席不可」は、その2人を同じ夜に入れてはいけない、という意味。
# 「◯回まで」なら、同じ夜に入る回数がその月に何回までか。
# 「同じナースが複数回同席しないように」は、同じ組み合わせを繰り返さないこと。

ALL_MARKS: List[str] = DAY_SHIFT_CODES + [NIGHT_IN, NIGHT_AFTER, LATE_NIGHT_IN, OFF]

# 毎日必要な人数。施設で実際に使われている勤務表から確認した値。
#
#   日①②③⑤⑦⑨  各ちょうど1人(勤務表の最下部に「6種そろっているか」の判定行がある)
#   ○(夜勤入り)   2人(実物は31日中26日が2人、5日が3人)
#   ◉(深夜入り)   1人(31日すべて1人)
#   日            決まった人数はなく、残りの勤務者が入る枠(実物は1日2〜9人)
#
DAILY_REQUIREMENT: Dict[str, int] = {code: 1 for code in REQUIRED_DAY_SHIFTS}
DAILY_REQUIREMENT[NIGHT_IN] = 2
DAILY_REQUIREMENT[LATE_NIGHT_IN] = 1

# CSVの「早出」「遅出」列には時間が書かれていないため、この範囲を指すものとして扱う。
# 条件文に時間が書かれている人は、そちらを優先する(こちらは使わない)。
# 両方とも不可のパート職員が「日③」と「日」に残るよう、この範囲にしている。
# 公休の日数は人によって違う(確認済み)。
# 全員を同じ日数で縛ると、必要人数を満たせず組めなくなる。
# 週の勤務日数の目安からゆるく寄せるだけにしている。

# 月の公休の日数。施設の担当者から「通常は9日。特例で8日や10日の月もある」と
# 伺っている。月ごとに変わり、こちらでは決められないので、勤務表を作るときに
# 指定してもらう。ここにあるのは、指定がないときの既定値。
#
# そろえるのは正職員だけ。パートの方に公休9日の方はいない(確認済み)。
# 公休(公)だけを数える。有給・正休・夏休・研修・健診は別勘定。
DEFAULT_MONTHLY_OFF_DAYS = 9

# 連続勤務の上限。実物の2026年7月では6日以上が1回も無く、
# 「5日まで」と確認済み。
# 夜勤明け(△)も勤務が続いているものとして数える。
MAX_CONSECUTIVE_WORK_DAYS = 5

# 時短の勤務時間。番号のシフトに当てはまらない時間帯で働く人のために、
# 希望の入力画面で「その日はこの時間」と選べるようにする。
# 実物の勤務表では「16」「15」のように終わりの時刻だけ書かれているが、
# 選んだものがそのまま入る方が分かりやすいので、この表記のまま入れる。
# 末尾の「時」は飾りではない。「9-16」のままだとExcelが日付(9月16日)に
# 変換してしまい、読み戻したときに希望が消える。
#
# 中身は施設の担当者のご要望に合わせて変えてきた:
#   9-13時 / 13-17時 を追加(2026年9月)
#   8-15時 を削除し 9-15時 を追加(2026年9月)
#     8-15時 はスタッフカードの例示から作ったもので、実際には使われていなかった。
#   9-16時 は一度削ったが、施設の担当者のご要望で戻した(2026年9月)。
#     「日勤のときと9-16の場合がある」方がいらっしゃるため。
HOUR_CHOICES: Tuple[str, ...] = ("9-13時", "13-17時", "9-15時", "9-16時")

# 勤務時間をセルに直接書く人が、希望としてそのまま選べる記号。
# 「日」(9-17)は番号なしで人数の決まりが無いので、そのまま書いても
# 他の人の枠を奪わない。
SELF_WRITTEN_MARKS: Tuple[str, ...] = ("日",) + HOUR_CHOICES

EARLY_SHIFTS: Tuple[str, ...] = ("日①", "日②")
LATE_SHIFTS: Tuple[str, ...] = ("日⑤", "日⑦", "日⑨")


def to_minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def starts_before(code: str, hhmm: str) -> bool:
    """そのシフトの開始が hhmm より前か。"""
    return to_minutes(DAY_SHIFTS[code][0]) < to_minutes(hhmm)


def ends_after(code: str, hhmm: str) -> bool:
    """そのシフトの終了が hhmm より後か。"""
    return to_minutes(DAY_SHIFTS[code][1]) > to_minutes(hhmm)


def shifts_within(not_before: str = None, not_after: str = None) -> List[str]:
    """「◯時より前は不可」「◯時より後は不可」を満たす日勤帯コードを返す。"""
    codes = []
    for code in DAY_SHIFT_CODES:
        if not_before and starts_before(code, not_before):
            continue
        if not_after and ends_after(code, not_after):
            continue
        codes.append(code)
    return codes


def describe(code: str) -> str:
    """人が読む用の説明。例: 「日③ 8:00-16:00」"""
    if code in DAY_SHIFTS:
        start, end = DAY_SHIFTS[code]
        return f"{code} {start}-{end}"
    return {
        NIGHT_IN: "○ 夜勤(入り)",
        NIGHT_AFTER: "△ 夜勤明け",
        LATE_NIGHT_IN: "◉ 深夜(入り)",
        OFF: "公 公休",
    }.get(code, code)
