"""明示的なユーザー確認に基づき、PENDING UPDATE のみを確定します。"""

from pathlib import Path
from typing import Callable

from checker.check_pending import _validate_slug_ownership
from checker.create_observation import ArticleResponse, CreateResult, load_configured_user_id
from checker.errors import LocalApplyError
from checker.local_state import read_ledger, safe_path
from checker.terminal_update import read_update_pending, apply_update_success
from checker.update_observation import classify_update_response
from checker.zenn_http import fetch_article as http_fetch_article
from converter.git_inspection import identify_worktrees


def confirm_pending_update(
    article_root: Path, article_id: str, *,
    confirmed_deploy_success: bool,
    confirmed_content_match: bool,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> int:
  """対象 UPDATE を手動 SUCCESS として適用し、successful release を返します。

  両確認は毎回必要です。Checker は Dashboard 成功や remote content equality を
  検証しません。全体の commit readiness は通常 check で別途確認してください。
  前提不成立と処理失敗は例外を送出し、中断は呼び出し元に伝播します。
  """
  if confirmed_deploy_success is not True or confirmed_content_match is not True:
    raise LocalApplyError(
      "通常 check より強いユーザー責任を伴います。Dashboard で対象 deploy の成功完了と、"
      "現在の remote content と deploy artifact の一致をユーザー自身が検証し、"
      "両 confirmation を明示してください。Checker はこれらを検証しません。")
  root = identify_worktrees(article_root).deploy
  owner_id = load_configured_user_id(root)
  ledger = read_ledger(root)
  ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
  before = ledger_path.read_bytes()
  pending = read_update_pending(ledger, article_id)
  _validate_slug_ownership(ledger)
  artifact = safe_path(root, "articles", f"{pending['slug']}.md")
  if not artifact.is_file():
    raise LocalApplyError("Missing regular deploy artifact")
  snapshot = safe_path(root, ".zenn-authoring", "released", f"{article_id}.md")
  if snapshot.exists() and not snapshot.is_file():
    raise LocalApplyError("Not a regular released snapshot")
  result = classify_update_response(fetch_article(pending["slug"]), pending, owner_id)
  if result.result != CreateResult.PENDING:
    reason = "手動確認は不要です。" if result.result == CreateResult.SUCCESS else "手動確認は禁止されています。"
    raise LocalApplyError(
      f"UPDATE classification={result.result}: {reason} {result.detail} "
      "原因を確認し、通常 python -m checker check --article-root PATH を再実行してください。")
  if safe_path(root, ".zenn-authoring", "ledger.json").read_bytes() != before:
    raise LocalApplyError("Ledger changed during observation; local apply refused")
  # PENDING は検証済み current observation と baseline の完全一致を保証します。
  # ユーザー確認を根拠とする SUCCESS に限り、この等価な値を既存 apply に渡します。
  apply_update_success(root, article_id, pending["baseline_observation"])
  return pending["release"]
