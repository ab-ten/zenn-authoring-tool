"""既存 planner の分類済み結果に対して公開日時の遷移を検証します。"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from uuid import UUID

from converter.models import ArticlePlanningResult
from converter.source_parser import JST, ParsedSource, snapshot_published_at


def validate_publication_dates(
    sources: Iterable[ParsedSource], plan: ArticlePlanningResult,
    released_snapshots: Mapping[str, str], *, now: datetime | None = None,
) -> None:
  """plan_articles() 成功後に同じ source の metadata を渡してください。

  operation は plan entry から取得し、再分類しません。entry がない active
  source も検証します。これは final PublishPlan や実 Converter entry では
  なく、呼び出し側は日時検証の成功後にのみ後続処理へ進んでください。
  now は timezone-aware datetime を注入できます。
  """
  current = datetime.now(JST) if now is None else now
  if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
    raise ValueError("now must be a timezone-aware datetime")
  entries = {entry.article_id: entry for entry in plan.articles}
  if len(entries) != len(plan.articles):
    raise ValueError("Duplicate plan article ID")
  seen = set()
  for parsed in sources:
    record = parsed.record
    article_id = str(UUID(record.article_id))
    if article_id in seen:
      raise ValueError("Duplicate parsed source ID")
    seen.add(article_id)
    entry = entries.get(article_id)
    if entry is not None and (entry.state != record.state or entry.slug != record.slug or
                             entry.release != record.release or (
        entry.operation != "DELETE" and entry.candidate_content != record.candidate_content)):
      raise ValueError("Parsed source does not match classified plan entry")
    if record.state != "active":
      continue
    operation = entry.operation if entry is not None else None
    if operation in ("CREATE", "RECREATE"):
      if parsed.published_at is not None and parsed.published_at > current:
        raise ValueError("Future published_at is unsupported")
    elif operation in ("UPDATE", None):
      snapshot = released_snapshots.get(article_id)
      if not isinstance(snapshot, str):
        raise ValueError("Missing released snapshot for published_at validation")
      if parsed.published_at != snapshot_published_at(snapshot):
        raise ValueError("Established published_at cannot be added, removed or changed")
    else:
      raise ValueError("Active source has an incompatible classified operation")
  if entries.keys() - seen:
    raise ValueError("Plan article has no parsed source metadata")
