"""希望入力画面(app.py)のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import app  # noqa: E402
from shift_scheduler.calendar_utils import month_days  # noqa: E402
from shift_scheduler.models import StaffProfile  # noqa: E402
from shift_scheduler.request_sheet import StaffRequests  # noqa: E402


def test_from_frame_distinguishes_same_name_staff_by_id():
    """同姓同名の職員がいても、名前ではなく staff_id で突き合わせること。

    名前だけで突き合わせると辞書構築時に片方が消え、
    一方の希望がもう一方のものとして保存されてしまう。
    """
    profiles = [
        StaffProfile(staff_id="id-1", name="佐藤"),
        StaffProfile(staff_id="id-2", name="佐藤"),
    ]
    days = month_days(2026, 10)

    frame = app._to_frame(profiles, days, saved={})
    assert "スタッフID" in frame.columns

    frame.loc[frame["スタッフID"] == "id-1", app._column(days[0])] = "公"
    frame.loc[frame["スタッフID"] == "id-2", app._column(days[0])] = "○"

    requests = app._from_frame(frame, profiles, days)
    by_id = {r.staff_id: r for r in requests}

    assert len(requests) == 2
    assert by_id["id-1"].entries == {1: "公"}
    assert by_id["id-2"].entries == {1: "○"}


def test_column_order_hides_staff_id_from_the_editor():
    """スタッフIDは突き合わせ用の内部データで、画面には出さないこと。"""
    days = month_days(2026, 10)
    order = app._column_order(days)

    assert "スタッフID" not in order
    assert "スタッフ" in order


# --- 合言葉 --------------------------------------------------------------------


def test_password_is_read_from_environment(monkeypatch):
    """Secretsが無い手元の環境でも、環境変数で合言葉を指定できること。"""
    monkeypatch.setattr(app, "_secrets", lambda: None)
    monkeypatch.setenv("SHIFT_APP_PASSWORD", "test-word")
    assert app._app_password() == "test-word"


def test_password_prefers_secrets_over_environment(monkeypatch):
    """本番(Secrets)の値が、環境変数より優先されること。"""
    monkeypatch.setattr(app, "_secrets", lambda: {"app_password": "from-secrets"})
    monkeypatch.setenv("SHIFT_APP_PASSWORD", "from-env")
    assert app._app_password() == "from-secrets"


def test_password_is_empty_when_nowhere_set(monkeypatch):
    """どこにも設定が無ければ空。画面側は「設定されていません」と出す。"""
    monkeypatch.setattr(app, "_secrets", lambda: None)
    monkeypatch.delenv("SHIFT_APP_PASSWORD", raising=False)
    assert app._app_password() == ""


def test_password_ignores_surrounding_spaces(monkeypatch):
    """Secretsに余分な空白が入っていても通ること。"""
    monkeypatch.setattr(app, "_secrets", lambda: {"app_password": "  word  "})
    assert app._app_password() == "word"


def test_wrong_password_does_not_unlock(monkeypatch):
    """合言葉が違えば開かないこと。照合そのものを確かめる。"""
    state = {}
    monkeypatch.setattr(app, "_secrets", lambda: {"app_password": "正しい合言葉"})
    monkeypatch.setattr(app.st, "session_state", state)
    _stub_form(monkeypatch, entered="ちがう合言葉", submitted=True)
    errors = _capture_errors(monkeypatch)

    app._password_page()

    assert app._UNLOCKED_KEY not in state, "違う合言葉で開いてはいけない"
    assert errors, "違うことを画面に知らせる"


def test_correct_password_unlocks(monkeypatch):
    state = {}
    monkeypatch.setattr(app, "_secrets", lambda: {"app_password": "正しい合言葉"})
    monkeypatch.setattr(app.st, "session_state", state)
    _stub_form(monkeypatch, entered="正しい合言葉", submitted=True)
    _capture_errors(monkeypatch)
    monkeypatch.setattr(app.st, "rerun", lambda: None)

    app._password_page()

    assert state.get(app._UNLOCKED_KEY) is True


def test_password_with_surrounding_spaces_still_opens(monkeypatch):
    """入力欄に空白が混ざっても開けること(貼り付け時によくある)。"""
    state = {}
    monkeypatch.setattr(app, "_secrets", lambda: {"app_password": "合言葉"})
    monkeypatch.setattr(app.st, "session_state", state)
    _stub_form(monkeypatch, entered="  合言葉  ", submitted=True)
    _capture_errors(monkeypatch)
    monkeypatch.setattr(app.st, "rerun", lambda: None)

    app._password_page()

    assert state.get(app._UNLOCKED_KEY) is True


def test_page_does_not_open_when_no_password_is_set(monkeypatch):
    """合言葉が未設定なら、何を入れても開かないこと。"""
    state = {}
    monkeypatch.setattr(app, "_secrets", lambda: None)
    monkeypatch.delenv("SHIFT_APP_PASSWORD", raising=False)
    monkeypatch.setattr(app.st, "session_state", state)
    _stub_form(monkeypatch, entered="", submitted=True)
    errors = _capture_errors(monkeypatch)

    app._password_page()

    assert app._UNLOCKED_KEY not in state
    assert errors


# --- テスト用の差し替え ----------------------------------------------------------


class _FakeForm:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _stub_form(monkeypatch, entered: str, submitted: bool) -> None:
    """Streamlitのフォーム部品を、決まった値を返すものに差し替える。"""
    monkeypatch.setattr(app.st, "title", lambda *a, **k: None)
    monkeypatch.setattr(app.st, "write", lambda *a, **k: None)
    monkeypatch.setattr(app.st, "info", lambda *a, **k: None)
    monkeypatch.setattr(app.st, "form", lambda *a, **k: _FakeForm())
    monkeypatch.setattr(app.st, "text_input", lambda *a, **k: entered)
    monkeypatch.setattr(app.st, "form_submit_button", lambda *a, **k: submitted)


def _capture_errors(monkeypatch) -> list:
    errors: list = []
    monkeypatch.setattr(app.st, "error", lambda message, *a, **k: errors.append(message))
    return errors


# --- スプレッドシートの読み取り回数 ------------------------------------------------


class _CountingStorage:
    """呼ばれた回数を数えるだけの偽の保存先。"""

    def __init__(self):
        self.profile_calls = 0
        self.load_calls = 0
        self.stored: dict = {}

    def load_profiles(self):
        self.profile_calls += 1
        return [StaffProfile(staff_id="id-1", name="佐藤")]

    def load(self, year, month):
        self.load_calls += 1
        return list(self.stored.get((year, month), []))

    def save(self, year, month, requests):
        self.stored[(year, month)] = list(requests)


def _clear_caches() -> None:
    app._fetch_staff.clear()
    app._saved_requests.clear()


def test_staff_is_read_only_once_no_matter_how_many_reruns():
    """画面が何度作り直されても、スタッフ情報は1回しか読まないこと。

    Streamlit はマスを1つ触るたびに画面全体を作り直す。毎回読みに行くと
    Googleの上限(1分60回)に達して「Quota exceeded」で入力が止まる。
    実際にその不具合が起きたので、回数を固定しておく。
    """
    _clear_caches()
    storage = _CountingStorage()

    for _ in range(20):
        app._fetch_staff(storage, "key")

    assert storage.profile_calls == 1


def test_saved_requests_are_read_only_once():
    _clear_caches()
    storage = _CountingStorage()

    for _ in range(20):
        app._saved_requests(storage, 2026, 10)

    assert storage.load_calls == 1


def test_a_different_month_is_read_separately():
    """月を切り替えたら、その月の希望はちゃんと読み直すこと。"""
    _clear_caches()
    storage = _CountingStorage()

    app._saved_requests(storage, 2026, 10)
    app._saved_requests(storage, 2026, 11)

    assert storage.load_calls == 2


def test_clearing_the_cache_reads_again():
    """「最新に更新」を押したら読み直すこと。"""
    _clear_caches()
    storage = _CountingStorage()

    app._fetch_staff(storage, "key")
    _clear_caches()
    app._fetch_staff(storage, "key")

    assert storage.profile_calls == 2


def test_saving_then_reading_gives_the_new_content():
    """保存した直後に読むと、保存した内容が返ること。

    覚えた内容を捨て忘れると、保存したのに古い内容が表示される。
    「Quota exceeded」より厄介な不具合なので、ここで止める。
    """
    _clear_caches()
    storage = _CountingStorage()

    # 1回読んで覚えさせる(この時点では空)
    assert app._saved_requests(storage, 2026, 10) == []

    # 保存する。画面側は保存のあと必ず覚えた分を捨てる
    storage.save(2026, 10, [StaffRequests(staff_id="id-1", name="佐藤", entries={3: "公"})])
    app._saved_requests.clear()

    after = app._saved_requests(storage, 2026, 10)
    assert [r.entries for r in after] == [{3: "公"}], "保存した内容が返ること"


def test_staff_cache_key_differs_by_storage_destination():
    """保存先(スプレッドシートID)が違えば、キャッシュキーも違うこと。

    クラス名だけだと、別のスプレッドシートに切り替えても
    古いスタッフ情報がキャッシュから返り続けてしまう。
    """
    from shift_scheduler.request_storage import GoogleSheetStorage, LocalStorage

    storage_a = GoogleSheetStorage("sheet-aaa", {})
    storage_b = GoogleSheetStorage("sheet-bbb", {})
    local = LocalStorage(Path("/tmp/dummy"))

    assert app._storage_cache_key(storage_a) != app._storage_cache_key(storage_b)
    assert app._storage_cache_key(storage_a) != app._storage_cache_key(local)


def test_cached_status_is_read_separately_per_storage_destination():
    """保存先ごとにstatus()を読み直すこと(1プロセスで固定にしない)。"""
    _clear_status_cache()

    class _CountingStorage2:
        def __init__(self, sheet_id):
            self.sheet_id = sheet_id
            self.calls = 0

        def status(self):
            self.calls += 1
            return f"status-{self.sheet_id}"

    storage_a = _CountingStorage2("aaa")
    storage_b = _CountingStorage2("bbb")

    key_a = app._storage_cache_key(storage_a)
    key_b = app._storage_cache_key(storage_b)
    assert app._cached_status(storage_a, key_a) == "status-aaa"
    assert app._cached_status(storage_b, key_b) == "status-bbb"
    assert storage_a.calls == 1
    assert storage_b.calls == 1


def _clear_status_cache() -> None:
    app._cached_status.clear()


def test_wish_page_clears_the_cache_after_saving():
    """保存の処理に「覚えた分を捨てる」が書かれていること。

    書き忘れると上のテストが通っていても実際の画面では古い内容が出る。
    保存とキャッシュ削除が離れて書かれている間は、この見張りが要る。
    """
    import inspect

    source = inspect.getsource(app._wish_page)
    save_at = source.index("storage.save(")
    clear_at = source.index("_saved_requests.clear()")
    assert clear_at > save_at, "保存したあとに、覚えた分を捨てること"


# --- 夜勤明けの自動補完 ----------------------------------------------------------


def _days(year=2026, month=10):
    from shift_scheduler.calendar_utils import month_days as _md
    return _md(year, month)


def test_night_after_is_filled_in_the_next_day():
    """○(夜勤)の翌日に △(明け)が入ること。"""
    from shift_scheduler.request_sheet import StaffRequests

    requests = [StaffRequests(staff_id="id-1", name="高橋", entries={12: "○"})]
    added = app._fill_night_after(requests, _days())

    assert requests[0].entries == {12: "○", 13: "△"}
    assert added == 1


def test_night_after_does_not_overwrite_an_existing_entry():
    """翌日にすでに希望が入っていれば、上書きしないこと。

    ○の翌日に有給が入っているような食い違いは、
    勝手に消さずに勤務表を作るときの知らせに任せる。
    """
    from shift_scheduler.request_sheet import StaffRequests

    requests = [StaffRequests(staff_id="id-1", name="高橋", entries={12: "○", 13: "有"})]
    added = app._fill_night_after(requests, _days())

    assert requests[0].entries[13] == "有", "先に入っていた希望を消してはいけない"
    assert added == 0


def test_night_after_is_not_added_past_the_end_of_the_month():
    """月末の夜勤には明けを入れないこと(翌日がその月に無い)。"""
    from shift_scheduler.request_sheet import StaffRequests

    requests = [StaffRequests(staff_id="id-1", name="高橋", entries={31: "○"})]
    added = app._fill_night_after(requests, _days(2026, 10))  # 10月は31日まで

    assert requests[0].entries == {31: "○"}
    assert added == 0


def test_night_after_leaves_the_carry_over_column_alone():
    """前月末の欄(0日)は、その月の日にちではないので触らないこと。"""
    from shift_scheduler.request_sheet import CARRY_OVER_DAY, StaffRequests

    requests = [StaffRequests(staff_id="id-1", name="吉田", entries={CARRY_OVER_DAY: "○"})]
    added = app._fill_night_after(requests, _days())

    assert requests[0].entries == {CARRY_OVER_DAY: "○"}
    assert added == 0


def test_night_after_handles_several_nights():
    from shift_scheduler.request_sheet import StaffRequests

    requests = [StaffRequests(staff_id="id-1", name="森田", entries={2: "○", 14: "○", 20: "○"})]
    added = app._fill_night_after(requests, _days())

    assert added == 3
    for day in (3, 15, 21):
        assert requests[0].entries[day] == "△"


def test_deep_night_does_not_get_a_night_after():
    """◉(深夜)は ◉→公 の2日セット。明け(△)は付かないこと。"""
    from shift_scheduler.request_sheet import StaffRequests

    requests = [StaffRequests(staff_id="id-1", name="伊藤", entries={3: "◉"})]
    added = app._fill_night_after(requests, _days())

    assert requests[0].entries == {3: "◉"}
    assert added == 0


def test_column_config_options_include_the_auto_filled_night_after():
    """日付列の選択肢に △(NIGHT_AFTER) が含まれること。

    _fill_night_after は保存時に ○ の翌日へ自動で △ を書き込む。
    その△が選択肢に無いと、保存後の再描画で
    st.data_editor の SelectboxColumn が「選択肢に無い値」を
    持つセルを表示することになり、画面が壊れる。
    """
    days = _days()
    config = app._column_config(days)

    for day in days:
        # SelectboxColumn は dict を返す。選択肢は type_config の中にある。
        options = config[app._column(day)]["type_config"]["options"]
        assert app.NIGHT_AFTER in options, (
            f"{day.day}日の選択肢に自動で入る△(NIGHT_AFTER)が無い"
        )


def test_saved_message_count_excludes_auto_filled_night_after():
    """保存メッセージの件数(filled)に、自動で足した△を含めないこと。

    _fill_night_after のあとに filled を数えると、
    本人が入力していない△まで件数に上乗せされて誤解を招く。
    """
    import inspect

    source = inspect.getsource(app._wish_page)
    filled_at = source.index("filled = sum(")
    fill_after_at = source.index("_fill_night_after(requests, days)")
    assert filled_at < fill_after_at, (
        "filled の件数は _fill_night_after を呼ぶ前に数えること"
    )
