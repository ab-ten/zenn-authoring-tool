"""ローカル計画を確定し、既存 Unified Preflight の結果をそのまま保持します。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from checker import unified_preflight
from checker.create_observation import ArticleResponse
from checker.create_preflight import PreflightResult
from checker.zenn_http import fetch_article as http_fetch_article
from converter.models import PublishPlan
from converter.repository_planning import finalize_publish_plan, plan_repository


@dataclass(frozen=True)
class PreflightOutcome:
  """検証済み deploy root と、同一の最終計画・Checker 結果を保持します。

  永続状態ではありません。将来の Apply は全体の PreflightStatus.OK を
  明示的に確認する必要があり、記事単位の成功や baseline では許可されません。
  """

  deploy_root: Path
  plan: PublishPlan
  preflight: PreflightResult


def preflight_publish(
    article_root: Path,
    *,
    now: datetime | None = None,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightOutcome:
  """全ローカル検証後に一度だけ Preflight を呼び、非 OK の結果も保持します。

  ローカル計画や実行の例外はそのまま伝播します。plan と filesystem は
  変更せず、deploy root の再探索や pending の生成は行いません。
  """
  planning = plan_repository(article_root, now=now)
  deploy_root = planning.inputs.deploy_root
  plan = finalize_publish_plan(planning)
  preflight = unified_preflight.preflight_plan(deploy_root, plan, fetch_article)
  return PreflightOutcome(deploy_root, plan, preflight)
