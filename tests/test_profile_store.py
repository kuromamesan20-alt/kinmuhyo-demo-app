"""profile_store(YAML保存)と ReviewItem の安定IDに関するテスト。

一度 import-answers を実行して review_items の一部が消えた後の YAML を
--profiles に指定し、同じ(古い)回答シートをもう一度読み込んでも、
別の項目を誤って確認済みにしないことを保証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from shift_scheduler.models import ReviewItem, StaffProfile  # noqa: E402
from shift_scheduler.profile_store import load_profiles, save_profiles  # noqa: E402
from shift_scheduler.questions import YES, apply_answers, build_questions  # noqa: E402


def sample_profiles():
    return [
        StaffProfile(
            staff_id="id-1",
            name="スタッフさ",
            review_items=[
                ReviewItem(
                    message="早出の解釈",
                    code="early_late_column",
                    payload={"label": "早出", "removed": ["日①", "日②"]},
                ),
                ReviewItem(
                    message="未解釈の記述あり",
                    code="unparsed",
                    payload={"sentence": "介護リーダー"},
                ),
                ReviewItem(
                    message="遅出の解釈",
                    code="early_late_column",
                    payload={"label": "遅出", "removed": ["日⑦", "日⑨"]},
                ),
            ],
        )
    ]


def test_review_item_has_stable_id():
    item = ReviewItem(message="確認してください", code="other")
    assert item.item_id
    assert isinstance(item.item_id, str)


def test_item_id_is_the_same_every_time():
    """同じ内容なら毎回同じID。読み取りをやり直しても、お渡し済みのシートが使える。"""
    first = ReviewItem(message="確認してください", code="unparsed", payload={"sentence": "介護リーダー"})
    second = ReviewItem(message="確認してください", code="unparsed", payload={"sentence": "介護リーダー"})
    assert first.item_id == second.item_id


def test_item_id_does_not_change_when_wording_changes():
    """質問の文面を直しても、同じ確認事項ならIDは変わらない。"""
    before = ReviewItem(message="古い文面", code="early_late_column", payload={"label": "遅出"})
    after = ReviewItem(message="新しい文面", code="early_late_column", payload={"label": "遅出"})
    assert before.item_id == after.item_id


def test_ids_stay_unique_within_one_staff():
    """同じ種別・同じ原文が並んでも、1人の中ではIDがぶつからない。"""
    profile = StaffProfile(staff_id="x", name="本人")
    profile.flag_review("1つ目", code="unparsed", sentence="同じ文")
    profile.flag_review("2つ目", code="unparsed", sentence="同じ文")
    assert len({item.item_id for item in profile.review_items}) == 2


def test_weekday_unavailable_shifts_round_trips_with_int_keys(tmp_path):
    """weekday_unavailable_shifts がYAML往復後もキーがintのままであること。"""
    profile = StaffProfile(
        staff_id="id-9",
        name="スタッフ曜",
        weekday_unavailable_shifts={1: ["日①", "日②"], 4: ["日①"]},
    )
    path = tmp_path / "profiles.yaml"
    save_profiles([profile], path)
    loaded = load_profiles(path)

    assert loaded[0].weekday_unavailable_shifts == {1: ["日①", "日②"], 4: ["日①"]}
    assert all(isinstance(key, int) for key in loaded[0].weekday_unavailable_shifts)


def test_item_id_round_trips_through_yaml(tmp_path):
    profiles = sample_profiles()
    original_ids = [item.item_id for item in profiles[0].review_items]

    path = tmp_path / "profiles.yaml"
    save_profiles(profiles, path)
    loaded = load_profiles(path)

    assert [item.item_id for item in loaded[0].review_items] == original_ids


def test_question_id_uses_item_id_not_position():
    profiles = sample_profiles()
    item_ids = [item.item_id for item in profiles[0].review_items]
    questions = build_questions(profiles)
    assert [q.question_id for q in questions] == [f"id-1#{item_id}" for item_id in item_ids]


def test_import_answers_twice_does_not_remove_wrong_item(tmp_path):
    """import-answers を2回連続で実行しても、2回目で別の項目が消えないこと。

    1回目: いくつかの項目を「はい」で解消し、YAMLに保存する。
    2回目: 同じ(古い番号のままの)回答を、保存済みYAMLを読み直した profiles に
    再度適用しても、残っている項目が誤って消えないこと。
    """
    profiles = sample_profiles()
    questions = build_questions(profiles)
    old_answers = {
        questions[0].question_id: {"choice": YES, "note": ""},
        questions[2].question_id: {"choice": YES, "note": ""},
    }

    # 1回目: 早出と遅出の確認が解消され、未解釈の項目だけが残る
    apply_answers(profiles, old_answers)
    assert [item.code for item in profiles[0].review_items] == ["unparsed"]

    path = tmp_path / "profiles.yaml"
    save_profiles(profiles, path)
    reloaded = load_profiles(path)

    # 2回目: 古い(既に消費済みの)回答シートをそのまま再適用
    summary = apply_answers(reloaded, old_answers)

    assert [item.code for item in reloaded[0].review_items] == ["unparsed"], (
        "既に消えた項目のIDが、残っている項目を巻き込んで消してはいけない"
    )
    assert any(
        "対応済み" in line or "古いシート" in line for line in summary
    ), "存在しない質問IDには、その旨の警告をsummaryに入れる"
