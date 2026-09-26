#!/usr/bin/env python3
"""勤務表作成ツール CLI。

  python cli.py extract-profiles --input <CSV> --out <YAML>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).parent / "src"))

from shift_scheduler.csv_loader import CsvFormatError, load_staff_rows  # noqa: E402
from shift_scheduler.profile_builder import build_profiles  # noqa: E402
from shift_scheduler.excel_review import (  # noqa: E402
    ReviewSheetError,
    export_review_sheet,
    import_review_sheet,
)
from shift_scheduler.customer_sheet import (  # noqa: E402
    AnswerSheetError,
    export_question_sheet,
    import_answer_sheet,
)
from shift_scheduler.profile_store import (  # noqa: E402
    load_profiles,
    load_requests,
    save_profiles,
    save_requests,
)
from shift_scheduler.request_sheet import (  # noqa: E402
    RequestSheetError,
    export_request_sheet,
    import_request_sheet,
)
from shift_scheduler.questions import apply_answers, build_questions  # noqa: E402
from shift_scheduler.review_report import render_html  # noqa: E402
from shift_scheduler.facility import facility  # noqa: E402
from shift_scheduler.excel_export import export_schedule  # noqa: E402
from shift_scheduler.scheduler import build_schedule  # noqa: E402
from shift_scheduler.shifts import DEFAULT_MONTHLY_OFF_DAYS  # noqa: E402
from shift_scheduler.request_storage import GoogleSheetStorage  # noqa: E402


def _read_secrets(path: Path):
    """Streamlitに貼ったのと同じTOMLを読む。読めなければ理由を返す。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9/3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return None, (
                "TOMLを読む部品がありません。次を実行してください:\n"
                "  ./venv/bin/pip install tomli"
            )
    if not path.exists():
        return None, f"ファイルが見つかりません: {path}"
    with path.open("rb") as handle:
        secrets = tomllib.load(handle)
    if not secrets.get("requests_sheet_id") or not secrets.get("gcp_service_account"):
        return None, "secrets に requests_sheet_id と gcp_service_account が要ります"
    return secrets, None


def _cloud_storage(secrets):
    return GoogleSheetStorage(
        sheet_id=str(secrets["requests_sheet_id"]),
        credentials=dict(secrets["gcp_service_account"]),
    )


def cmd_extract_profiles(args: argparse.Namespace) -> int:
    csv_path = Path(args.input)
    if not csv_path.exists():
        print(f"エラー: CSVが見つかりません: {csv_path}", file=sys.stderr)
        return 1

    try:
        rows = load_staff_rows(csv_path)
    except CsvFormatError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    profiles = build_profiles(rows)
    out_path = Path(args.out)
    save_profiles(profiles, out_path)

    needs_review = [p for p in profiles if p.needs_review]
    print(f"スタッフ {len(profiles)} 名を読み込みました → {out_path}")
    print(f"うち要確認: {len(needs_review)} 名")
    for profile in needs_review:
        print(f"\n● {profile.name}")
        for reason in profile.review_reasons:
            print(f"    - {reason}")
    if needs_review:
        print(
            f"\nYAMLを開いて上記を確認・修正し、"
            f"ファイル名から _draft を外して保存してください。"
        )
    return 0


def cmd_review_list(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"エラー: YAMLが見つかりません: {profiles_path}", file=sys.stderr)
        return 1

    profiles = load_profiles(profiles_path)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(profiles, args.case_name), encoding="utf-8")

    review_count = sum(1 for p in profiles if p.needs_review)
    print(f"一覧を書き出しました → {out_path}")
    print(f"{len(profiles)} 名中 {review_count} 名が要確認です。")
    return 0


def cmd_export_review(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"エラー: YAMLが見つかりません: {profiles_path}", file=sys.stderr)
        return 1

    profiles = load_profiles(profiles_path)
    out_path = Path(args.out)
    export_review_sheet(profiles, out_path)

    review_count = sum(1 for p in profiles if p.needs_review)
    print(f"確認用のExcelを書き出しました → {out_path}")
    print(f"{len(profiles)} 名中 {review_count} 名が要確認です。")
    print("白い列を直し、直した行の「確認済み」を「はい」にして保存してください。")
    return 0


def cmd_import_review(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    review_path = Path(args.review)
    for path in (profiles_path, review_path):
        if not path.exists():
            print(f"エラー: ファイルが見つかりません: {path}", file=sys.stderr)
            return 1

    profiles = load_profiles(profiles_path)
    try:
        updated, warnings = import_review_sheet(profiles, review_path)
    except ReviewSheetError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    save_profiles(updated, out_path)

    remaining = [p for p in updated if p.needs_review]
    print(f"Excelの内容を反映しました → {out_path}")
    if warnings:
        print(f"\n読み取れなかった記入が {len(warnings)} 件あります:")
        for warning in warnings:
            print(f"    - {warning}")
        print("  ※ これらは反映されていません。Excelを直して、もう一度実行してください。")
    print(f"\n残りの要確認: {len(remaining)} 名")
    for profile in remaining:
        print(f"    - {profile.name}")
    return 0


def cmd_export_questions(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"エラー: YAMLが見つかりません: {profiles_path}", file=sys.stderr)
        return 1

    profiles = load_profiles(profiles_path)
    questions = build_questions(profiles)
    if not questions:
        print("確認したいことはありません。お客様への確認は不要です。")
        return 0

    out_path = Path(args.out)
    export_question_sheet(questions, out_path, args.case_name)
    staff_count = len({q.staff_name for q in questions})
    print(f"お客様への確認シートを書き出しました → {out_path}")
    print(f"質問 {len(questions)} 件 / スタッフ {staff_count} 名分")
    print("Googleスプレッドシートにアップロードして共有してください。")
    return 0


def cmd_import_answers(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    answers_path = Path(args.answers)
    for path in (profiles_path, answers_path):
        if not path.exists():
            print(f"エラー: ファイルが見つかりません: {path}", file=sys.stderr)
            return 1

    profiles = load_profiles(profiles_path)
    try:
        answers = import_answer_sheet(answers_path)
    except AnswerSheetError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    summary = apply_answers(profiles, answers)
    out_path = Path(args.out)
    save_profiles(profiles, out_path)

    print(f"お客様の回答 {len(answers)} 件を取り込みました → {out_path}")
    for line in summary:
        print(f"    - {line}")

    remaining = [p for p in profiles if p.needs_review]
    print(f"\n残りの要確認: {len(remaining)} 名")
    print("「はい」以外の回答は自動では反映していません。")
    print("回答の内容を見て、確認用Excelで直してください。")
    return 0


def cmd_export_requests(args: argparse.Namespace) -> int:
    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"エラー: YAMLが見つかりません: {profiles_path}", file=sys.stderr)
        return 1

    profiles = load_profiles(profiles_path)
    existing = []
    carry_path = None
    if args.carry_over:
        carry_path = Path(args.carry_over)
        if not carry_path.exists():
            print(f"エラー: 引き継ぎ元が見つかりません: {carry_path}", file=sys.stderr)
            return 1
        existing, _ = import_request_sheet(carry_path)

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        same_as_carry_over = carry_path is not None and out_path.resolve() == carry_path.resolve()
        if not same_as_carry_over:
            try:
                out_requests, _ = import_request_sheet(out_path)
            except RequestSheetError:
                out_requests = []
            if any(r.entries for r in out_requests):
                print(
                    "エラー: 出力先には記入済みの希望シートがあります。"
                    "内容を引き継ぐなら --carry-over に同じファイルを指定してください。"
                    "捨ててよいなら --force を付けてください。",
                    file=sys.stderr,
                )
                return 1

    try:
        discarded = export_request_sheet(profiles, args.year, args.month, out_path, existing)
    except RequestSheetError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    print(f"{args.year}年{args.month}月の希望入力シートを作りました → {out_path}")
    print(f"スタッフ {len(profiles)} 名分。施設の担当者に共有してください。")
    if discarded:
        print(f"\n引き継げなかった記入が {len(discarded)} 件あります(この月には無い日のため):")
        for item in discarded:
            print(f"    - {item.name} {item.day}日「{item.mark}」")
    return 0


def cmd_import_requests(args: argparse.Namespace) -> int:
    requests_path = Path(args.requests)
    if not requests_path.exists():
        print(f"エラー: ファイルが見つかりません: {requests_path}", file=sys.stderr)
        return 1

    try:
        requests, warnings = import_request_sheet(requests_path)
    except RequestSheetError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    structure_warnings: List[str] = []
    if getattr(args, "profiles", None):
        profiles_path = Path(args.profiles)
        if not profiles_path.exists():
            print(f"エラー: YAMLが見つかりません: {profiles_path}", file=sys.stderr)
            return 1
        profiles = load_profiles(profiles_path)
        profile_by_id = {p.staff_id: p for p in profiles}
        request_by_id = {r.staff_id: r for r in requests}

        for staff_id, profile in profile_by_id.items():
            if staff_id not in request_by_id:
                structure_warnings.append(f"スタッフ{profile.name}の行がシートにありません")
        for staff_id, request in request_by_id.items():
            if staff_id not in profile_by_id:
                structure_warnings.append(
                    f"シートのスタッフ{request.name}(ID:{staff_id})はプロフィールにいません"
                )

    out_path = Path(args.out)
    save_requests(requests, out_path)

    filled = [r for r in requests if r.entries]
    print(f"希望を読み込みました → {out_path}")
    print(f"スタッフ {len(requests)} 名中 {len(filled)} 名に記入があります。")
    for request in filled:
        off = request.wish_off_days()
        work = request.wish_work_days()
        parts = []
        if off:
            parts.append(f"希望休 {'・'.join(str(d) for d in off)}日")
        if work:
            parts.append(
                "希望出勤 " + "・".join(f"{d}日={m}" for d, m in work.items())
            )
        print(f"    {request.name}: {' / '.join(parts)}")
    if warnings:
        print(f"\n読み取れなかった記入が {len(warnings)} 件あります:")
        for warning in warnings:
            print(f"    - {warning}")
    if structure_warnings:
        print(f"\n行の過不足が {len(structure_warnings)} 件あります:")
        for warning in structure_warnings:
            print(f"    - {warning}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    """月次勤務表を組む。

    --secrets を渡すと、Webアプリと同じスプレッドシートから
    スタッフ情報と希望を読む。渡さなければ手元のファイルを使う。
    """
    profiles = None
    requests = []

    if args.secrets:
        secrets, error = _read_secrets(Path(args.secrets))
        if error:
            print(f"エラー: {error}", file=sys.stderr)
            return 1
        storage = _cloud_storage(secrets)
        print("スプレッドシートから読み込んでいます...")
        profiles = storage.load_profiles()
        if not profiles:
            print(
                "エラー: スプレッドシートにスタッフ情報がありません。\n"
                "  先に upload-profiles で書き出してください。",
                file=sys.stderr,
            )
            return 1
        requests = storage.load(args.year, args.month)
        filled = [r for r in requests if r.entries]
        print(f"  スタッフ {len(profiles)} 名 / 希望の記入 {len(filled)} 名分")
        if not filled:
            print("  ※ その月の希望はまだ1件も入っていません。")

    if profiles is None:
        profiles_path = Path(args.profiles) if args.profiles else None
        if not profiles_path or not profiles_path.exists():
            print(
                "エラー: --profiles か --secrets のどちらかを指定してください",
                file=sys.stderr,
            )
            return 1
        profiles = load_profiles(profiles_path)
        if args.requests:
            requests_path = Path(args.requests)
            if not requests_path.exists():
                print(f"エラー: 希望が見つかりません: {requests_path}", file=sys.stderr)
                return 1
            requests = load_requests(requests_path)

    result = build_schedule(
        profiles,
        requests,
        args.year,
        args.month,
        args.time_limit,
        monthly_off_days=args.off_days,
    )
    print(f"{args.year}年{args.month}月: {result.status}")
    for message in result.messages:
        print(f"    - {message}")
    if not result.ok:
        return 1

    out_path = Path(args.out)
    export_schedule(result, profiles, out_path)
    print(f"\n勤務表を書き出しました → {out_path}")
    print(f"  {len(result.assignments)} 名 / 責任者「せ」{len(result.responsible)} 日")
    return 0


def cmd_upload_profiles(args: argparse.Namespace) -> int:
    """スタッフ情報をGoogleスプレッドシートに書き出す。

    Webアプリは個人情報をGitHubに置かないため、スタッフ情報も
    スプレッドシート側に持たせる。その1回きりの書き出し。
    """
    profiles_path = Path(args.profiles)
    secrets_path = Path(args.secrets)
    for path in (profiles_path, secrets_path):
        if not path.exists():
            print(f"エラー: ファイルが見つかりません: {path}", file=sys.stderr)
            return 1

    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9/3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            print(
                "エラー: TOMLを読む部品がありません。次を実行してください:\n"
                "  ./venv/bin/pip install tomli",
                file=sys.stderr,
            )
            return 1

    with secrets_path.open("rb") as handle:
        secrets = tomllib.load(handle)

    sheet_id = secrets.get("requests_sheet_id")
    account = secrets.get("gcp_service_account")
    if not sheet_id or not account:
        print(
            "エラー: secrets に requests_sheet_id と gcp_service_account が要ります",
            file=sys.stderr,
        )
        return 1

    profiles = load_profiles(profiles_path)
    storage = GoogleSheetStorage(sheet_id=str(sheet_id), credentials=dict(account))
    storage.save_profiles(profiles)

    print(f"スタッフ {len(profiles)} 名をスプレッドシートに書き出しました。")
    print("Webアプリを再読み込みすると反映されます。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="勤務表作成ツール")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract-profiles", help="スタッフ条件CSVから構造化YAML(ドラフト)を作る"
    )
    extract.add_argument("--input", required=True, help="スタッフ条件CSVのパス")
    extract.add_argument("--out", required=True, help="出力するドラフトYAMLのパス")
    extract.set_defaults(func=cmd_extract_profiles)

    review = subparsers.add_parser(
        "review-list", help="読み取り結果を人が確認するための一覧(HTML)を作る"
    )
    review.add_argument("--profiles", required=True, help="構造化YAMLのパス")
    review.add_argument("--out", required=True, help="出力するHTMLのパス")
    review.add_argument("--case-name", default=facility().case_name, help="ページに表示する案件名")
    review.set_defaults(func=cmd_review_list)

    export = subparsers.add_parser(
        "export-review", help="確認・修正用のExcelシートを書き出す"
    )
    export.add_argument("--profiles", required=True, help="構造化YAMLのパス")
    export.add_argument("--out", required=True, help="出力するExcelのパス")
    export.set_defaults(func=cmd_export_review)

    importer = subparsers.add_parser(
        "import-review", help="修正したExcelの内容をYAMLに反映する"
    )
    importer.add_argument("--profiles", required=True, help="元になる構造化YAMLのパス")
    importer.add_argument("--review", required=True, help="修正したExcelのパス")
    importer.add_argument("--out", required=True, help="反映後のYAMLの出力先")
    importer.set_defaults(func=cmd_import_review)

    questions = subparsers.add_parser(
        "export-questions", help="お客様にお渡しする確認シートを書き出す"
    )
    questions.add_argument("--profiles", required=True, help="構造化YAMLのパス")
    questions.add_argument("--out", required=True, help="出力するExcelのパス")
    questions.add_argument("--case-name", default="", help="シートに表示する案件名")
    questions.set_defaults(func=cmd_export_questions)

    answers = subparsers.add_parser(
        "import-answers", help="お客様が記入した確認シートを取り込む"
    )
    answers.add_argument("--profiles", required=True, help="元になる構造化YAMLのパス")
    answers.add_argument("--answers", required=True, help="記入済みシート(.xlsx か .csv)")
    answers.add_argument("--out", required=True, help="取り込み後のYAMLの出力先")
    answers.set_defaults(func=cmd_import_answers)

    requests_out = subparsers.add_parser(
        "export-requests", help="その月の希望休・希望出勤の入力シートを作る"
    )
    requests_out.add_argument("--profiles", required=True, help="構造化YAMLのパス")
    requests_out.add_argument("--year", type=int, required=True)
    requests_out.add_argument("--month", type=int, required=True)
    requests_out.add_argument("--out", required=True, help="出力するExcelのパス")
    requests_out.add_argument(
        "--carry-over", help="前月までの記入済みシート(内容を引き継ぐ場合)"
    )
    requests_out.add_argument(
        "--force",
        action="store_true",
        help="出力先に記入済みシートがあっても強制的に上書きする",
    )
    requests_out.set_defaults(func=cmd_export_requests)

    requests_in = subparsers.add_parser(
        "import-requests", help="記入された希望シートを読み込む"
    )
    requests_in.add_argument("--requests", required=True, help="記入済みシート(.xlsx)")
    requests_in.add_argument("--out", required=True, help="読み込んだ希望の保存先(YAML)")
    requests_in.add_argument(
        "--profiles", help="構造化YAMLのパス(行の過不足を警告したい場合)"
    )
    requests_in.set_defaults(func=cmd_import_requests)

    generate = subparsers.add_parser("generate", help="月次勤務表を組んでExcelに出す")
    generate.add_argument("--profiles", help="構造化YAMLのパス(--secrets を使う場合は不要)")
    generate.add_argument("--requests", help="その月の希望(YAML)。無くても組める")
    generate.add_argument(
        "--secrets",
        help="Webアプリと同じスプレッドシートから読む場合のTOMLファイル",
    )
    generate.add_argument("--year", type=int, required=True)
    generate.add_argument("--month", type=int, required=True)
    generate.add_argument("--out", required=True, help="出力するExcelのパス")
    generate.add_argument("--time-limit", type=float, default=120.0, help="計算の制限時間(秒)")
    generate.add_argument(
        "--off-days",
        type=int,
        default=DEFAULT_MONTHLY_OFF_DAYS,
        help="正職員の公休の日数。ふつうは9日。0にすると縛らない",
    )
    generate.set_defaults(func=cmd_generate)

    upload = subparsers.add_parser(
        "upload-profiles", help="スタッフ情報をGoogleスプレッドシートに書き出す"
    )
    upload.add_argument("--profiles", required=True, help="構造化YAMLのパス")
    upload.add_argument(
        "--secrets", required=True, help="Streamlitに貼ったのと同じ内容のTOMLファイル"
    )
    upload.set_defaults(func=cmd_upload_profiles)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
