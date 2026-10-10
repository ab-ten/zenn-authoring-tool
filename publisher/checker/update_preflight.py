"""固定済み UPDATE plan の drift と一回限りの override を検証します。"""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Protocol

from checker.create_observation import ArticleResponse, load_configured_user_id
from checker.create_preflight import (
  ArticlePlan, ArticlePlanInput, ArticlePreflight, PublishPlanInput,
  PreflightResult, PreflightStatus,
)
from checker.local_state import read_ledger
from checker.terminal_create import _SLUG, _validate_article_id, _validate_observation
from checker.zenn_http import fetch_article as http_fetch_article


class UpdateArticlePlanInput(ArticlePlanInput, Protocol):
  """UPDATE に必要な読み取り専用の追加フィールドです。"""

  @property
  def state(self) -> str: ...

  @property
  def release(self) -> int: ...

  @property
  def observation_override(self) -> Mapping[str, str] | None: ...


@dataclass(frozen=True)
class UpdateArticlePlan(ArticlePlan):
  """state は source の target state です。override は入力から切り離します。"""

  state: str
  release: int
  observation_override: Mapping[str, str] | None = None

  def __post_init__(self):
    if isinstance(self.observation_override, Mapping):
      object.__setattr__(self, "observation_override",
                         MappingProxyType(dict(self.observation_override)))


@dataclass(frozen=True)
class UpdateArticlePreflight(ArticlePreflight):
  """成功時の baseline と、失敗時の比較可能な診断値を返します。"""

  baseline_observation: Mapping[str, str] | None = None
  ledger_observation: Mapping[str, str] | None = None
  current_observation: Mapping[str, str] | None = None
  override_observation: Mapping[str, str] | None = None
  observation_override_json: str | None = None


def _observation(value: object) -> Mapping[str, str]:
  """既存の文字列検証で正規化し、変更可能な参照を保持しません。"""
  if not isinstance(value, Mapping):
    raise ValueError("A concrete Zenn observation is required")
  return MappingProxyType(_validate_observation(dict(value)))


def _validate_entry(entry: UpdateArticlePlanInput, ledger: dict):
  _validate_article_id(entry.article_id)
  if entry.operation != "UPDATE":
    raise ValueError(f"Unsupported operation: {entry.operation}")
  confirmed = ledger["articles"].get(entry.article_id)
  if not isinstance(confirmed, dict) or confirmed.get("state") != "active":
    raise ValueError("UPDATE requires an active confirmed article")
  if entry.state != "active":
    raise ValueError("UPDATE target/source state must be active")
  if not isinstance(entry.slug, str) or not _SLUG.fullmatch(entry.slug) or (
      entry.slug != confirmed.get("slug")):
    raise ValueError("UPDATE slug must equal valid confirmed slug")
  previous = confirmed.get("successful_release")
  if type(previous) is not int or previous < 1 or type(entry.release) is not int or (
      entry.release != previous + 1):
    raise ValueError("UPDATE release must be the next successful release")
  successful = _observation(confirmed.get("successful_zenn_observation"))
  override = None if entry.observation_override is None else _observation(entry.observation_override)
  return successful, override


def _remote_observation(response: object, slug: str, owner_id: int):
  if not isinstance(response, ArticleResponse) or type(response.status) is not int:
    raise ValueError("Invalid transport response or HTTP status")
  if response.status != 200:
    raise ValueError(f"Expected HTTP 200; received {response.status}")
  article = response.payload.get("article") if isinstance(response.payload, dict) else None
  if not isinstance(article, dict) or article.get("slug") != slug:
    raise ValueError("Invalid article schema or slug mismatch")
  user = article.get("user")
  remote_id = user.get("id") if isinstance(user, dict) else None
  if type(remote_id) is not int or remote_id <= 0 or remote_id != owner_id:
    raise ValueError("Expected valid configured-user publication")
  return _observation(article)


def preflight_update(
    deploy_root: Path, plan: PublishPlanInput,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightResult:
  """全件の local 検証後に観測し、OK 記事だけに固定 baseline を返します。

  plan、ledger、snapshot、source は変更しません。override の消費や除去は
  行わず、将来の converter が同じ plan と結果を apply に渡します。
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
        successful, override = _validate_entry(entry, ledger)
        if entry.article_id in ids or entry.slug in slugs:
          raise ValueError("Duplicate plan article ID or slug")
        ids.add(entry.article_id)
        slugs.add(entry.slug)
        validated.append((entry, successful, override))
      except (ValueError, TypeError, AttributeError) as exc:
        errors.append(UpdateArticlePreflight(
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
  for entry, successful, override in validated:
    try:
      current = _remote_observation(fetch_article(entry.slug), entry.slug, owner_id)
    except Exception as exc:
      # transport 例外の種類に依存せず、観測失敗時には適用を拒否します。
      results.append(UpdateArticlePreflight(
        entry.article_id, entry.slug, PreflightStatus.ERROR,
        f"Remote observation failed: {exc}"))
      continue
    status, detail, override_json = PreflightStatus.OK, "", None
    if override is not None:
      if current != override:
        status = PreflightStatus.ERROR
        detail = "Remote publication may have changed again after override creation"
      elif current == successful:
        status = PreflightStatus.ERROR
        detail = "Observation override is stale or unnecessary; no remote drift"
    elif current != successful:
      status = PreflightStatus.ERROR
      detail = "Remote drift; review current observation for #observation-override"
      override_json = json.dumps(dict(current), ensure_ascii=False, sort_keys=True)
    results.append(UpdateArticlePreflight(
      entry.article_id, entry.slug, status, detail,
      baseline_observation=current if status == PreflightStatus.OK else None,
      ledger_observation=successful, current_observation=current,
      override_observation=override, observation_override_json=override_json))
  overall = PreflightStatus.ERROR if any(
    item.result == PreflightStatus.ERROR for item in results) else PreflightStatus.OK
  return PreflightResult(overall, tuple(results))
