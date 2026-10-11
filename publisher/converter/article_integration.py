"""read-only guards と既存の記事 parser、planner、日時検証を接続します。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from converter.entry_guards import CheckedInputs, inspect_inputs
from converter.filesystem_inputs import load_ledger, load_released_snapshots, load_sources
from converter.models import ArticlePlanningResult
from converter.planning import plan_articles
from converter.publication_dates import validate_publication_dates
from converter.source_parser import parse_source


@dataclass(frozen=True)
class CheckedArticlePlanning:
  """後続 image 処理に渡す入力と、日時検証済みの記事計画を保持します。

  Final PublishPlan ではありません。ledger と snapshot の可変参照は公開しません。
  """

  inputs: CheckedInputs
  article_plan: ArticlePlanningResult


def plan_repository_articles(
    article_root: Path, *, now: datetime | None = None,
) -> CheckedArticlePlanning:
  """明示された記事 repository を読み込み、検証完了後にのみ結果を返します。"""
  inputs = inspect_inputs(article_root)
  ledger = load_ledger(inputs.deploy_root)
  markdowns = load_sources(inputs)
  snapshots = load_released_snapshots(inputs.deploy_root, ledger)
  parsed = []
  for checked, markdown in zip(inputs.sources, markdowns, strict=True):
    try:
      source = parse_source(markdown)
      if source.record.state != checked.state:
        raise ValueError("Source state differs from entry guard")
    except ValueError as exc:
      raise ValueError(f"Invalid source {checked.path}: {exc}") from exc
    parsed.append(source)
  parsed_sources = tuple(parsed)
  plan = plan_articles(tuple(source.record for source in parsed_sources), ledger, snapshots)
  validate_publication_dates(parsed_sources, plan, snapshots, now=now)
  return CheckedArticlePlanning(inputs, plan)
