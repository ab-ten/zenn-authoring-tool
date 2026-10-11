"""filesystem integration に先行する read-only entry guards を提供します。"""

from dataclasses import dataclass
from pathlib import Path
import stat

from checker.local_state import read_ledger, safe_path
from converter.git_inspection import (
  GuardError, StatusEntry, head_paths, identify_worktrees, inspect_status,
)
from converter.source_parser import minimal_source_state


@dataclass(frozen=True)
class CheckedSource:
  path: Path
  state: str


@dataclass(frozen=True)
class CheckedInputs:
  """記事 operation や final PublishPlan を含まない検査済み入力です。"""

  authoring_root: Path
  deploy_root: Path
  sources: tuple[CheckedSource, ...]
  images: tuple[Path, ...]


def validate_deploy(entries: tuple[StatusEntry, ...]) -> None:
  if entries:
    raise GuardError("Dirty deploy worktree: " + ", ".join(
      f"{entry.code} {entry.path!r}" +
      (f" (from {entry.original_path!r})" if entry.original_path else "")
      for entry in entries))


def validate_inputs(sources: tuple[tuple[str, str], ...], images: tuple[str, ...],
                    tracked: frozenset[str], entries: tuple[StatusEntry, ...]) -> None:
  """取得済み情報だけから publish input の cleanliness を判定します。"""
  dirty = {path for entry in entries for path in (entry.path, entry.original_path)
           if path is not None}
  for path, state in sources:
    if state not in ("active", "erased", "hold"):
      raise GuardError(f"Invalid source state: {path!r}")
    if state != "hold" and (path not in tracked or path in dirty):
      raise GuardError(f"Source must be tracked in HEAD and clean: {path!r}")
  for path in images:
    if path not in tracked or path in dirty:
      raise GuardError(f"Image must be tracked in HEAD and clean: {path!r}")


def _files(root: Path, name: str) -> tuple[Path, ...]:
  """実 filesystem を再帰走査し、symlink と特殊 object を拒否します。"""
  base = safe_path(root, name)
  if not base.exists():
    return ()
  if not base.is_dir():
    raise GuardError(f"Input root is not a directory: {base}")
  files = []

  def walk(directory):
    for path in sorted(directory.iterdir()):
      mode = path.lstat().st_mode
      if stat.S_ISDIR(mode):
        walk(path)
      elif stat.S_ISREG(mode):
        files.append(path)
      else:
        raise GuardError(f"Unsafe filesystem input: {path}")

  walk(base)
  return tuple(files)


def inspect_inputs(article_root: Path) -> CheckedInputs:
  """worktree 検証、cleanliness、pending の順に検査します。

  article_root は必須です。CWD への fallback、full parse、planning、書き込みは行いません。
  削除された Markdown は state を取得できないため、安全側で ERROR とします。
  """
  try:
    trees = identify_worktrees(article_root)
    validate_deploy(inspect_status(trees.deploy))
    entries = inspect_status(trees.authoring)
    tracked = head_paths(trees.authoring)
    files = _files(trees.authoring, "articles")
    sources = []
    for path in files:
      if path.suffix != ".md":
        continue
      try:
        state = minimal_source_state(path.read_bytes().decode("utf-8"))
      except (ValueError, UnicodeError) as exc:
        raise GuardError(f"Invalid source state at {path}: {exc}") from exc
      sources.append(CheckedSource(path, state))
    existing = {source.path.relative_to(trees.authoring).as_posix() for source in sources}
    missing = {path for path in tracked if path.startswith("articles/") and
               path.endswith(".md") and path not in existing}
    missing.update(path for entry in entries for path in (entry.path, entry.original_path)
                   if path and path.startswith("articles/") and path.endswith(".md")
                   and path not in existing)
    if missing:
      raise GuardError(f"Missing source state for removed paths: {sorted(missing)!r}")
    images = _files(trees.authoring, "images")
    validate_inputs(tuple((source.path.relative_to(trees.authoring).as_posix(), source.state)
                          for source in sources),
                    tuple(path.relative_to(trees.authoring).as_posix() for path in images),
                    tracked, entries)
    ledger = read_ledger(trees.deploy)
    if ledger["pending_next_state"]:
      raise GuardError("Unresolved pending_next_state")
    return CheckedInputs(trees.authoring, trees.deploy, tuple(sources), images)
  except GuardError:
    raise
  except (OSError, ValueError, RuntimeError) as exc:
    raise GuardError(f"Input inspection failed: {exc}") from exc
