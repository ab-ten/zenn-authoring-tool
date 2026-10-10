"""混在 publish plan を全件 local 検証後に既存 preflight へ委譲します。"""

from pathlib import Path
from typing import Callable

from checker import create_preflight, update_preflight, delete_preflight, recreate_preflight
from checker.create_observation import ArticleResponse, load_configured_user_id
from checker.create_preflight import (
  ArticlePreflight, PublishPlan, PublishPlanInput, PreflightResult, PreflightStatus,
)
from checker.local_state import read_ledger
from checker.terminal_create import _validate_article_id
from checker.zenn_http import fetch_article as http_fetch_article


def preflight_plan(
    deploy_root: Path, plan: PublishPlanInput,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightResult:
  """全件の local 検証成功後のみ、元 entry の順序で remote を観測します。

  単記事 view は dispatch 専用です。元 plan と entry を変更せず、各操作の
  result をそのまま保持します。全体 OK の場合のみ同じ plan を適用できます。
  """
  operations = {
    "CREATE": (create_preflight._validate_entry, create_preflight.preflight_create),
    "UPDATE": (update_preflight._validate_entry, update_preflight.preflight_update),
    "DELETE": (delete_preflight._validate_entry, delete_preflight.preflight_delete),
    "RECREATE": (recreate_preflight._validate_entry, recreate_preflight.preflight_recreate),
  }
  try:
    root = Path(deploy_root)
    ledger = read_ledger(root)
    if ledger["pending_next_state"]:
      raise ValueError("Unresolved pending_next_state")
    entries = plan.articles
    if not isinstance(entries, tuple):
      raise ValueError("Plan articles must be an immutable tuple")
    errors = []
    ids, slugs = set(), set()
    for entry in entries:
      try:
        _validate_article_id(entry.article_id)
        if not isinstance(entry.operation, str) or entry.operation not in operations:
          raise ValueError(f"Unsupported operation: {entry.operation}")
        if entry.operation != "UPDATE" and getattr(entry, "observation_override", None) is not None:
          raise ValueError(f"{entry.operation} does not accept observation override")
        # 操作別検証の成否によらず、plan 全体の重複を拒否します。
        if entry.article_id in ids:
          raise ValueError("Duplicate plan article ID across operations")
        ids.add(entry.article_id)
        if isinstance(entry.slug, str):
          if entry.slug in slugs:
            raise ValueError("Duplicate plan candidate slug across operations")
          slugs.add(entry.slug)
        operations[entry.operation][0](entry, ledger)
      except (ValueError, TypeError, AttributeError) as exc:
        errors.append(ArticlePreflight(
          getattr(entry, "article_id", ""), getattr(entry, "slug", ""),
          PreflightStatus.ERROR, str(exc)))
    if errors:
      return PreflightResult(PreflightStatus.ERROR, tuple(errors))
    if not entries:
      return PreflightResult(PreflightStatus.OK)
    load_configured_user_id(root)
  except (OSError, ValueError, TypeError, AttributeError) as exc:
    return PreflightResult(PreflightStatus.ERROR, diagnostics=(str(exc),))

  results, diagnostics, statuses = [], [], []
  for entry in entries:
    result = operations[entry.operation][1](root, PublishPlan((entry,)), fetch_article)
    statuses.append(result.result)
    diagnostics.extend(result.diagnostics)
    if result.articles:
      results.extend(result.articles)
    else:
      # 記事結果のない local failure でも、元 plan の記事との対応を保持します。
      results.append(ArticlePreflight(
        entry.article_id, entry.slug, PreflightStatus.ERROR,
        "; ".join(result.diagnostics) or "Operation preflight returned no article result"))
      statuses.append(PreflightStatus.ERROR)
  overall = PreflightStatus.OK
  if PreflightStatus.ERROR in statuses:
    overall = PreflightStatus.ERROR
  elif PreflightStatus.COLLISION in statuses:
    overall = PreflightStatus.COLLISION
  return PreflightResult(overall, tuple(results), tuple(diagnostics))
