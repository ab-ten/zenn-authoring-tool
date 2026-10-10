"""固定済み DELETE plan を読み取り専用で検証し、記事別の警告を保持します。"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Protocol

from checker.create_observation import ArticleResponse, load_configured_user_id, _valid_user_id
from checker.create_preflight import (
  ArticlePlan, ArticlePlanInput, ArticlePreflight, PublishPlanInput,
  PreflightResult, PreflightStatus,
)
from checker.local_state import read_ledger
from checker.terminal_create import _SLUG, _validate_article_id, _validate_observation
from checker.zenn_http import fetch_article as http_fetch_article


class DeleteArticlePlanInput(ArticlePlanInput, Protocol):
  """DELETE の target/source state と確定済み release を公開します。"""

  @property
  def state(self) -> str: ...

  @property
  def release(self) -> int: ...


@dataclass(frozen=True)
class DeleteArticlePlan(ArticlePlan):
  """単独利用や fixture 用の immutable DELETE entry です。"""

  state: str
  release: int


@dataclass(frozen=True)
class DeleteArticlePreflight(ArticlePreflight):
  """警告と比較用 observation を保持します。baseline は返しません。"""

  warnings: tuple[str, ...] = ()
  ledger_observation: Mapping[str, str] | None = None
  current_observation: Mapping[str, str] | None = None


def _validate_entry(entry: DeleteArticlePlanInput, ledger: dict) -> Mapping[str, str]:
  _validate_article_id(entry.article_id)
  if entry.operation != "DELETE":
    raise ValueError(f"Unsupported operation: {entry.operation}")
  if getattr(entry, "observation_override", None) is not None:
    raise ValueError("DELETE does not accept observation override")
  confirmed = ledger["articles"].get(entry.article_id)
  if not isinstance(confirmed, dict) or confirmed.get("state") != "active":
    raise ValueError("DELETE requires an active confirmed article")
  if entry.state != "erased":
    raise ValueError("DELETE target/source state must be erased")
  if not isinstance(entry.slug, str) or not _SLUG.fullmatch(entry.slug) or (
      entry.slug != confirmed.get("slug")):
    raise ValueError("DELETE slug must equal valid confirmed slug")
  previous = confirmed.get("successful_release")
  if type(previous) is not int or previous < 1 or type(entry.release) is not int or (
      entry.release != previous):
    raise ValueError("DELETE release must equal successful release")
  successful = MappingProxyType(_validate_observation(
    confirmed.get("successful_zenn_observation")))
  for confirmed_id, value in ledger["articles"].items():
    if confirmed_id == entry.article_id:
      continue
    if not isinstance(value, dict):
      raise ValueError(f"Invalid confirmed entry: {confirmed_id}")
    confirmed_slug = value.get("slug")
    if not isinstance(confirmed_slug, str) or not _SLUG.fullmatch(confirmed_slug):
      raise ValueError(f"Invalid confirmed slug: {confirmed_id}")
    if confirmed_slug == entry.slug:
      raise ValueError(f"Plan slug overlaps another confirmed article: {confirmed_id}")
  return successful


def _classify(response: object, entry: DeleteArticlePlanInput, owner_id: int,
              successful: Mapping[str, str]) -> DeleteArticlePreflight:
  if not isinstance(response, ArticleResponse) or type(response.status) is not int:
    raise ValueError("Invalid transport response or HTTP status")
  if response.status == 404:
    return DeleteArticlePreflight(
      entry.article_id, entry.slug, PreflightStatus.OK,
      warnings=("Publication has already been deleted",))
  if response.status != 200:
    raise ValueError(f"Unexpected HTTP status {response.status}")
  article = response.payload.get("article") if isinstance(response.payload, dict) else None
  if not isinstance(article, dict):
    raise ValueError("Missing article object")
  if article.get("slug") != entry.slug:
    raise ValueError("Unexpected article slug")
  user = article.get("user")
  remote_id = user.get("id") if isinstance(user, dict) else None
  if not _valid_user_id(remote_id):
    raise ValueError("Missing or invalid article.user.id")
  if remote_id != owner_id:
    return DeleteArticlePreflight(
      entry.article_id, entry.slug, PreflightStatus.OK,
      warnings=("Old slug has already been reused by another user",))
  current = MappingProxyType(_validate_observation(article))
  drift = current != successful
  return DeleteArticlePreflight(
    entry.article_id, entry.slug, PreflightStatus.ERROR if drift else PreflightStatus.OK,
    "Remote drift of own-user publication" if drift else "",
    ledger_observation=successful, current_observation=current)


def preflight_delete(
    deploy_root: Path, plan: PublishPlanInput,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightResult:
  """全件の local 検証後に観測し、全件 OK の場合のみ適用を許可します。

  plan と filesystem は変更しません。Protocol 実装の entry は呼び出し側で
  immutable に確定してください。空 plan は pending guard 後に OK を返します。
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
        successful = _validate_entry(entry, ledger)
        if entry.article_id in ids or entry.slug in slugs:
          raise ValueError("Duplicate plan article ID or slug")
        ids.add(entry.article_id)
        slugs.add(entry.slug)
        validated.append((entry, successful))
      except (ValueError, TypeError, AttributeError) as exc:
        errors.append(DeleteArticlePreflight(
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
  for entry, successful in validated:
    try:
      item = _classify(fetch_article(entry.slug), entry, owner_id, successful)
    except Exception as exc:
      # transport の例外の種類に依存せず、観測失敗時には適用を拒否します。
      item = DeleteArticlePreflight(
        entry.article_id, entry.slug, PreflightStatus.ERROR,
        f"Remote observation failed: {exc}")
    results.append(item)
  overall = PreflightStatus.ERROR if any(
    item.result == PreflightStatus.ERROR for item in results) else PreflightStatus.OK
  return PreflightResult(overall, tuple(results))
