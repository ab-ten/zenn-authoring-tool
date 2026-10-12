"""現在の authoring source を読み取り専用で検証します。"""

from dataclasses import dataclass
from pathlib import Path
import stat
from uuid import UUID

from checker.local_state import safe_path
from checker.terminal_create import _validate_observation
from converter.git_inspection import identify_worktrees
from converter.planning import _release, _slug
from converter.source_parser import parse_source


class LintError(ValueError):
  """対象指定、repository 検証、列挙、読み込みの失敗を表します。"""


@dataclass(frozen=True)
class LintDiagnostic:
  """記事に対応する安定した診断を保持します。"""

  path: str
  severity: str
  code: str
  message: str


@dataclass(frozen=True)
class LintResult:
  """完了した検証の範囲、記事数、path 順の診断を保持します。"""

  scope: str
  article_count: int
  diagnostics: tuple[LintDiagnostic, ...]

  @property
  def error_count(self) -> int:
    return sum(item.severity == "error" for item in self.diagnostics)


def _target(value: str) -> tuple[str, ...]:
  if (not isinstance(value, str) or "\0" in value or "\\" in value or
      any(part in ("", ".", "..") for part in value.split("/")) or
      not value.startswith("articles/") or not value.endswith(".md")):
    raise LintError(f"Invalid article target: {value!r}")
  return tuple(value.split("/"))


def _targets(root: Path, articles: tuple[str, ...], all_articles: bool) -> tuple[str, ...]:
  if not all_articles:
    for value in articles:
      parts = _target(value)
      path = safe_path(root, *parts)
      if not stat.S_ISREG(path.stat().st_mode):
        raise LintError(f"Missing regular article file: {value!r}")
    return tuple(sorted(set(articles)))
  base = safe_path(root, "articles")
  if not base.is_dir():
    raise LintError("Missing or invalid articles directory")
  paths = []

  def walk(directory: Path) -> None:
    for path in sorted(directory.iterdir()):
      mode = path.lstat().st_mode
      if stat.S_ISDIR(mode):
        walk(path)
      elif stat.S_ISREG(mode):
        if path.suffix == ".md":
          value = path.relative_to(root).as_posix()
          _target(value)
          paths.append(value)
      else:
        raise LintError(f"Unsafe filesystem entry: {path}")

  walk(base)
  return tuple(sorted(paths))


def lint_sources(article_root: Path, *, all_articles: bool = False,
                 articles: tuple[str, ...] = ()) -> LintResult:
  """明示した記事 root の現在の bytes を検証します。

  all_articles と articles はどちらか一方を指定してください。
  Git identity のみを検証し、cleanliness、ledger、remote は参照しません。
  identify_worktrees の branch、相対リンク、両 HEAD commit の制約を継承します。
  同時編集に対しては読み込んだ bytes の観測結果であり、atomic snapshot ではありません。
  対象指定時には repository 全体の整合性を検証しません。
  """
  if article_root is None or bool(all_articles) == bool(articles):
    raise LintError("Explicit article root and exactly one lint scope are required")
  diagnostics, identities = [], {}
  try:
    safe_path(Path(article_root))
    root = identify_worktrees(article_root).authoring
    paths = _targets(root, articles, all_articles)
    for value in paths:
      path = safe_path(root, *_target(value))
      if not stat.S_ISREG(path.stat().st_mode):
        raise LintError(f"Not a regular article file: {value!r}")
      content = path.read_bytes()

      def error(code: str, message: str) -> None:
        diagnostics.append(LintDiagnostic(value, "error", code, message))

      try:
        markdown = content.decode("utf-8")
      except UnicodeDecodeError:
        error("INVALID_UTF8", "Article source must be valid UTF-8")
        continue
      try:
        record = parse_source(markdown).record
      except ValueError as exc:
        error("SOURCE_PARSE", str(exc))
        continue
      try:
        article_id = str(UUID(record.article_id))
      except (ValueError, TypeError, AttributeError):
        error("INVALID_UUID", "Invalid source UUID")
      else:
        identities.setdefault(article_id, []).append(value)
      if record.state not in ("active", "hold", "erased"):
        error("INVALID_STATE", "Invalid source state")
      if record.slug is not None and not _slug(record.slug):
        error("INVALID_SLUG", "Invalid optional source slug")
      if record.release is not None and not _release(record.release):
        error("INVALID_RELEASE", "Optional source release must be a positive integer")
      if record.observation_override is not None:
        try:
          _validate_observation(record.observation_override)
        except ValueError as exc:
          error("INVALID_OBSERVATION_OVERRIDE", str(exc))
    if all_articles:
      for article_id, involved in identities.items():
        if len(involved) > 1:
          message = f"Duplicate normalized source UUID {article_id}: {', '.join(involved)}"
          diagnostics.extend(LintDiagnostic(path, "error", "DUPLICATE_UUID", message)
                             for path in involved)
  except (OSError, ValueError) as exc:
    raise LintError(f"Lint could not complete: {exc}") from exc
  return LintResult("all" if all_articles else "selected", len(paths),
                    tuple(sorted(diagnostics, key=lambda item: (item.path, item.code))))
