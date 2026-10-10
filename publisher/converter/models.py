"""パース済み入力と、final PublishPlan ではない記事 planning result を定義します。"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from checker.terminal_create import _validate_observation


@dataclass(frozen=True)
class PreparedSourceRecord:
  """呼び出し側が管理コメントを解析・除去したメモリ上の入力です。

  この型は parser、Front Matter 検証、Git cleanliness 検証を実装しません。
  candidate_content は管理コメント除去済みの article-content を渡してください。
  source のファイル名や Git 状態は入力に含めません。
  """

  article_id: str
  state: str
  slug: str | None = None
  release: int | None = None
  candidate_content: str | None = None
  observation_override: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ArticlePlanEntry:
  """既存 checker Protocol に対応する immutable 記事 entry です。"""

  article_id: str
  operation: str
  slug: str
  state: str
  release: int
  candidate_content: str | None = None
  observation_override: Mapping[str, str] | None = None

  def __post_init__(self):
    if self.operation not in ("CREATE", "UPDATE", "DELETE", "RECREATE"):
      raise ValueError("Unsupported article operation")
    if self.operation != "DELETE" and not isinstance(self.candidate_content, str):
      raise ValueError("Publish operation requires string candidate content")
    if self.operation == "DELETE" and self.candidate_content is not None:
      raise ValueError("DELETE does not retain candidate content")
    if self.observation_override is not None:
      if self.operation != "UPDATE":
        raise ValueError("Only UPDATE accepts observation override")
      if not isinstance(self.observation_override, Mapping):
        raise ValueError("A concrete Zenn observation is required")
      object.__setattr__(self, "observation_override", MappingProxyType(
        _validate_observation(dict(self.observation_override))))


@dataclass(frozen=True)
class ArticlePlanningResult:
  """image diff を含まない記事部分の結果です。preflight/apply は呼び出しません。"""

  articles: tuple[ArticlePlanEntry, ...] = ()

  def __post_init__(self):
    if not isinstance(self.articles, tuple) or not all(
        isinstance(entry, ArticlePlanEntry) for entry in self.articles):
      raise TypeError("Articles must be a tuple of immutable article entries")
