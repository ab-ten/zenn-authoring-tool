"""固定済み publish plan の CREATE を読み取り専用で検証します。"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
from typing import Callable, Protocol

from checker.create_observation import ArticleResponse, load_configured_user_id
from checker.local_state import read_ledger
from checker.terminal_create import _validate_article_id
from checker.zenn_http import fetch_article as http_fetch_article


class ArticlePlanInput(Protocol):
  """converter と apply が共有する記事 entry の必要最小限の契約です。"""

  @property
  def article_id(self) -> str: ...

  @property
  def operation(self) -> str: ...

  @property
  def slug(self) -> str: ...


class PublishPlanInput(Protocol):
  """確定済み記事 entry を読み取り専用で公開します。"""

  @property
  def articles(self) -> tuple[ArticlePlanInput, ...]: ...


@dataclass(frozen=True)
class ArticlePlan:
  """単独利用や fixture 用の immutable 記事 entry です。"""

  article_id: str
  operation: str
  slug: str


@dataclass(frozen=True)
class PublishPlan:
  """永続化しない最小 plan です。converter は独自の実装も渡せます。"""

  articles: tuple[ArticlePlanInput, ...] = ()

  def __post_init__(self):
    if not isinstance(self.articles, tuple):
      raise TypeError("PublishPlan.articles must be a tuple")


class PreflightStatus(StrEnum):
  """deploy 適用の可否を示す post-deploy とは独立した結果です。"""

  OK = "OK"
  COLLISION = "COLLISION"
  ERROR = "ERROR"


@dataclass(frozen=True)
class ArticlePreflight:
  """問題の記事を識別できる診断を返します。baseline は生成しません。"""

  article_id: str
  slug: str
  result: PreflightStatus
  detail: str = ""


@dataclass(frozen=True)
class PreflightResult:
  """全件 OK の場合に限り、同じ plan の適用を許可します。"""

  result: PreflightStatus
  articles: tuple[ArticlePreflight, ...] = ()
  diagnostics: tuple[str, ...] = ()


def _classify(response: object, slug: str, owner_id: int) -> tuple[PreflightStatus, str]:
  if not isinstance(response, ArticleResponse) or type(response.status) is not int:
    return PreflightStatus.ERROR, "Invalid transport response or HTTP status"
  if response.status == 404:
    return PreflightStatus.OK, ""
  if response.status != 200:
    return PreflightStatus.ERROR, f"Unexpected HTTP status {response.status}"
  payload = response.payload
  article = payload.get("article") if isinstance(payload, dict) else None
  if not isinstance(article, dict):
    return PreflightStatus.ERROR, "Missing article object"
  if article.get("slug") != slug:
    return PreflightStatus.ERROR, "Unexpected article slug"
  user = article.get("user")
  remote_id = user.get("id") if isinstance(user, dict) else None
  if type(remote_id) is not int or remote_id <= 0:
    return PreflightStatus.ERROR, "Missing or invalid article.user.id"
  if remote_id == owner_id:
    return PreflightStatus.ERROR, "CREATE slug already belongs to configured user"
  return PreflightStatus.COLLISION, "CREATE slug belongs to another user"


def preflight_create(
    deploy_root: Path, plan: PublishPlanInput,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightResult:
  """plan を変更せず CREATE の ledger 前提と remote 整合性を検証します。

  全件のローカル検証に成功してから HTTP を呼び出します。記事が
  0 件の場合は pending guard 後に config と remote の検証を省略します。
  Protocol の実装は呼び出し側で immutable に確定してください。
  """
  try:
    ledger = read_ledger(Path(deploy_root))
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
        if entry.operation != "CREATE":
          raise ValueError(f"Unsupported operation: {entry.operation}")
        if not isinstance(entry.slug, str) or not re.fullmatch(r"[a-z0-9_-]{12,50}", entry.slug):
          raise ValueError("Invalid plan slug")
        if entry.article_id in ledger["articles"]:
          raise ValueError("CREATE already has a confirmed article")
        if entry.article_id in ids or entry.slug in slugs:
          raise ValueError("Duplicate plan article ID or slug")
        if any(isinstance(value, dict) and value.get("slug") == entry.slug
               for value in ledger["articles"].values()):
          raise ValueError("Plan slug overlaps a confirmed article")
        ids.add(entry.article_id)
        slugs.add(entry.slug)
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
  for entry in entries:
    try:
      status, detail = _classify(fetch_article(entry.slug), entry.slug, owner_id)
    except Exception as exc:
      # transport の実装に依存せず、観測失敗時には適用を拒否します。
      status, detail = PreflightStatus.ERROR, f"Remote observation failed: {exc}"
    results.append(ArticlePreflight(entry.article_id, entry.slug, status, detail))
  overall = PreflightStatus.OK
  if any(item.result == PreflightStatus.ERROR for item in results):
    overall = PreflightStatus.ERROR
  elif any(item.result == PreflightStatus.COLLISION for item in results):
    overall = PreflightStatus.COLLISION
  return PreflightResult(overall, tuple(results))
