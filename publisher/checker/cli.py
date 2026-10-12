"""Checker の引数解析、実行前提検証、結果表示を提供します。"""

import argparse
from enum import IntEnum
from pathlib import Path
import sys

from checker.check_pending import check_pending
from checker.confirm_pending_update import confirm_pending_update
from checker.commit_readiness import CommitReadiness
from checker.create_observation import CreateResult, load_configured_user_id
from checker.initialization import initialize
from checker.local_state import read_ledger
from converter.git_inspection import GuardError, identify_worktrees


class ExitCode(IntEnum):
  READY = 0
  NOT_READY = 1
  ERROR = 2
  INTERRUPTED = 130


def _article_root(value: str) -> Path:
  if not value:
    raise argparse.ArgumentTypeError("記事 repository のパスを指定してください。")
  return Path(value)


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="python -m checker", allow_abbrev=False,
    description="明示された記事 repository の公開結果を一度だけ確認します。")
  commands = parser.add_subparsers(dest="command", required=True)
  check = commands.add_parser("check", allow_abbrev=False, help="公開結果と commit readiness を確認します。")
  check.add_argument("--article-root", required=True, type=_article_root, metavar="PATH",
                     help="記事 repository の authoring worktree root（必須）")
  init = commands.add_parser("init", allow_abbrev=False, help="Zenn owner と初期管理状態を設定します。")
  init.add_argument("username", metavar="USERNAME", help="初期化する Zenn username")
  init.add_argument("--article-root", required=True, type=_article_root, metavar="PATH",
                    help="記事 repository の authoring worktree root（必須）")
  confirm = commands.add_parser(
    "confirm-pending-update", allow_abbrev=False,
    help="ユーザーの明示的確認により PENDING UPDATE を手動確定します。",
    description="通常 check より強いユーザー責任を伴います。Dashboard の deploy 成功完了と、"
                "現在の remote content と deploy artifact の一致をユーザー自身が検証してください。"
                "Checker はこれらを検証しません。")
  confirm.add_argument("article_id", metavar="ARTICLE_ID", help="対象 pending UPDATE の UUID")
  confirm.add_argument("--article-root", required=True, type=_article_root, metavar="PATH",
                       help="記事 repository の authoring worktree root（必須）")
  confirm.add_argument("--confirmed-deploy-success", action="store_true", required=True,
                       help="ユーザーが Dashboard で対象 deploy の成功完了を確認済み（必須）")
  confirm.add_argument("--confirmed-content-match", action="store_true", required=True,
                       help="ユーザーが現在の remote content と deploy artifact の一致を確認済み（必須）")
  return parser


def _init(args: argparse.Namespace) -> int:
  """初期化結果と host 側の完了手順を表示します。失敗時は管理状態を保持します。"""
  try:
    outcome = initialize(args.article_root, args.username)
    if outcome.changed:
      print(f"初期管理状態を作成しました。Zenn user ID: {outcome.user.id}")
      print("Git commit は未実行です。ホストで deploy worktree の Git status と差分を確認し、"
            "config.json と ledger.json を stage して内容を確認してください。\n"
            "初回 commit message: Initialize Zenn management state [skip ci]\n"
            "初回 commit 後に deploy branch を push し、deploy worktree が clean になってから"
            "通常 workflow を開始してください。")
    else:
      print(f"ALREADY_INITIALIZED: 同一 Zenn user ID {outcome.user.id} のため no-op です。"
            "管理ファイルは変更していません。")
    print("初期化は Zenn deployment 成功の確認ではありません。")
    return ExitCode.READY
  except KeyboardInterrupt:
    print("INTERRUPTED: 初期化を中断しました。", file=sys.stderr)
    code = ExitCode.INTERRUPTED
  except (OSError, ValueError, TypeError) as exc:
    print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    code = ExitCode.ERROR
  except Exception as exc:
    print(f"ERROR: {type(exc).__name__}: 初期化を完了できません。", file=sys.stderr)
    code = ExitCode.ERROR
  print("管理ファイルの部分変更が残っている可能性があります。ホストで deploy worktree の"
        "Git status、staged/unstaged diff、untracked paths、ledger.json と config.json を確認してください。"
        "不完全な状態を commit/push しないでください。\n"
        "config 不在で canonical empty ledger のみが残った場合は init を再実行できます。"
        "既存の管理ファイルが欠落・破損している場合は、Git からの復旧を手動で行ってください。",
        file=sys.stderr)
  return code


def _recovery() -> None:
  print("管理変更を commit/push しないでください。ホストで deploy worktree の Git status、"
        "staged/unstaged diff、untracked paths、config.json、ledger.json の pending_next_state、"
        "released snapshots と記事 artifact を手動で確認し、復旧してください。"
        "復旧後に one-shot check を再実行できます。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
  """引数を解析して init、check または手動 UPDATE 確定を実行します。

  記事 root は必須です。init/check の成功は 0、check の NOT_READY は 1、
  手動 UPDATE 確定の成功は readiness を確定せず 0、
  判定不能な失敗は 2、中断は 130 を返します。help と引数エラーは SystemExit を送出します。
  """
  started = False
  args = None
  try:
    raw_args = sys.argv[1:] if argv is None else argv
    try:
      args = _parser().parse_args(raw_args)
    except SystemExit as exc:
      if exc.code == ExitCode.ERROR and raw_args and raw_args[0] == "confirm-pending-update":
        print("通常 check より強いユーザー責任を伴います。Dashboard の対象 deploy 成功完了と"
              "現在の remote content と deploy artifact の一致をユーザー自身が検証し、"
              "両確認オプションを明示してください。Checker はこれらを検証しません。", file=sys.stderr)
      raise
    if args.command == "init":
      return _init(args)
    if args.command == "confirm-pending-update":
      started = True
      release = confirm_pending_update(
        args.article_root, args.article_id,
        confirmed_deploy_success=args.confirmed_deploy_success,
        confirmed_content_match=args.confirmed_content_match)
      print(f"article {args.article_id}: Pending UPDATE をユーザー確認に基づく手動 SUCCESS として確定しました。")
      print(f"Successful release を {release} に更新し、対象 Pending を解消しました。"
            "Released snapshot を deploy artifact の内容に更新しました。")
      print("Dashboard 成功と remote content equality はユーザーによる確認です。"
            "Checker 自身が検証したものではありません。")
      print("この操作の成功・終了コード 0 は COMMIT_READY を保証しません。")
      print(f"通常 check の再実行が必要です: python -m checker check --article-root {str(args.article_root)!r}")
      print("通常 check が COMMIT_READY と判定した場合のみ、管理差分を確認して [skip ci] commit / push できます。")
      return ExitCode.READY
    worktrees = identify_worktrees(args.article_root)
    # pending がない場合にも初期化済みの owner 設定を要求します。
    load_configured_user_id(worktrees.deploy)
    # 操作表示用の入力を保持します。状態検証と readiness はコアに委譲します。
    pending = read_ledger(worktrees.deploy)["pending_next_state"]
    started = True
    outcome = check_pending(worktrees.deploy)
    unresolved = len(read_ledger(worktrees.deploy)["pending_next_state"])
    fatal = outcome.fatal_error or outcome.readiness.fatal_error
    stream = sys.stderr if fatal else sys.stdout
    for article_id, item in outcome.articles.items():
      entry = pending.get(article_id)
      operation = entry.get("operation", "INVALID") if isinstance(entry, dict) else "INVALID"
      # 全体の致命的失敗では未適用の観測を SUCCESS と表示しません。
      result = "UNDETERMINED" if fatal and item.result == CreateResult.SUCCESS else item.result
      print(f"article {article_id}: operation={operation} result={result}", file=stream)
      if item.detail:
        print(f"  {item.detail}", file=stream)
    for detail in (*outcome.diagnostics, *outcome.readiness.diagnostics):
      print(detail, file=stream)
    if fatal:
      print("ERROR: global readiness を確定できません。", file=sys.stderr)
      print("terminal apply によりローカル管理状態が部分変更されている可能性があります。", file=sys.stderr)
      _recovery()
      return ExitCode.ERROR
    print(f"Global readiness: {outcome.readiness.result}")
    print(f"Unresolved pending articles: {unresolved}")
    if outcome.readiness.result == CommitReadiness.COMMIT_READY:
      print("deploy state は confirmed management state と整合しています。"
            "deploy の差分を確認してください。管理・cleanup commit には [skip ci] を使用してください。")
      return ExitCode.READY
    print("復旧が完了するまで管理・cleanup 変更を commit/push しないでください。"
          "原因を確認し、後で one-shot check を再実行してください。")
    if any(item.result == CreateResult.ERROR for item in outcome.articles.values()):
      print("terminal apply の部分変更が残っている可能性があります。"
            "deploy の差分、ledger の pending_next_state と released snapshots を確認してください。")
    return ExitCode.NOT_READY
  except KeyboardInterrupt:
    print("INTERRUPTED: キーボード操作により中断しました。", file=sys.stderr)
    code = ExitCode.INTERRUPTED
  except (GuardError, OSError, ValueError, TypeError) as exc:
    print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    code = ExitCode.ERROR
  except Exception as exc:
    # 未知の例外の内容には private な情報が含まれる可能性があります。
    print(f"ERROR: {type(exc).__name__}: Checker の処理を完了できません。", file=sys.stderr)
    code = ExitCode.ERROR
  if started:
    print("terminal apply によりローカル管理状態が部分変更されている可能性があります。", file=sys.stderr)
  if args is not None and args.command == "confirm-pending-update":
    print("通常 check より強いユーザー責任を伴います。再実行時も Dashboard の対象 deploy 成功完了と"
          "現在の remote content と deploy artifact の一致をユーザー自身が確認してください。"
          "Checker はこれらを検証しません。", file=sys.stderr)
    print(f"原因・管理状態を確認後、通常 check を再実行してください: "
          f"python -m checker check --article-root {str(args.article_root)!r}", file=sys.stderr)
  _recovery()
  return code
