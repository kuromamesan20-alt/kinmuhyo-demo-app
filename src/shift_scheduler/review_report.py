"""読み取り結果を人が確認するための一覧(HTML)を作る。

YAMLは機械向けの形式で確認しづらいため、
「顧客が書いた原文」と「こう読み取りました」を並べて見せる。
"""

from __future__ import annotations

import html
from typing import List, Sequence, Tuple

from .models import WEEKDAY_NAMES, StaffProfile
from .shifts import DAY_SHIFT_CODES

Row = Tuple[str, str]


# --- 読み取り結果を日本語の文にする -------------------------------------------


def describe(profile: StaffProfile) -> List[Row]:
    """StaffProfile を「項目名, 読み取った内容」の一覧にする。"""
    rows: List[Row] = []

    def add(label: str, value: str) -> None:
        if value:
            rows.append((label, value))

    add("週の勤務日数", f"{profile.weekly_work_days}日" if profile.weekly_work_days else "")
    if profile.work_hours:
        add("勤務時間", f"{profile.work_hours[0]}-{profile.work_hours[1]}(番号は使わない)")
    add("入れる日勤帯", _day_shift_text(profile))
    add("夜勤・深夜", _night_text(profile))
    add("夜勤の回数", _count_text(profile.night_shift_count))
    add("深夜の回数", _count_text(profile.late_night_shift_count))
    add("毎週の固定休み", _weekday_text(profile.fixed_off_weekdays))
    add(
        "月ごとの休み",
        "、".join(
            f"{WEEKDAY_NAMES[weekday]}曜に月{count}回"
            for weekday, count in sorted(profile.monthly_off_quota.items())
        ),
    )
    if profile.weekend_off_either:
        add("土日", "どちらかは休みたい(希望)")
    add(
        "曜日ごとの勤務時間",
        "／".join(
            f"{WEEKDAY_NAMES[slot.weekday]} {slot.start}-{slot.end}"
            for slot in profile.fixed_time_slots
        ),
    )
    add("夜勤に入る曜日", _weekday_text(profile.night_weekdays))
    add("勤務する曜日(固定)", _weekday_text(profile.fixed_work_weekdays))
    add("勤務形態の限定", _exclusive_text(profile))
    add("日勤責任者", profile.day_responsible or "")
    for constraint in profile.pair_constraints:
        if constraint.kind == "no_pair_night":
            add("夜勤で同席不可", constraint.other_staff)
        else:
            add(
                "夜勤で同席できる回数",
                f"{constraint.other_staff} とは月{constraint.max_count}回まで",
            )
    if profile.preferred_night:
        add("本人の希望", "夜勤・深夜を希望している")
    if profile.avoid_early:
        add("運用上の配慮", "早出には入れていない")
    if profile.random_days_allowed:
        add("勤務日", "曜日は作成側で決めてよい")
    return rows


def _day_shift_text(profile: StaffProfile) -> str:
    if profile.writes_own_hours and not profile.available_day_shifts:
        return "番号のシフトには入らない(時間を直接記入)"
    if not profile.available_day_shifts:
        return "なし"
    if len(profile.available_day_shifts) == len(DAY_SHIFT_CODES):
        return "すべて可"
    excluded = [c for c in DAY_SHIFT_CODES if c not in profile.available_day_shifts]
    return f"{'・'.join(profile.available_day_shifts)}({'・'.join(excluded)}は不可)"


def _night_text(profile: StaffProfile) -> str:
    parts = []
    parts.append(f"夜勤{'可' if profile.can_night else '不可'}")
    parts.append(f"深夜{'可' if profile.can_late_night else '不可'}")
    return "・".join(parts)


def _count_text(value) -> str:
    if not value:
        return ""
    low, high = value
    return f"月{low}回" if low == high else f"月{low}〜{high}回"


def _weekday_text(weekdays: Sequence[int]) -> str:
    return "・".join(f"{WEEKDAY_NAMES[w]}曜" for w in weekdays)


def _exclusive_text(profile: StaffProfile) -> str:
    return "・".join(
        name
        for name, value in (
            ("夜勤専従", profile.night_shift_exclusive),
            ("深夜のみ", profile.late_night_only),
            ("日勤のみ", profile.day_shift_only),
        )
        if value
    )


# --- HTML ---------------------------------------------------------------------


def render_html(profiles: Sequence[StaffProfile], case_name: str) -> str:
    review_count = sum(1 for p in profiles if p.needs_review)
    cards = "\n".join(_render_card(p, i) for i, p in enumerate(profiles, 1))
    return _TEMPLATE.format(
        case_name=html.escape(case_name),
        total=len(profiles),
        review_count=review_count,
        ok_count=len(profiles) - review_count,
        cards=cards,
    )


def _render_card(profile: StaffProfile, index: int) -> str:
    esc = html.escape
    state = "review" if profile.needs_review else "ok"
    badge = "要確認" if profile.needs_review else "確認不要"

    raw_parts = [
        (label, text)
        for label, text in (("固定条件", profile.raw_conditions), ("備考", profile.raw_notes))
        if text.strip()
    ]
    raw_html = (
        "\n".join(
            f'<div class="raw-block"><span class="raw-label">{esc(label)}</span>'
            f"<p>{esc(text).replace(chr(10), '<br>')}</p></div>"
            for label, text in raw_parts
        )
        or '<p class="empty">記載なし</p>'
    )

    rows = describe(profile)
    rows_html = (
        "\n".join(
            f'<div class="row"><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>'
            for label, value in rows
        )
        or '<p class="empty">条件の指定なし</p>'
    )

    reasons_html = ""
    if profile.review_reasons:
        items = "\n".join(f"<li>{esc(r)}</li>" for r in profile.review_reasons)
        reasons_html = f'<div class="reasons"><h4>確認してほしいこと</h4><ul>{items}</ul></div>'

    return f"""<article class="card" data-state="{state}">
  <header class="card-head">
    <span class="num">{index}</span>
    <h3>{esc(profile.name)}</h3>
    <span class="chip">{esc(profile.role)}</span>
    <span class="chip">{esc(profile.employment_type)}</span>
    <span class="badge badge-{state}">{badge}</span>
  </header>
  <div class="card-body">
    <section class="pane">
      <h4>顧客が書いた文</h4>
      {raw_html}
    </section>
    <section class="pane">
      <h4>こう読み取りました</h4>
      <dl class="rows">{rows_html}</dl>
    </section>
  </div>
  {reasons_html}
</article>"""


_TEMPLATE = """<title>スタッフ勤務条件の読み取り結果</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=Shippori+Mincho+B1:wght@600&display=swap">
<style>
  :root {{
    --ground: #F4F6F8;
    --surface: #FFFFFF;
    --surface-sunken: #EDF0F4;
    --line: #D9DEE5;
    --ink: #1A1E25;
    --ink-soft: #5B6572;
    --accent: #2F5D8C;
    --accent-soft: #E8EFF6;
    --attention: #A8631A;
    --attention-soft: #FBF1E3;
    --ok: #3B7359;
    --shadow: 0 1px 2px rgba(26, 30, 37, .06), 0 8px 24px rgba(26, 30, 37, .05);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground: #14171C;
      --surface: #1C2128;
      --surface-sunken: #232932;
      --line: #333B46;
      --ink: #E4E8EE;
      --ink-soft: #97A1AE;
      --accent: #7FAEDC;
      --accent-soft: #1E2B39;
      --attention: #E0A25E;
      --attention-soft: #2B2318;
      --ok: #7FBF9C;
      --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 8px 24px rgba(0, 0, 0, .25);
    }}
  }}
  :root[data-theme="dark"] {{
    --ground: #14171C;
    --surface: #1C2128;
    --surface-sunken: #232932;
    --line: #333B46;
    --ink: #E4E8EE;
    --ink-soft: #97A1AE;
    --accent: #7FAEDC;
    --accent-soft: #1E2B39;
    --attention: #E0A25E;
    --attention-soft: #2B2318;
    --ok: #7FBF9C;
    --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 8px 24px rgba(0, 0, 0, .25);
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: "Noto Sans JP", system-ui, -apple-system, sans-serif;
    font-size: 15px;
    line-height: 1.75;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 48px 20px 96px; }}

  header.page {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 28px; }}
  .eyebrow {{
    font-size: 12px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--accent); font-weight: 700;
  }}
  h1 {{
    font-family: "Shippori Mincho B1", "Noto Sans JP", serif;
    font-weight: 600; font-size: clamp(26px, 4vw, 36px);
    margin: 0; letter-spacing: .02em; text-wrap: balance;
  }}
  .lede {{ color: var(--ink-soft); margin: 0; max-width: 62ch; }}

  .stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 10px; }}
  .stat {{
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 18px; display: flex; align-items: baseline; gap: 10px;
    box-shadow: var(--shadow);
  }}
  .stat b {{ font-size: 24px; font-variant-numeric: tabular-nums; line-height: 1; }}
  .stat span {{ font-size: 13px; color: var(--ink-soft); }}
  .stat.attention b {{ color: var(--attention); }}
  .stat.ok b {{ color: var(--ok); }}

  .filters {{ display: flex; gap: 8px; margin: 20px 0 26px; flex-wrap: wrap; }}
  .filters button {{
    font: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
    background: var(--surface); color: var(--ink-soft);
    border: 1px solid var(--line); border-radius: 999px; padding: 7px 16px;
    transition: background .15s, color .15s, border-color .15s;
  }}
  .filters button:hover {{ border-color: var(--accent); color: var(--accent); }}
  .filters button[aria-pressed="true"] {{
    background: var(--accent); border-color: var(--accent); color: var(--surface);
  }}
  :root[data-theme="dark"] .filters button[aria-pressed="true"],
  .filters button[aria-pressed="true"] {{ color: var(--ground); }}
  .filters button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  .list {{ display: flex; flex-direction: column; gap: 16px; }}

  .card {{
    background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
    box-shadow: var(--shadow); overflow: hidden;
  }}
  .card[data-state="review"] {{ border-left: 4px solid var(--attention); }}
  .card[hidden] {{ display: none; }}

  .card-head {{
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 14px 20px; border-bottom: 1px solid var(--line);
    background: var(--surface-sunken);
  }}
  .num {{
    font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-soft);
    min-width: 22px;
  }}
  .card-head h3 {{ margin: 0; font-size: 17px; font-weight: 700; }}
  .chip {{
    font-size: 12px; color: var(--ink-soft);
    border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px;
  }}
  .badge {{
    margin-left: auto; font-size: 12px; font-weight: 700;
    border-radius: 6px; padding: 3px 10px;
  }}
  .badge-review {{ background: var(--attention-soft); color: var(--attention); }}
  .badge-ok {{ background: var(--accent-soft); color: var(--ok); }}

  .card-body {{ display: grid; grid-template-columns: 1fr 1fr; }}
  .pane {{ padding: 18px 20px; }}
  .pane + .pane {{ border-left: 1px solid var(--line); }}
  .pane h4 {{
    margin: 0 0 12px; font-size: 11px; font-weight: 700;
    letter-spacing: .1em; color: var(--ink-soft); text-transform: uppercase;
  }}
  .raw-block + .raw-block {{ margin-top: 12px; }}
  .raw-label {{
    display: inline-block; font-size: 11px; font-weight: 700; color: var(--accent);
    background: var(--accent-soft); border-radius: 4px; padding: 1px 7px; margin-bottom: 4px;
  }}
  .raw-block p {{ margin: 0; white-space: pre-wrap; }}
  .empty {{ margin: 0; color: var(--ink-soft); font-size: 14px; }}

  .rows {{ margin: 0; display: flex; flex-direction: column; gap: 7px; }}
  .row {{ display: grid; grid-template-columns: 132px 1fr; gap: 12px; align-items: baseline; }}
  .row dt {{ font-size: 12px; color: var(--ink-soft); }}
  .row dd {{ margin: 0; font-size: 14px; font-variant-numeric: tabular-nums; }}

  .reasons {{
    background: var(--attention-soft); border-top: 1px solid var(--line);
    padding: 14px 20px;
  }}
  .reasons h4 {{
    margin: 0 0 8px; font-size: 11px; font-weight: 700;
    letter-spacing: .1em; color: var(--attention); text-transform: uppercase;
  }}
  .reasons ul {{ margin: 0; padding-left: 20px; display: flex; flex-direction: column; gap: 5px; }}
  .reasons li {{ font-size: 14px; }}

  @media (max-width: 720px) {{
    .card-body {{ grid-template-columns: 1fr; }}
    .pane + .pane {{ border-left: none; border-top: 1px solid var(--line); }}
    .row {{ grid-template-columns: 110px 1fr; }}
  }}
</style>

<div class="wrap">
  <header class="page">
    <span class="eyebrow">{case_name}</span>
    <h1>スタッフ勤務条件の読み取り結果</h1>
    <p class="lede">
      顧客からのCSVに書かれていた文を、勤務表を組むための条件として読み取った結果です。
      左が原文、右が読み取った内容。推測で埋めずに済まなかった箇所は「要確認」にしています。
    </p>
  </header>

  <div class="stats">
    <div class="stat"><b>{total}</b><span>名</span></div>
    <div class="stat attention"><b>{review_count}</b><span>名は要確認</span></div>
    <div class="stat ok"><b>{ok_count}</b><span>名はそのまま使える</span></div>
  </div>

  <div class="filters" role="group" aria-label="表示の絞り込み">
    <button type="button" data-filter="all" aria-pressed="true">全員</button>
    <button type="button" data-filter="review" aria-pressed="false">要確認だけ</button>
    <button type="button" data-filter="ok" aria-pressed="false">確認不要だけ</button>
  </div>

  <div class="list">
{cards}
  </div>
</div>

<script>
  var buttons = document.querySelectorAll(".filters button");
  var cards = document.querySelectorAll(".card");
  buttons.forEach(function (button) {{
    button.addEventListener("click", function () {{
      var filter = button.dataset.filter;
      buttons.forEach(function (other) {{
        other.setAttribute("aria-pressed", String(other === button));
      }});
      cards.forEach(function (card) {{
        card.hidden = filter !== "all" && card.dataset.state !== filter;
      }});
    }});
  }});
</script>
"""
