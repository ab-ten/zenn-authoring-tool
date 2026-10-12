"""Converter の引数解析、結果表示、終了コードを提供します。"""

import argparse
from collections import Counter
from enum import IntEnum
import json
from pathlib import Path
import sys
import traceback

from checker.create_preflight import PreflightStatus
from checker.errors import LocalApplyError
from converter import workflow
from converter.lint import LintError, lint_sources
from converter.git_inspection import GuardError
from converter.preflight_integration import PreflightOutcome


class ExitCode(IntEnum):
  """Converter CLI 固有の終了コードです。Checker の契約は変更しません。"""

  COMPLETED = 0
  COLLISION = 1
  ERROR = 2
  INTERRUPTED = 130


def _article_root(value: str) -> Path:
  if not value:
    raise argparse.ArgumentTypeError("記事 repository のパスを指定してください。")
  return Path(value)


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="python -m converter", allow_abbrev=False,
    description="明示された記事 repository をローカル変換します。")
  commands = parser.add_subparsers(dest="command", required=True)
  convert = commands.add_parser(
    "convert", allow_abbrev=False, help="記事・画像のローカル変換を実行します。")
  convert.add_argument(
    "--article-root", required=True, type=_article_root, metavar="PATH",
    help="記事 repository の authoring worktree root（必須）")
  lint = commands.add_parser(
    "lint", allow_abbrev=False, help="現在の記事 source を読み取り専用で検証します。")
  lint.add_argument("--article-root", required=True, type=_article_root, metavar="PATH")
  scope = lint.add_mutually_exclusive_group(required=True)
  scope.add_argument("--all", action="store_true", help="articles/ 以下を再帰的に検証します。")
  scope.add_argument("--article", action="append", metavar="PATH",
                     help="articles/ 以下の repository 相対パス（複数指定可）")
  return parser


def _lint(args: argparse.Namespace) -> int:
  """lint の失敗を変換の復旧案内から分離して表示します。"""
  try:
    result = lint_sources(args.article_root, all_articles=args.all,
                          articles=tuple(args.article or ()))
    for item in result.diagnostics:
      print(f"{item.path}: {item.severity} [{item.code}]: {item.message}", file=sys.stderr)
    print(f"lint: scope={result.scope} articles={result.article_count} errors={result.error_count}")
    print("source のローカル検証が完了しました。公開準備の判定ではありません。")
    return 1 if result.error_count else 0
  except KeyboardInterrupt:
    print("INTERRUPTED: lint を中断しました。", file=sys.stderr)
    return ExitCode.INTERRUPTED
  except LintError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    return ExitCode.ERROR
  except Exception as exc:
    traceback.print_exception(exc, file=sys.stderr)
    return ExitCode.ERROR


def _diagnostics(outcome: PreflightOutcome) -> list[str]:
  """既存の診断値を、元の計画の操作と slug に対応付けて表示します。"""
  lines = [f"Preflight: {outcome.preflight.result}", *outcome.preflight.diagnostics]
  entries = {entry.article_id: entry for entry in outcome.plan.articles}
  for item in outcome.preflight.articles:
    entry = entries.get(item.article_id)
    context = f"{entry.operation} {entry.slug}" if entry else f"article {item.article_id} {item.slug}"
    lines.append(f"{context}: {item.result}: {item.detail}")
    for warning in getattr(item, "warnings", ()):
      lines.append(f"  warning: {warning}")
    for name in ("ledger_observation", "current_observation", "override_observation"):
      value = getattr(item, name, None)
      if value is not None:
        lines.append(f"  {name}: {json.dumps(dict(value), ensure_ascii=False)}")
    override = getattr(item, "observation_override_json", None)
    if override is not None:
      lines.append(f"  #observation-override: {override}")
  return lines


def _summary(outcome: PreflightOutcome) -> str:
  """適用済み計画のみを集計し、公開確認とは独立した次の手順を表示します。"""
  plan = outcome.plan
  articles = Counter(entry.operation for entry in plan.articles)
  images = Counter(entry.operation for entry in plan.images)
  changed = bool(plan.articles or plan.images)
  lines = ["ローカル変換が完了しました。Zenn の公開結果は未確認です。" if changed else "NO_CHANGES",
    "articles: " + " ".join(f"{name}={articles[name]}" for name in ("CREATE", "UPDATE", "DELETE", "RECREATE")),
    "images: " + " ".join(f"{name}={images[name]}" for name in ("ADD", "UPDATE", "DELETE")),
    "pending article transitions: " + ("あり" if plan.articles else "なし")]
  if plan.articles:
    lines.append("deploy の差分を確認し、ホストから commit/push してください。"
                 "Zenn のデプロイ後に state-checker check で記事の結果を確認してください。")
  elif plan.images:
    lines.append("記事の pending state は生成していません。deploy の差分を確認し、"
                 "ホストから commit/push してください。")
  if plan.images:
    lines.append("画像のデプロイ結果は記事と別に、Zenn Dashboard で確認してください。")
  return "\n".join(lines)


def _recovery() -> None:
  print("deploy worktree に部分変更が残っている可能性があります。"
        "不完全な変更を commit/push しないでください。\n"
        "ホストで deploy worktree の Git status、staged/unstaged diff、untracked paths、"
        ".zenn-authoring/ledger.json の pending_next_state を確認してください。\n"
        "次の変換前に手動で復旧を完了してください。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
  """引数を解析し、既存 workflow を一度だけ実行して終了コードを返します。

  argv が None の場合はプロセス引数を使用します。記事 root は必須で、
  repository の探索・検証は workflow に委譲します。argparse の help と
  引数エラーは SystemExit を送出します。Apply 失敗、未知の例外、中断では
  部分変更の可能性を案内し、再試行や復旧操作は実行しません。
  """
  args = _parser().parse_args(argv)
  if args.command == "lint":
    return _lint(args)
  try:
    outcome = workflow.run_convert(args.article_root)
    if outcome.preflight.result is not PreflightStatus.OK:
      print("\n".join(_diagnostics(outcome)), file=sys.stderr)
      print("計画は適用していません。", file=sys.stderr)
      return ExitCode.COLLISION if outcome.preflight.result is PreflightStatus.COLLISION else ExitCode.ERROR
    entries = {entry.article_id: entry for entry in outcome.plan.articles}
    for item in outcome.preflight.articles:
      for warning in getattr(item, "warnings", ()):
        entry = entries.get(item.article_id)
        context = f"{entry.operation} {entry.slug}" if entry else f"article {item.article_id} {item.slug}"
        print(f"{context}: warning: {warning}", file=sys.stderr)
    print(_summary(outcome))
    return ExitCode.COMPLETED
  except KeyboardInterrupt:
    print("INTERRUPTED: キーボード操作により中断しました。", file=sys.stderr)
    _recovery()
    return ExitCode.INTERRUPTED
  except LocalApplyError as exc:
    print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    _recovery()
    return ExitCode.ERROR
  except GuardError as exc:
    print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    return ExitCode.ERROR
  except ValueError as exc:
    print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    # 汎用 ValueError は発生工程を示さないため、書き込み前とは断定しません。
    _recovery()
    return ExitCode.ERROR
  except Exception as exc:
    traceback.print_exception(exc, file=sys.stderr)
    _recovery()
    return ExitCode.ERROR
