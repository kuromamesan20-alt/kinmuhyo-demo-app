"""施設ごとに変わる呼び名を config/facility.yaml から読む。

病棟名やユニット名は施設によって違う。コードに直接書くと施設ごとに
コードを書き換えることになるので、設定ファイルに追い出している。
設定ファイルが無い・壊れている場合は既定値で動く(デプロイの取りこぼしを
起動失敗にしないため)。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Tuple

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "facility.yaml"

DEFAULTS = {
    "facility_name": "ひまわり苑",
    "ward_label": "第1病棟",
    "case_name": "デモ案件",
    "download_name_template": "勤務計画表_{year}年{month:02d}月_{ward}全体.xlsx",
    "units": ("ばら", "さくら", "ゆり", "すみれ"),
}


@dataclass(frozen=True)
class Facility:
    facility_name: str
    ward_label: str
    case_name: str
    download_name_template: str
    units: Tuple[str, ...]

    def download_name(self, year: int, month: int) -> str:
        # 設定ファイルのテンプレートは人が手で書き換える。書式指定を壊されても
        # ダウンロードボタンで落ちないよう、既定のテンプレートに戻す。
        for template in (self.download_name_template, DEFAULTS["download_name_template"]):
            try:
                return template.format(year=year, month=month, ward=self.ward_label)
            except (KeyError, IndexError, ValueError):
                continue
        return f"勤務計画表_{year}年{month:02d}月.xlsx"


@lru_cache(maxsize=1)
def facility() -> Facility:
    values = dict(DEFAULTS)
    try:
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        loaded = {}
    if isinstance(loaded, dict):
        for key in DEFAULTS:
            if loaded.get(key):
                values[key] = loaded[key]
    values["units"] = tuple(values["units"])
    return Facility(**values)
