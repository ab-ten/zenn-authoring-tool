"""pending の全観測、terminal apply、global readiness を一度だけ実行します。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from checker.commit_readiness import ReadinessResult, check_commit_readiness
from checker.create_observation import (
  ArticleResponse, CreateObservation, CreateResult, classify_create_response,
  load_configured_user_id,
)
from checker.delete_observation import classify_delete_response
from checker.local_state import read_ledger, safe_path
from checker.recreate_observation import classify_recreate_response
from checker.terminal_collision import apply_collision
from checker.terminal_create import (
  LocalApplyError, _SLUG, _read_create_pending, _validate_article_id, apply_create_success,
)
from checker.terminal_delete import read_delete_pending, apply_delete_success
from checker.terminal_recreate import read_recreate_pending, apply_recreate_success
from checker.terminal_update import read_update_pending, apply_update_success
from checker.update_observation import classify_update_response
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
  """CREATE 専用の互換 API として未対応 operation を全体で拒否します。"""
  return _check_pending(deploy_root, fetch_article, creates_only=True)


def check_pending(
    deploy_root: Path,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> CheckResult:
  """全 operation の pending を一度観測し、記事別結果と readiness を返します。"""
  return _check_pending(deploy_root, fetch_article, creates_only=False)


def _validate_slug_ownership(ledger: dict) -> None:
  """記事間の slug 所有権を検証し、自記事の一致を許可します。

  不正な個別 pending の形式は operation validator が拒否します。
  confirmed slug と pending slug の両方を予約し、artifact の共有を防ぎます。
  """
  owners = {}
  for entries in (ledger["articles"], ledger["pending_next_state"]):
    for article_id, entry in entries.items():
      if not isinstance(entry, dict):
        if entries is ledger["articles"]:
          raise LocalApplyError("Invalid confirmed state")
        continue
      slug = entry.get("slug")
      if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        continue
      if slug in owners and owners[slug] != article_id:
        raise LocalApplyError("Slug overlaps another managed article")
      owners[slug] = article_id


def _check_pending(deploy_root: Path,
                   fetch_article: Callable[[str], ArticleResponse],
                   *, creates_only: bool) -> CheckResult:
  """全観測終了後に terminal result を個別適用します。

  CREATE 専用 API では未対応 operation があれば全 local apply を拒否します。
  unified API では不正な operation を記事単位の ERROR とします。
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
    if creates_only:
      unsupported = [article_id for article_id, entry in pending_map.items()
                     if not isinstance(entry, dict) or entry.get("operation") != "CREATE"]
      if unsupported:
        raise LocalApplyError(f"Unsupported pending operations: {', '.join(unsupported)}")
    if not pending_map:
      return CheckResult({}, check_commit_readiness(root))
    safe_path(root, ".zenn-authoring", "config.json")
    owner_id = load_configured_user_id(root)
    if creates_only:
      slugs = [entry.get("slug") for entry in pending_map.values()]
      if len(slugs) != len(set(slugs)):
        raise LocalApplyError("Duplicate pending slugs")
      for entry in ledger["articles"].values():
        if not isinstance(entry, dict) or entry.get("slug") in slugs:
          raise LocalApplyError("Pending slug overlaps or invalid confirmed state")
    else:
      _validate_slug_ownership(ledger)
  except (OSError, ValueError, TypeError) as exc:
    if ledger is not None:
      results = {article_id: CreateObservation(CreateResult.ERROR, detail=str(exc))
                 for article_id in ledger["pending_next_state"]}
    return CheckResult(results, check_commit_readiness(root), (str(exc),))

  for article_id in pending_map:
    try:
      _validate_article_id(article_id)
      entry = pending_map[article_id]
      operation = entry.get("operation") if isinstance(entry, dict) else None
      validators = {"CREATE": _read_create_pending, "UPDATE": read_update_pending,
                    "DELETE": read_delete_pending, "RECREATE": read_recreate_pending}
      if not isinstance(operation, str) or operation not in validators:
        raise LocalApplyError("Unsupported or invalid pending operation")
      pending = validators[operation](ledger, article_id)
      response = fetch_article(pending["slug"])
      if not isinstance(response, ArticleResponse):
        raise ValueError("Invalid transport response")
      if operation == "CREATE":
        results[article_id] = classify_create_response(response, pending["slug"], owner_id)
      else:
        classifiers = {"UPDATE": classify_update_response,
                       "DELETE": classify_delete_response,
                       "RECREATE": classify_recreate_response}
        results[article_id] = classifiers[operation](response, pending, owner_id)
    except (OSError, ValueError, TypeError) as exc:
      results[article_id] = CreateObservation(CreateResult.ERROR, detail=str(exc))

  try:
    if safe_path(root, ".zenn-authoring", "ledger.json").read_bytes() != before:
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
        operation = pending["operation"]
        if operation == "DELETE":
          apply_delete_success(root, article_id)
        else:
          appliers = {"CREATE": apply_create_success, "UPDATE": apply_update_success,
                      "RECREATE": apply_recreate_success}
          appliers[operation](root, article_id, result.observation)
      else:
        apply_collision(root, article_id)
    except (OSError, ValueError, TypeError) as exc:
      results[article_id] = CreateObservation(CreateResult.ERROR,
                                            detail=f"Local apply failed: {exc}")
  return CheckResult(results, check_commit_readiness(root))
