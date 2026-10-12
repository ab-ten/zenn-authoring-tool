"""Checker の引数解析、実行前提検証、結果表示を提供します。"""

import argparse
from enum import IntEnum
from pathlib import Path
import sys

from checker.check_pending import check_pending
from checker.commit_readiness import CommitReadiness
from checker.create_observation import CreateResult, load_configured_user_id
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
  return parser


def _recovery() -> None:
  print("管理変更を commit/push しないでください。ホストで deploy worktree の Git status、"
        "staged/unstaged diff、untracked paths、config.json、ledger.json の pending_next_state、"
        "released snapshots と記事 artifact を手動で確認し、復旧してください。"
        "復旧後に one-shot check を再実行できます。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
  """既存 unified Checker を一度だけ実行し、構造化情報から終了コードを返します。"""
  started = False
  try:
    args = _parser().parse_args(argv)
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
  _recovery()
  return code
