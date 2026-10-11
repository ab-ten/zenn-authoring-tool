"""パース済み入力、部分計画と最終の不変 PublishPlan を定義します。"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from pathlib import PurePosixPath

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


@dataclass(frozen=True)
class ImagePlanEntry:
  """画像操作と、ADD/UPDATE の確定した content bytes を保持します。"""

  operation: str
  path: str
  content: bytes | None = None

  def __post_init__(self):
    if self.operation not in ("ADD", "UPDATE", "DELETE"):
      raise ValueError("Unsupported image operation")
    if (not isinstance(self.path, str) or "\0" in self.path or "\\" in self.path or
        not self.path.startswith("images/") or
        any(part in ("", ".", "..") for part in self.path.split("/")) or
        PurePosixPath(self.path).is_absolute()):
      raise ValueError("Unsafe image path")
    if self.operation == "DELETE":
      if self.content is not None:
        raise ValueError("DELETE does not retain source bytes")
    elif not isinstance(self.content, bytes):
      raise TypeError("ADD/UPDATE requires immutable content bytes")


@dataclass(frozen=True)
class ImagePlanningResult:
  """Final PublishPlan ではない、path 順の immutable 画像差分計画です。"""

  images: tuple[ImagePlanEntry, ...] = ()

  def __post_init__(self):
    if not isinstance(self.images, tuple) or not all(
        isinstance(entry, ImagePlanEntry) for entry in self.images):
      raise TypeError("Images must be a tuple of immutable image entries")
    paths = tuple(entry.path for entry in self.images)
    if len(set(paths)) != len(paths):
      raise ValueError("Duplicate image operation")
    object.__setattr__(self, "images", tuple(sorted(self.images, key=lambda entry: entry.path)))

  @property
  def has_image_changes(self) -> bool:
    return bool(self.images)


@dataclass(frozen=True)
class PublishPlan:
  """検証済みの記事・画像 entry のみを保持する最終計画です。"""

  articles: tuple[ArticlePlanEntry, ...] = ()
  images: tuple[ImagePlanEntry, ...] = ()

  def __post_init__(self):
    if not isinstance(self.articles, tuple) or not all(
        isinstance(entry, ArticlePlanEntry) for entry in self.articles):
      raise TypeError("Articles must be a tuple of immutable article entries")
    if not isinstance(self.images, tuple) or not all(
        isinstance(entry, ImagePlanEntry) for entry in self.images):
      raise TypeError("Images must be a tuple of immutable image entries")

  @property
  def has_image_changes(self) -> bool:
    return bool(self.images)
