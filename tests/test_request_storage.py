"""希望の保存先(ローカル/スプレッドシート)のテスト。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from shift_scheduler.request_sheet import StaffRequests  # noqa: E402
from shift_scheduler.request_storage import (  # noqa: E402
    HEADER,
    GoogleSheetStorage,
    LocalStorage,
    get_storage,
)


class _FakeWorksheet:
    """gspread の Worksheet の代わり。書き込み範囲と行数上限だけ再現する。"""

    def __init__(self, initial_rows=None, row_count=2000):
        self.grid = initial_rows if initial_rows is not None else [list(HEADER)]
        self.row_count = row_count
        self.calls = []
        self.fail_update = False

    def row_values(self, row):
        index = row - 1
        return self.grid[index] if index < len(self.grid) else []

    def get_all_records(self):
        if not self.grid:
            return []
        header = self.grid[0]
        return [dict(zip(header, row)) for row in self.grid[1:]]

    def update(self, range_name, values):
        self.calls.append(("update", range_name))
        if self.fail_update:
            raise RuntimeError("通信エラー(テスト用)")
        match = re.match(r"[A-Z]+(\d+)", range_name)
        start_row = int(match.group(1))
        end_row = start_row - 1 + len(values)
        if end_row > self.row_count:
            raise RuntimeError("行数がシートの上限を超えています(テスト用)")
        while len(self.grid) < end_row:
            self.grid.append([])
        for offset, row in enumerate(values):
            self.grid[start_row - 1 + offset] = row

    def resize(self, rows):
        self.calls.append(("resize", rows))
        self.row_count = rows

    def clear(self):
        self.calls.append(("clear",))
        self.grid = []


def test_local_round_trip(tmp_path):
    storage = LocalStorage(tmp_path)
    original = [
        StaffRequests(staff_id="id-1", name="職員A", entries={3: "公", 12: "有"}),
        StaffRequests(staff_id="id-2", name="職員B", entries={5: "○"}),
    ]
    storage.save(2026, 10, original)

    loaded = {r.staff_id: r for r in storage.load(2026, 10)}
    assert loaded["id-1"].entries == {3: "公", 12: "有"}
    assert loaded["id-2"].entries == {5: "○"}


def test_months_are_kept_apart(tmp_path):
    """10月の保存が11月を消さないこと。"""
    storage = LocalStorage(tmp_path)
    storage.save(2026, 10, [StaffRequests(staff_id="id-1", name="職員A", entries={3: "公"})])
    storage.save(2026, 11, [StaffRequests(staff_id="id-1", name="職員A", entries={7: "有"})])

    assert storage.load(2026, 10)[0].entries == {3: "公"}
    assert storage.load(2026, 11)[0].entries == {7: "有"}


def test_missing_month_is_empty_not_an_error(tmp_path):
    assert LocalStorage(tmp_path).load(2026, 10) == []


def test_falls_back_to_local_without_secrets(tmp_path):
    """Secrets が無い/壊れている環境では、この端末のファイルに保存する。"""
    assert isinstance(get_storage(None, tmp_path), LocalStorage)
    assert isinstance(get_storage({}, tmp_path), LocalStorage)
    assert isinstance(get_storage({"requests_sheet_id": "x"}, tmp_path), LocalStorage)


def test_uses_google_sheets_when_configured(tmp_path):
    secrets = {
        "requests_sheet_id": "sheet-123",
        "gcp_service_account": {"client_email": "x@y.z"},
    }
    storage = get_storage(secrets, tmp_path)
    assert isinstance(storage, GoogleSheetStorage)
    assert storage.sheet_id == "sheet-123"
    assert storage.status().is_cloud is True


class _FakeSecrets:
    """st.secrets のように、設定が無いと例外を投げるもの。"""

    def get(self, key, default=None):
        raise RuntimeError("No secrets found")


def test_secrets_that_raise_are_treated_as_absent(tmp_path):
    """st.secrets は secrets.toml が無いと例外を投げる。落とさずローカルに倒す。"""
    assert isinstance(get_storage(_FakeSecrets(), tmp_path), LocalStorage)


# --- GoogleSheetStorage.save の安全性 ---------------------------------------


def test_save_does_not_lose_data_when_the_write_fails():
    """clearしてからupdateする実装だと、updateの失敗で全データが消える。

    書き込みが失敗しても、もともとあったデータは残っていること。
    """
    fake = _FakeWorksheet(
        initial_rows=[
            list(HEADER),
            ["2026", "9", "id-1", "師長", "3", "公"],
        ]
    )
    storage = GoogleSheetStorage("sheet-1", {"client_email": "x@y.z"})
    storage._worksheet = lambda: fake
    fake.fail_update = True

    with pytest.raises(RuntimeError):
        storage.save(2026, 10, [StaffRequests(staff_id="id-2", name="職員B", entries={5: "○"})])

    assert fake.get_all_records() == [
        {"年": "2026", "月": "9", "スタッフID": "id-1", "名前": "師長", "日": "3", "記号": "公"}
    ]


def test_save_never_clears_the_sheet():
    """「消してから書く」をやめたので、clear は一切呼ばれないこと。"""
    fake = _FakeWorksheet()
    storage = GoogleSheetStorage("sheet-1", {"client_email": "x@y.z"})
    storage._worksheet = lambda: fake

    storage.save(2026, 10, [StaffRequests(staff_id="id-1", name="職員A", entries={3: "公"})])

    assert all(call[0] != "clear" for call in fake.calls)


def test_save_resizes_before_writing_when_it_would_not_fit():
    """34名×31日を貯めても行数上限に引っかからないよう、書き込み前に拡張すること。"""
    fake = _FakeWorksheet(initial_rows=[list(HEADER)], row_count=50)
    storage = GoogleSheetStorage("sheet-1", {"client_email": "x@y.z"})
    storage._worksheet = lambda: fake

    requests = [
        StaffRequests(
            staff_id=f"id-{i}",
            name=f"職員{i}",
            entries={day: "公" for day in range(1, 32)},
        )
        for i in range(34)
    ]

    storage.save(2026, 10, requests)

    assert fake.row_count >= 1 + 34 * 31
    assert len(fake.get_all_records()) == 34 * 31


# --- スタッフ情報の保存 -----------------------------------------------------------


def test_local_profiles_round_trip(tmp_path):
    """スタッフ情報が保存先を通して往復すること。"""
    from shift_scheduler.models import StaffProfile

    storage = LocalStorage(tmp_path)
    assert storage.load_profiles() is None, "まだ何も無ければ None"

    original = [
        StaffProfile(
            staff_id="id-1",
            name="師長",
            is_head_nurse=True,
            available_day_shifts=["日③", "日"],
            last_resort_day_shifts=["日③"],
            can_night=False,
        ),
        StaffProfile(staff_id="id-2", name="職員A", unit="ばら", leave_from="2026-09"),
    ]
    storage.save_profiles(original)

    loaded = {p.staff_id: p for p in storage.load_profiles()}
    assert loaded["id-1"].is_head_nurse is True
    assert loaded["id-1"].available_day_shifts == ["日③", "日"]
    assert loaded["id-1"].last_resort_day_shifts == ["日③"]
    assert loaded["id-1"].can_night is False
    assert loaded["id-2"].unit == "ばら"
    assert loaded["id-2"].leave_from == "2026-09"
    assert loaded["id-2"].is_on_leave(2026, 10) is True


# --- 接続の使い回し --------------------------------------------------------------


class _FakeSheet:
    """gspread のシートのふり。呼ばれた回数を数える。"""

    def __init__(self, header):
        self.header = header
        self.row_values_calls = 0

    def row_values(self, index):
        self.row_values_calls += 1
        return self.header

    def get_all_records(self):
        return []

    def update(self, *args, **kwargs):
        pass


class _FakeBook:
    def __init__(self, header):
        self.sheet = _FakeSheet(header)
        self.worksheet_calls = 0

    def worksheet(self, name):
        self.worksheet_calls += 1
        return self.sheet


class _FakeCredentials:
    @classmethod
    def from_service_account_info(cls, info, scopes=None):
        return cls()


class _FakeClient:
    def __init__(self, book):
        self._book = book

    def open_by_key(self, sheet_id):
        return self._book


def _storage_with_fake_book(monkeypatch):
    """認証と接続の部分だけ差し替えたストレージを返す。

    _book() 自体は本番のまま動かすことで、キャッシュの再利用ロジック
    (if self._cached_book is None: ...)を実際に検証する。
    """
    import gspread

    from shift_scheduler import request_storage as module

    storage = module.GoogleSheetStorage("sheet-id", {}, worksheet="requests")
    book = _FakeBook(module.HEADER)
    opened = {"count": 0}

    def fake_authorize(creds):
        opened["count"] += 1
        return _FakeClient(book)

    monkeypatch.setattr(gspread, "authorize", fake_authorize)
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials", _FakeCredentials
    )
    return storage, book, opened


def test_spreadsheet_is_opened_only_once(monkeypatch):
    """同じストレージで何度読んでも、つなぎ直さないこと。

    毎回つなぎ直すと、画面を触るたびにGoogleの読み取り回数を消費し、
    1分あたりの上限(60回)にすぐ達してしまう。
    """
    storage, _book, opened = _storage_with_fake_book(monkeypatch)

    for _ in range(10):
        storage.load(2026, 10)

    assert opened["count"] == 1, "スプレッドシートを開くのは1回だけでよい"


def test_header_is_checked_only_once(monkeypatch):
    """見出し行の確認も、シートにつき1回だけにすること。"""
    storage, book, _opened = _storage_with_fake_book(monkeypatch)

    for _ in range(10):
        storage.load(2026, 10)

    assert book.sheet.row_values_calls == 1
    assert book.worksheet_calls == 1


# --- _sheet() の例外の扱い --------------------------------------------------------


class _BookRaisingOnWorksheet:
    """worksheet() が指定した例外を投げるだけの偽物。"""

    def __init__(self, error):
        self.error = error

    def worksheet(self, name):
        raise self.error


def test_sheet_returns_none_when_worksheet_is_missing(monkeypatch):
    """create=False のとき、シートが無ければ(WorksheetNotFound)None を返すこと。"""
    import gspread

    storage = GoogleSheetStorage("sheet-id", {})
    book = _BookRaisingOnWorksheet(gspread.WorksheetNotFound("no such sheet"))
    monkeypatch.setattr(storage, "_book", lambda: book)

    assert storage._sheet("staff_profiles", ["a"], create=False) is None


def test_sheet_does_not_swallow_unrelated_errors(monkeypatch):
    """429などの一時的な通信エラーを「シートが無い」と誤判定しないこと。

    WorksheetNotFound 以外の例外は、そのまま上に投げること。
    """
    storage = GoogleSheetStorage("sheet-id", {})
    book = _BookRaisingOnWorksheet(RuntimeError("Quota exceeded (テスト用)"))
    monkeypatch.setattr(storage, "_book", lambda: book)

    with pytest.raises(RuntimeError, match="Quota exceeded"):
        storage._sheet("staff_profiles", ["a"], create=False)
