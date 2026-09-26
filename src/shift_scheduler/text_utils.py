"""自由記述テキストの正規化ユーティリティ。

「スタッフＡ」(全角A)と「スタッフA」(半角A)のような表記揺れが実データにあるため、
比較・照合の前に必ず正規化する。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

# 文の区切り。改行・句点・箇条書き記号で分割する。
_SENTENCE_SPLIT = re.compile(r"[\n。；;]+")


def normalize(text: str) -> str:
    """全角英数を半角に、波ダッシュ・チルダ類を「〜」に揃える。"""
    if not text:
        return ""
    # NFKC で全角英数・全角記号を半角へ(「〜」は別途戻す)
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("~", "〜").replace("∼", "〜").replace("－", "-")
    return normalized.strip()


def split_sentences(text: str) -> List[str]:
    """自由記述を文単位に分割する。空文は除く。"""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(normalize(text)) if s.strip()]


def to_hhmm(hour: str, minute: str = "") -> str:
    """「9」「9」「30」から "09:00" / "09:30" 形式を作る。"""
    h = int(hour)
    m = int(minute) if minute else 0
    return f"{h:02d}:{m:02d}"
