"""固定済み RECREATE plan の旧 slug 優先使用を読み取り専用で検証します。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from checker.create_observation import ArticleResponse, load_configured_user_id, _valid_user_id
from checker.create_preflight import (
  ArticlePlan, ArticlePlanInput, ArticlePreflight, PublishPlanInput,
  PreflightResult, PreflightStatus,
)
from checker.local_state import read_ledger
from checker.terminal_create import _SLUG, _validate_article_id, _validate_observation
from checker.zenn_http import fetch_article as http_fetch_article


class RecreateArticlePlanInput(ArticlePlanInput, Protocol):
  """RECREATE の target/source state と確定済み release を公開します。"""

  @property
  def state(self) -> str: ...

  @property
  def release(self) -> int: ...


@dataclass(frozen=True)
class RecreateArticlePlan(ArticlePlan):
  """単独利用や fixture 用の immutable RECREATE entry です。"""

  state: str
  release: int


def _validate_entry(entry: RecreateArticlePlanInput, ledger: dict) -> str:
  _validate_article_id(entry.article_id)
  if entry.operation != "RECREATE":
    raise ValueError(f"Unsupported operation: {entry.operation}")
  if getattr(entry, "observation_override", None) is not None:
    raise ValueError("RECREATE does not accept observation override")
  confirmed = ledger["articles"].get(entry.article_id)
  if not isinstance(confirmed, dict) or confirmed.get("state") != "erased":
    raise ValueError("RECREATE requires an erased confirmed article")
  if entry.state != "active":
    raise ValueError("RECREATE target/source state must be active")
  old_slug = confirmed.get("slug")
  for label, slug in (("confirmed", old_slug), ("candidate", entry.slug)):
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
      raise ValueError(f"Invalid RECREATE {label} slug")
  previous = confirmed.get("successful_release")
  if type(previous) is not int or previous < 1 or type(entry.release) is not int or (
      entry.release not in (previous, previous + 1)):
    raise ValueError("RECREATE release must be N or N+1")
  _validate_observation(confirmed.get("successful_zenn_observation"))
  for confirmed_id, value in ledger["articles"].items():
    if confirmed_id == entry.article_id:
      continue
    if not isinstance(value, dict):
      raise ValueError(f"Invalid confirmed entry: {confirmed_id}")
    confirmed_slug = value.get("slug")
    if not isinstance(confirmed_slug, str) or not _SLUG.fullmatch(confirmed_slug):
      raise ValueError(f"Invalid confirmed slug: {confirmed_id}")
    if confirmed_slug in (old_slug, entry.slug):
      raise ValueError(f"RECREATE slug overlaps another managed article: {confirmed_id}")
  return old_slug


def _classify(response: object, slug: str, owner_id: int) -> PreflightStatus:
  """候補の空きと所有者を検証します。timestamp は要求しません。"""
  if not isinstance(response, ArticleResponse) or type(response.status) is not int:
    raise ValueError("Invalid transport response or HTTP status")
  if response.status == 404:
    return PreflightStatus.OK
  if response.status != 200:
    raise ValueError(f"Unexpected HTTP status {response.status}")
  article = response.payload.get("article") if isinstance(response.payload, dict) else None
  if not isinstance(article, dict):
    raise ValueError("Missing article object")
  if article.get("slug") != slug:
    raise ValueError("Unexpected article slug")
  user = article.get("user")
  remote_id = user.get("id") if isinstance(user, dict) else None
  if not _valid_user_id(remote_id):
    raise ValueError("Missing or invalid article.user.id")
  if remote_id == owner_id:
    raise ValueError("Own-user publication still exists or remote state is unexpected")
  return PreflightStatus.COLLISION


def preflight_recreate(
    deploy_root: Path, plan: PublishPlanInput,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightResult:
  """全件の local 検証後に旧 slug、必要な場合のみ候補 slug を観測します。

  plan と filesystem は変更せず、baseline は生成しません。Protocol の
  entry は呼び出し側で immutable に確定してください。内容比較は行いません。
  """
  try:
    ledger = read_ledger(Path(deploy_root))
    if ledger["pending_next_state"]:
      raise ValueError("Unresolved pending_next_state")
    entries = plan.articles
    if not isinstance(entries, tuple):
      raise ValueError("Plan articles must be an immutable tuple")
    errors, validated = [], []
    ids, slugs = set(), set()
    for entry in entries:
      try:
        old_slug = _validate_entry(entry, ledger)
        if entry.article_id in ids or entry.slug in slugs:
          raise ValueError("Duplicate plan article ID or candidate slug")
        ids.add(entry.article_id)
        slugs.add(entry.slug)
        validated.append((entry, old_slug))
      except (ValueError, TypeError, AttributeError) as exc:
        errors.append(ArticlePreflight(
          getattr(entry, "article_id", ""), getattr(entry, "slug", ""),
          PreflightStatus.ERROR, str(exc)))
    if errors:
      return PreflightResult(PreflightStatus.ERROR, tuple(errors))
    if not entries:
      return PreflightResult(PreflightStatus.OK)
    owner_id = load_configured_user_id(Path(deploy_root))
  except (OSError, ValueError, TypeError, AttributeError) as exc:
    return PreflightResult(PreflightStatus.ERROR, diagnostics=(str(exc),))

  results = []
  for entry, old_slug in validated:
    stage = "old slug" if old_slug != entry.slug else "candidate slug"
    context = f"S_old={old_slug}, S_new={entry.slug}"
    try:
      if old_slug != entry.slug:
        old_status = _classify(fetch_article(old_slug), old_slug, owner_id)
        if old_status == PreflightStatus.OK:
          raise ValueError(f"Previous confirmed slug is available; use S_old={old_slug}")
        stage = "candidate slug"
      status = _classify(fetch_article(entry.slug), entry.slug, owner_id)
      detail = f"candidate slug ({context}): Slug belongs to another user" if (
        status == PreflightStatus.COLLISION) else ""
    except Exception as exc:
      # 観測失敗時は同じ記事の次の観測を行わず、他記事の診断を継続します。
      status = PreflightStatus.ERROR
      detail = f"{stage} ({context}): {exc}"
    results.append(ArticlePreflight(entry.article_id, entry.slug, status, detail))
  overall = PreflightStatus.OK
  if any(item.result == PreflightStatus.ERROR for item in results):
    overall = PreflightStatus.ERROR
  elif any(item.result == PreflightStatus.COLLISION for item in results):
    overall = PreflightStatus.COLLISION
  return PreflightResult(overall, tuple(results))
