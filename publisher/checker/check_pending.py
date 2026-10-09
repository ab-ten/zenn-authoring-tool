"""全 CREATE の観測、terminal apply、global readiness を一度だけ実行します。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from checker.commit_readiness import ReadinessResult, check_commit_readiness
from checker.create_observation import (
  ArticleResponse, CreateObservation, CreateResult, classify_create_response,
  load_configured_user_id,
)
from checker.local_state import read_ledger, safe_path
from checker.terminal_collision import apply_collision
from checker.terminal_create import (
  LocalApplyError, _read_create_pending, _validate_article_id, apply_create_success,
)
from checker.zenn_http import fetch_article as http_fetch_article


@dataclass(frozen=True)
class CheckResult:
  """記事別の最終結果、readiness、全体の診断を返します。"""

  articles: dict[str, CreateObservation]
  readiness: ReadinessResult
  diagnostics: tuple[str, ...] = ()


def check_pending_creates(
    deploy_root: Path,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> CheckResult:
  """全観測終了後に terminal result を個別適用します。

  未対応 operation が含まれる場合は全 local apply を拒否します。
  個別の観測・apply エラーは他記事の terminal completion を妨げません。
  解決済み記事は次回の観測対象に含めません。
  """
  root = Path(deploy_root)
  results = {}
  ledger = None
  try:
    ledger = read_ledger(root)
    ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
    before = ledger_path.read_bytes()
    pending_map = ledger["pending_next_state"]
    unsupported = [article_id for article_id, entry in pending_map.items()
                   if not isinstance(entry, dict) or entry.get("operation") != "CREATE"]
    if unsupported:
      raise LocalApplyError(f"Unsupported pending operations: {', '.join(unsupported)}")
    if not pending_map:
      return CheckResult({}, check_commit_readiness(root))
    safe_path(root, ".zenn-authoring", "config.json")
    owner_id = load_configured_user_id(root)
    slugs = [entry.get("slug") for entry in pending_map.values()]
    if len(slugs) != len(set(slugs)):
      raise LocalApplyError("Duplicate pending slugs")
    for entry in ledger["articles"].values():
      if not isinstance(entry, dict) or entry.get("slug") in slugs:
        raise LocalApplyError("Pending slug overlaps or invalid confirmed state")
  except (OSError, ValueError, TypeError) as exc:
    if ledger is not None:
      results = {article_id: CreateObservation(CreateResult.ERROR, detail=str(exc))
                 for article_id in ledger["pending_next_state"]}
    return CheckResult(results, check_commit_readiness(root), (str(exc),))

  for article_id in pending_map:
    try:
      _validate_article_id(article_id)
      pending = _read_create_pending(ledger, article_id)
      response = fetch_article(pending["slug"])
      if not isinstance(response, ArticleResponse):
        raise ValueError("Invalid transport response")
      results[article_id] = classify_create_response(response, pending["slug"], owner_id)
    except (OSError, ValueError, TypeError) as exc:
      results[article_id] = CreateObservation(CreateResult.ERROR, detail=str(exc))

  try:
    if ledger_path.read_bytes() != before:
      raise LocalApplyError("Ledger changed during observation; local apply refused")
  except (OSError, ValueError) as exc:
    return CheckResult(results, check_commit_readiness(root), (str(exc),))

  for article_id, result in results.items():
    if result.result not in (CreateResult.SUCCESS, CreateResult.COLLISION):
      continue
    try:
      pending = pending_map[article_id]
      safe_path(root, "articles", f"{pending['slug']}.md")
      safe_path(root, ".zenn-authoring", "ledger.json")
      if result.result == CreateResult.SUCCESS:
        safe_path(root, ".zenn-authoring", "released", f"{article_id}.md")
        apply_create_success(root, article_id, result.observation)
      else:
        apply_collision(root, article_id)
    except (OSError, ValueError, TypeError) as exc:
      results[article_id] = CreateObservation(CreateResult.ERROR,
                                            detail=f"Local apply failed: {exc}")
  return CheckResult(results, check_commit_readiness(root))
