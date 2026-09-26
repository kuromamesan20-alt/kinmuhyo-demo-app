"""希望休・希望出勤の入力画面(Streamlit)。

施設の担当者がブラウザで開き、31名分の希望を入力する。
入力内容は Googleスプレッドシートに保存され、勤務表を組むときに読み込む。

  起動: ./venv/bin/python -m streamlit run app.py
"""

from __future__ import annotations

import hmac
import io
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from shift_scheduler.calendar_utils import month_days, month_label  # noqa: E402
from shift_scheduler.profile_store import load_profiles  # noqa: E402
from shift_scheduler.request_sheet import (  # noqa: E402
    CARRY_OVER_CHOICES,
    CARRY_OVER_DAY,
    WISH_CHOICES,
    StaffRequests,
)
from shift_scheduler.request_storage import get_storage  # noqa: E402
from shift_scheduler.excel_export import export_schedule  # noqa: E402
from shift_scheduler.facility import facility  # noqa: E402
from shift_scheduler.scheduler import ALERT_MARK, build_schedule  # noqa: E402
from shift_scheduler.shifts import (  # noqa: E402
    ABSENCE_MARKS,
    DAY_SHIFTS,
    DEFAULT_MONTHLY_OFF_DAYS,
    HOUR_CHOICES,
    LATE_NIGHT_IN,
    NIGHT_AFTER,
    NIGHT_IN,
    OFF,
)

PROFILES_PATH = Path(__file__).parent / "data/staff_profiles/case001.yaml"
# 空欄はStreamlitが薄い「None」と表示してしまうので、控えめな記号を置く。
# 画面を埋め尽くさないよう短くし、保存時には空欄に戻す。
BLANK = "・"
# 前月の最終日の列。夜勤は ○→△→公 と3日にまたがるので、
# 前月末が分からないと1日・2日が前月と食い違う。
CARRY_LABEL = "前月末"

MEANING = {
    OFF: "休みたい",
    NIGHT_IN: "夜勤に入りたい",
    LATE_NIGHT_IN: "深夜に入りたい",
    "有": "有給休暇",
    "夏": "夏季休暇",
    "研": "研修",
    "健": "健康診断",
}


def main() -> None:
    st.set_page_config(page_title="勤務表", layout="wide")

    # 合言葉を通らないと中身を出さない。
    # スタッフの氏名・希望休が見えるうえ、書き換えもできる画面なので、
    # URLを知っただけでは開けないようにする。
    if not _unlocked():
        _password_page()
        return

    # 上のタブで画面を切り替える。URLを分けると案内が増えるので1つにまとめる。
    wish_tab, build_tab = st.tabs(["希望を入力する", "勤務表を作る"])
    with wish_tab:
        _wish_page()
    with build_tab:
        _build_page()


# --- 合言葉 --------------------------------------------------------------------

_UNLOCKED_KEY = "unlocked"


def _password_page() -> None:
    """合言葉の入力画面。ここを通らないと中身は出ない。"""
    st.title("勤務表アプリ")

    password = _app_password()
    if not password:
        st.error("合言葉が設定されていないため、開けません。")
        st.info(
            "手元で動かすときは、環境変数 `SHIFT_APP_PASSWORD` に合言葉を入れて起動してください。"
            "インターネットに公開するときは Streamlit Cloud の Settings → Secrets に "
            "`app_password = \"(合言葉)\"` を追加します。"
        )
        return

    st.write("合言葉を入れてください。")
    with st.form("password_form"):
        entered = st.text_input("合言葉", type="password")
        submitted = st.form_submit_button("開く", type="primary")

    if submitted:
        # 文字数の違いから中身を推測されないよう、時間のかからない比較を使う。
        # compare_digest は非ASCIIの文字列を比較できないので、バイト列にしてから渡す。
        # (日本語の合言葉を設定したときにここで落ちる)
        if hmac.compare_digest(entered.strip().encode("utf-8"), password.encode("utf-8")):
            st.session_state[_UNLOCKED_KEY] = True
            st.rerun()
        else:
            st.error("合言葉が違います。")


def _unlocked() -> bool:
    return bool(st.session_state.get(_UNLOCKED_KEY))


def _app_password() -> str:
    """合言葉を読む。手元で動かすときは環境変数でも指定できる。"""
    secrets = _secrets()
    if secrets is not None:
        value = str(secrets.get("app_password") or "").strip()
        if value:
            return value
    return os.environ.get("SHIFT_APP_PASSWORD", "").strip()


def _wish_page() -> None:
    st.title("希望休・希望出勤の入力")

    storage = get_storage(_secrets(), PROFILES_PATH.parent)
    status = _cached_status(storage, _storage_cache_key(storage))

    try:
        profiles = _staff(storage)
    except Exception as error:
        st.error("スタッフ情報の読み込みに失敗しました。")
        st.exception(error)
        _refresh_button()
        return
    if not profiles:
        st.error("スタッフ情報がまだ登録されていません。")
        st.markdown(
            "スプレッドシートに `staff_profiles` シートを作り、スタッフ情報を"
            "書き出してください。手順は開発者にお問い合わせください。"
        )
        st.caption(f"保存先: {status.provider}")
        return

    year, month = _pick_month()
    active = [p for p in profiles if not p.is_on_leave(year, month)]

    st.caption(f"保存先: {status.provider} — {status.message}")

    saved = {r.staff_id: r for r in _saved_requests(storage, year, month)}
    days = month_days(year, month)

    st.subheader(f"{month_label(year, month)}（スタッフ {len(active)} 名）")
    _show_legend()

    edited = st.data_editor(
        _editor_frame(active, days, saved, year, month),
        use_container_width=True,
        height=min(760, 80 + 36 * len(active)),
        column_config=_column_config(days),
        column_order=_column_order(days),
        disabled=["スタッフ"],
        hide_index=True,
        key=f"editor-{year}-{month}-v4",
    )

    left, middle, right = st.columns([1, 1, 3])
    if left.button("保存する", type="primary"):
        requests = _from_frame(edited, active, days)
        # 自動で足す△を含めないよう、本人の入力数はここで先に数えておく
        filled = sum(len(r.entries) for r in requests)
        # ○(夜勤)の翌日に △(明け)を補う。勤務表と同じ「○△」の並びで見えるように。
        filled_after = _fill_night_after(requests, days)
        storage.save(year, month, requests)
        # 保存した内容が次の表示に反映されるよう、覚えていた分を捨てる
        _saved_requests.clear()
        st.session_state.pop(_frame_key(year, month), None)
        message = f"保存しました（{filled} 件の希望）"
        if filled_after:
            message += f" … 夜勤の翌日に明け（{NIGHT_AFTER}）を {filled_after} 件入れました"
        right.success(message)
    with middle:
        _refresh_button()

    _show_summary(edited, days)


def _build_page() -> None:
    """勤務表を作ってダウンロードする画面。"""
    st.title("勤務表を作る")

    storage = get_storage(_secrets(), PROFILES_PATH.parent)
    try:
        profiles = _staff(storage)
    except Exception as error:
        st.error("スタッフ情報の読み込みに失敗しました。")
        st.exception(error)
        _refresh_button()
        return
    if not profiles:
        st.error("スタッフ情報がまだ登録されていません。")
        return

    year, month = _pick_month(key="build")
    requests = _saved_requests(storage, year, month)
    filled = [r for r in requests if r.entries]

    st.write(
        f"**{month_label(year, month)}** … スタッフ {len(profiles)} 名 / "
        f"希望の記入 **{len(filled)} 名分**"
    )
    if not filled:
        st.warning("この月の希望はまだ1件も入っていません。このまま作ることもできます。")

    off_days = int(
        st.number_input(
            "今月の公休の日数",
            min_value=0,
            max_value=15,
            value=DEFAULT_MONTHLY_OFF_DAYS,
            step=1,
            key="build-off-days",
            help=(
                "正職員の方の公休をこの日数ちょうどにします。"
                "ふつうは9日です。8日や10日の月だけ直してください。"
                "パートの方は対象外です。"
                "有・夏・研・健はこの日数に含めません。"
                "0にすると、日数をそろえません。"
            ),
        )
    )

    if not st.button("勤務表を作る", type="primary", key="build-button"):
        return

    with st.spinner("組んでいます。1分ほどかかることがあります..."):
        result = build_schedule(
            profiles,
            requests,
            year,
            month,
            time_limit_seconds=120,
            monthly_off_days=off_days,
        )

    if not result.ok:
        st.error(f"組めませんでした（{result.status}）")
        for message in result.messages:
            st.write(f"- {message}")
        st.info(
            f"公休を {off_days} 日ちょうどにする条件が、"
            "毎日の必要人数と噛み合っていない可能性があります。"
            "日数を1日変えて試すと組めることがあります。"
        )
        return

    st.success(
        f"できました（{result.status}） … {len(result.assignments)} 名 / "
        f"責任者「せ」{len(result.responsible)} 日"
    )

    # 「★要確認」は見落とすと1か月まるごと休みの人が出たまま渡ってしまう。
    # 折りたたみの中に入れず、そのまま画面に出す。
    alerts = [m for m in result.messages if m.startswith(ALERT_MARK)]
    notes = [m for m in result.messages if not m.startswith(ALERT_MARK)]
    for message in alerts:
        st.warning(message.removeprefix(ALERT_MARK).strip())
    if notes:
        with st.expander("組んだときのメモ", expanded=False):
            for message in notes:
                st.write(f"- {message}")

    st.download_button(
        "Excelをダウンロード",
        data=_to_excel_bytes(result, profiles),
        file_name=facility().download_name(year, month),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )


def _to_excel_bytes(result, profiles) -> bytes:
    """Excelを作ってバイト列で返す。Cloudではファイルを残せないため。"""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schedule.xlsx"
        export_schedule(result, profiles, path)
        return path.read_bytes()


# --- スプレッドシートの読み取りを減らす ---------------------------------------
#
# Streamlit はマスを1つ触るたびに画面全体を作り直す。そのたびに読みに行くと、
# 1操作あたり数回のAPI呼び出しになり、Googleの上限(1分あたり60回)にすぐ達する。
# 実際に「Quota exceeded」で入力が止まる不具合が起きた。
#
# そこで一度読んだ内容は覚えておき、操作中はAPIを叩かないようにする。
# 覚えた内容を捨てるのは次の2つのときだけ:
#   - 「保存する」を押したとき(自分が書き換えたので読み直す)
#   - 「最新に更新」を押したとき(スプレッドシートを直接直した場合)


def _storage_cache_key(storage) -> str:
    """保存先を一意に区別するキー。

    クラス名だけだと、Googleスプレッドシートを別のものに切り替えても
    同じキーとみなされ、古いスタッフ情報がキャッシュから返り続けてしまう。
    保存先そのもの(スプレッドシートID、無ければ"local")を含める。
    """
    return f"{type(storage).__name__}:{getattr(storage, 'sheet_id', 'local')}"


@st.cache_data(show_spinner="スタッフ情報を読んでいます...")
def _fetch_staff(_storage, cache_key: str):
    """スタッフ情報をスプレッドシートから読む。結果は覚えておく。

    _storage は先頭にアンダースコアを付けて、Streamlit の照合対象から外す
    (接続オブジェクトは比較できないため)。かわりに cache_key で区別する。
    """
    return _storage.load_profiles()


@st.cache_data(show_spinner="希望を読んでいます...")
def _saved_requests(_storage, year: int, month: int):
    """その月の保存済みの希望を読む。結果は覚えておく。"""
    return _storage.load(year, month)


@st.cache_data(show_spinner=False)
def _cached_status(_storage, cache_key: str):
    """保存先の状態。画面の上に出すだけなので、毎回問い合わせない。

    cache_key で保存先を区別する(理由は _storage_cache_key を参照)。
    """
    return _storage.status()


def _staff(storage):
    """スタッフ情報。スプレッドシートに無ければ手元のファイルを使う。"""
    profiles = _fetch_staff(storage, _storage_cache_key(storage))
    return profiles or _load_profiles()


def _refresh_button() -> None:
    """スプレッドシートを直接直したときに、画面へ反映させるボタン。"""
    if st.button("最新に更新", help="スプレッドシートを直接直したときに押してください"):
        _fetch_staff.clear()
        _saved_requests.clear()
        _cached_status.clear()
        _load_profiles.clear()
        for key in [k for k in st.session_state if str(k).startswith("frame-")]:
            del st.session_state[key]
        st.rerun()


def _frame_key(year: int, month: int) -> str:
    return f"frame-{year}-{month}"


def _editor_frame(profiles, days, saved, year: int, month: int):
    """入力欄に渡す表。一度作ったら使い回す。

    画面が作り直されるたびに新しい表を渡すと、入力したばかりの内容が
    捨てられて「1回目が反映されない」ことがある。同じ表を渡し続ける。
    """
    key = _frame_key(year, month)
    # 表示していない月の表は捨てる。月を切り替えるたびに溜まらないようにする。
    for stale in [k for k in st.session_state if str(k).startswith("frame-") and k != key]:
        del st.session_state[stale]
    if key not in st.session_state:
        st.session_state[key] = _to_frame(profiles, days, saved)
    return st.session_state[key]


# --- 部品 ---------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _load_profiles():
    if not PROFILES_PATH.exists():
        return []
    return load_profiles(PROFILES_PATH)


def _secrets():
    """secrets.toml が無い環境では None を返す(手元で動かすとき)。

    st.secrets は存在しない場合、触れた時点で例外を投げる。
    ここで実際にキーを読んで確かめておく。
    """
    try:
        st.secrets.get("requests_sheet_id")
        return st.secrets
    except Exception:
        return None


def _pick_month(key: str = "wish"):
    today = date.today()
    default_year, default_month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    col1, col2 = st.columns(2)
    year = col1.number_input(
        "年", min_value=2020, max_value=2100, value=default_year, step=1, key=f"{key}-year"
    )
    month = col2.number_input(
        "月", min_value=1, max_value=12, value=default_month, step=1, key=f"{key}-month"
    )
    return int(year), int(month)


def _show_legend() -> None:
    with st.expander("記号の意味", expanded=False):
        lines = [f"**{code}** {start}-{end} に入りたい" for code, (start, end) in DAY_SHIFTS.items()]
        lines += [
            f"**{code}** {code} の時短で入りたい"
            for code in HOUR_CHOICES
        ]
        lines += [f"**{mark}** {text}" for mark, text in MEANING.items()]
        lines.append("**(空欄)** 希望なし。こちらで組みます")
        for chunk in [lines[i::3] for i in range(3)]:
            st.markdown("　/　".join(chunk))
        st.caption("夜勤は ○ → △ → 公 の3日、深夜は ◉ → 公 の2日が続きます。")
        st.caption(
            f"いちばん左の「{CARRY_LABEL}」には、"
            "**前月の最終日**に ○・△・◉ だった方だけ入れてください。"
            "前月から続く勤務を引き継ぐために使います(ふつうは5人)。"
        )
        st.caption(
            f"時短({' / '.join(HOUR_CHOICES)})は、勤務時間が決まっている方のためのものです。"
            "番号のシフトで組む方に選ぶと、作るときに知らせが出ます。"
        )


def _to_frame(profiles, days, saved) -> pd.DataFrame:
    rows = []
    for profile in profiles:
        entries = saved[profile.staff_id].entries if profile.staff_id in saved else {}
        # スタッフIDは同姓同名の職員を取り違えないための突き合わせ用。画面には出さない。
        row = {
            "スタッフID": profile.staff_id,
            "スタッフ": profile.name,
            CARRY_LABEL: entries.get(CARRY_OVER_DAY) or BLANK,
        }
        for day in days:
            row[_column(day)] = entries.get(day.day) or BLANK
        rows.append(row)
    return pd.DataFrame(rows)


def _column(day) -> str:
    return f"{day.day}\n{day.weekday_name}"


def _column_config(days):
    config = {
        "スタッフID": st.column_config.TextColumn("スタッフID"),
        "スタッフ": st.column_config.TextColumn("スタッフ", width="small", pinned=True),
        CARRY_LABEL: st.column_config.SelectboxColumn(
            CARRY_LABEL,
            options=[BLANK] + list(CARRY_OVER_CHOICES),
            width="small",
            help="前月の最終日の記号。前月から続く勤務を引き継ぐために使います",
            required=False,
        ),
    }
    for day in days:
        label = _column(day)
        config[label] = st.column_config.SelectboxColumn(
            f"{day.day}({day.weekday_name})",
            # NIGHT_AFTER(△) は選べる希望ではなく、_fill_night_after が
            # ○(夜勤)の翌日へ保存時に自動で書き込む値。ここに含めておかないと、
            # 保存後の再描画で SelectboxColumn が「選択肢に無い値」を持つ
            # セルを表示することになり、画面が壊れる。
            options=[BLANK] + list(WISH_CHOICES) + [NIGHT_AFTER],
            width="small",
            required=False,
        )
    return config


def _column_order(days):
    """画面に表示する列の順番。スタッフID列は突き合わせ専用なので表示しない。"""
    return ["スタッフ", CARRY_LABEL] + [_column(day) for day in days]


def _from_frame(frame, profiles, days):
    # 同姓同名の職員がいても取り違えないよう、名前ではなく staff_id で突き合わせる。
    by_id = {p.staff_id: p for p in profiles}
    requests = []
    for _, row in frame.iterrows():
        profile = by_id.get(row.get("スタッフID"))
        if profile is None:
            continue
        entries = {}
        carry = str(row.get(CARRY_LABEL, "") or "").strip()
        if carry and carry != BLANK:
            entries[CARRY_OVER_DAY] = carry
        for day in days:
            mark = str(row.get(_column(day), "") or "").strip()
            if mark and mark != BLANK:
                entries[day.day] = mark
        requests.append(
            StaffRequests(staff_id=profile.staff_id, name=profile.name, entries=entries)
        )
    return requests


def _fill_night_after(requests, days) -> int:
    """○(夜勤)の翌日に △(明け)を入れる。入れた数を返す。

    明けは希望ではなく、夜勤に入れば必ずそうなるもの。
    施設の担当者は勤務表を「○△」と並べて書いているので、
    入力画面でも同じ並びで見えたほうが確認しやすい。

    すでに何か入っている日は上書きしない。
    例えば ○ の翌日に 有 が入っていれば、そちらを尊重して知らせに任せる。
    """
    day_numbers = [day.day for day in days]
    last = day_numbers[-1] if day_numbers else 0
    added = 0
    for request in requests:
        for day in list(request.entries):
            if request.entries.get(day) != NIGHT_IN or day < 1 or day >= last:
                continue
            if request.entries.get(day + 1):
                continue  # すでに何か入っている日は触らない
            request.entries[day + 1] = NIGHT_AFTER
            added += 1
    return added


def _show_summary(frame, days) -> None:
    columns = [_column(day) for day in days]
    values = frame[columns].values.ravel()
    counts = {}
    for value in values:
        mark = str(value or "").strip()
        if mark and mark != BLANK:
            counts[mark] = counts.get(mark, 0) + 1
    if not counts:
        return
    st.caption(
        "入力済み: "
        + "　".join(f"{mark} {count}件" for mark, count in sorted(counts.items()))
    )


if __name__ == "__main__":
    main()
