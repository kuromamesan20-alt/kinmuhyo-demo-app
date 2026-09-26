"""顧客から届くスタッフ条件CSVの読み込み。

Googleフォームの回答ログ形式(送信ID/送信日時が先頭列)を想定。
列順が変わっても壊れないよう、ヘッダー名でマッピングする。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pandas as pd

# CSVのヘッダー名 -> 内部で使うキー
COLUMN_MAP = {
    "送信ID": "submission_id",
    "送信日時": "submitted_at",
    "案件コード": "case_code",
    "案件名": "case_name",
    "アタマ文字": "surname",  # 苗字。これを表示名に使う
    "名前": "name",
    "勤務表の表記": "sheet_label",  # 勤務表に書く記号(旧: 番号)
    "職種": "role",
    "雇用形態": "employment_type",
    "社会保険": "social_insurance",
    "週勤務日数": "weekly_work_days",
    "固定条件": "conditions",
    "兼務先": "secondary_workplace",
    "夜勤": "can_night",
    "早出": "can_early",
    "遅出": "can_late",
    "保有資格・修了証": "qualifications",
    "備考": "notes",
}

REQUIRED_COLUMNS = ["名前", "固定条件", "夜勤", "早出", "遅出"]

# 表示名に使う列。左から順に、値が入っている最初のものを採る。
# 「アタマ文字」(苗字)が入るようになったので、それを最優先にする。
NAME_COLUMNS = ("surname", "name")

# 顧客側のテスト送信を判別するキーワード(この語が名前に含まれる行は除外)
TEST_ROW_KEYWORDS = ["テスト", "test", "TEST"]


class CsvFormatError(ValueError):
    """CSVの形式が想定と違う場合に送出する。"""


def load_staff_rows(csv_path: Path) -> List[Dict[str, str]]:
    """スタッフ条件の表を読み、正規化した dict のリストを返す。

    - CSV でも Excel でも読める
    - 未知の列は無視し、既知の列だけを内部キーに読み替える
    - 表示名には「アタマ文字」(苗字)を使う。無ければ「名前」
    - 名前が空の行、テスト送信とみられる行は除外する
    """
    if csv_path.suffix.lower() in (".xlsx", ".xlsm"):
        df = pd.read_excel(csv_path, dtype=str).fillna("")
    else:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise CsvFormatError(
            f"CSVに必要な列がありません: {', '.join(missing)}\n"
            f"読み込んだ列: {', '.join(df.columns)}"
        )

    rows: List[Dict[str, str]] = []
    for _, raw in df.iterrows():
        row = {
            key: str(raw[header]).strip()
            for header, key in COLUMN_MAP.items()
            if header in df.columns
        }
        if not row.get("name") and not row.get("surname"):
            continue
        if _is_test_row(row):
            continue

        # 表示名は苗字を優先する。
        # 「名前」列の値(「スタッフW」のような仮名)は、条件文の中でスタッフを
        # 指すのに使われている。苗字で上書きすると同席制約の相手が特定できなく
        # なるので、alias として必ず残す。
        display = next((row[key] for key in NAME_COLUMNS if row.get(key)), "")
        row["alias"] = row.get("name", "")
        row["display_name"] = display
        row["name"] = display
        rows.append(row)
    return rows


def _is_test_row(row: Dict[str, str]) -> bool:
    """顧客側の動作確認用ダミー行かどうか。"""
    haystack = f"{row.get('name', '')} {row.get('notes', '')} {row.get('conditions', '')}"
    return any(keyword in haystack for keyword in TEST_ROW_KEYWORDS)
