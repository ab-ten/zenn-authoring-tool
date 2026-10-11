"""検証済み記事計画と画像計画を読み取り専用で集約します。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from converter.article_integration import plan_repository_articles
from converter.entry_guards import CheckedInputs
from converter.image_planning import plan_images
from converter.models import ArticlePlanningResult, ImagePlanningResult, PublishPlan


@dataclass(frozen=True)
class RepositoryPlanningResult:
  """Final PublishPlan 生成に渡せる、ローカル検証済みの部分計画です。"""

  inputs: CheckedInputs
  article_plan: ArticlePlanningResult
  image_plan: ImagePlanningResult


def plan_repository(article_root: Path, *, now: datetime | None = None) -> RepositoryPlanningResult:
  """記事計画が成功した後に画像計画を一度だけ実行します。"""
  articles = plan_repository_articles(article_root, now=now)
  images = plan_images(articles.inputs)
  return RepositoryPlanningResult(articles.inputs, articles.article_plan, images)


def finalize_publish_plan(planning: RepositoryPlanningResult) -> PublishPlan:
  """I/O や再計画を行わず、検証済み entry の tuple をそのまま保持します。"""
  return PublishPlan(planning.article_plan.articles, planning.image_plan.images)


def plan_publish(article_root: Path, *, now: datetime | None = None) -> PublishPlan:
  """既存のローカル検証がすべて成功した後に最終計画を一度だけ生成します。"""
  return finalize_publish_plan(plan_repository(article_root, now=now))
