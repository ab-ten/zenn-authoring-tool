"""I/O を行わず、記事 operation を一度だけ分類して検証します。"""

from collections.abc import Iterable, Mapping
from uuid import UUID

from checker.terminal_create import _SLUG, _validate_article_id
from converter.models import ArticlePlanEntry, ArticlePlanningResult, PreparedSourceRecord


def _release(value: object) -> bool:
  return type(value) is int and value >= 1


def _slug(value: object) -> bool:
  return isinstance(value, str) and _SLUG.fullmatch(value) is not None


def _classify(state: str, confirmed: Mapping | None, changed: bool) -> str | None:
  if state == "hold":
    return None
  if confirmed is None:
    return "CREATE" if state == "active" else None
  if confirmed["state"] == "erased":
    return "RECREATE" if state == "active" else None
  if state == "erased":
    return "DELETE"
  return "UPDATE" if changed else None


def plan_articles(
    sources: Iterable[PreparedSourceRecord], ledger: Mapping,
    released_snapshots: Mapping[str, str],
) -> ArticlePlanningResult:
  """prepared source record と confirmed ledger から記事部分のみを計画します。

  ledger は articles と pending_next_state の map を持つ既存表現です。
  snapshot は canonical article ID をキーとする、未正規化の content です。
  pending guard は sources の列挙より先に実行します。ただし source record
  生成自体は呼び出し側の責務であり、実 entry の worktree/pending guard、
  parser の呼び出しも呼び出し側が行います。image diff、final PublishPlan、
  preflight、apply は未実装です。
  """
  if not isinstance(ledger, Mapping) or not isinstance(
      ledger.get("pending_next_state"), Mapping):
    raise ValueError("Invalid ledger pending map")
  if ledger["pending_next_state"]:
    raise ValueError("Unresolved pending_next_state")
  confirmed_articles = ledger.get("articles")
  if not isinstance(confirmed_articles, Mapping):
    raise ValueError("Invalid ledger articles map")
  if not isinstance(released_snapshots, Mapping):
    raise ValueError("Invalid released snapshot map")

  confirmed_slugs = {}
  for article_id, confirmed in confirmed_articles.items():
    _validate_article_id(article_id)
    if not isinstance(confirmed, Mapping) or confirmed.get("state") not in (
        "active", "erased"):
      raise ValueError(f"Invalid confirmed state: {article_id}")
    if not _release(confirmed.get("successful_release")):
      raise ValueError(f"Invalid successful release: {article_id}")
    slug = confirmed.get("slug")
    if not _slug(slug):
      raise ValueError(f"Invalid confirmed slug: {article_id}")
    if slug in confirmed_slugs:
      raise ValueError("Duplicate confirmed slug")
    confirmed_slugs[slug] = article_id
    if not isinstance(released_snapshots.get(article_id), str):
      raise ValueError(f"Missing or invalid released snapshot: {article_id}")

  ids, candidate_slugs, entries = set(), set(), []
  for source in sources:
    if not isinstance(source, PreparedSourceRecord):
      raise ValueError("A prepared source record is required")
    try:
      article_id = str(UUID(source.article_id))
    except (ValueError, TypeError, AttributeError) as exc:
      raise ValueError("Invalid source UUID") from exc
    if article_id in ids:
      raise ValueError("Duplicate normalized source UUID")
    ids.add(article_id)
    if source.state not in ("active", "hold", "erased"):
      raise ValueError("Invalid source state")
    confirmed = confirmed_articles.get(article_id)
    changed = False
    if source.state == "active":
      if not isinstance(source.candidate_content, str):
        raise ValueError("Active source requires candidate article content")
      if confirmed is not None:
        changed = source.candidate_content != released_snapshots[article_id]
    operation = _classify(source.state, confirmed, changed)
    if source.observation_override is not None and operation != "UPDATE":
      raise ValueError("Only UPDATE accepts observation override")

    if operation is not None or source.state == "active":
      previous = confirmed["successful_release"] if confirmed is not None else 0
      expected = 1 if operation == "CREATE" else previous + (
        1 if operation in ("UPDATE", "RECREATE") and changed else 0)
      if not _release(source.release) or source.release != expected:
        raise ValueError(f"Invalid source release: expected {expected}")
      if not _slug(source.slug):
        raise ValueError("Invalid source slug")
      if operation in ("UPDATE", "DELETE") or operation is None:
        if source.slug != confirmed["slug"]:
          raise ValueError("Source slug must equal confirmed slug")
      owner = confirmed_slugs.get(source.slug)
      if owner is not None and owner != article_id:
        raise ValueError("Candidate slug overlaps another confirmed article")
    if operation is not None:
      if source.slug in candidate_slugs:
        raise ValueError("Duplicate candidate slug")
      candidate_slugs.add(source.slug)
      entries.append(ArticlePlanEntry(
        article_id, operation, source.slug, source.state, source.release,
        source.candidate_content if operation != "DELETE" else None,
        source.observation_override))

  if confirmed_articles.keys() - ids:
    raise ValueError("Ledger-managed article has no source")
  return ArticlePlanningResult(tuple(entries))
