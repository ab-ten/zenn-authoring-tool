"""明示された worktree の Git 情報を読み取り専用で取得します。"""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


class GuardError(ValueError):
  """Converter entry guard の ERROR を表します。"""


@dataclass(frozen=True)
class StatusEntry:
  code: str
  path: str
  original_path: str | None = None


def _path(raw: bytes, directory: bool = False) -> str:
  path = os.fsdecode(raw)
  parts = path[:-1].split("/") if directory and path.endswith("/") else path.split("/")
  if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
    raise GuardError(f"Invalid Git path: {path!r}")
  return path


def parse_status(output: bytes) -> tuple[StatusEntry, ...]:
  """porcelain v1 -z を解析し、rename/copy の両 path を保持します。"""
  if not output:
    return ()
  if not output.endswith(b"\0"):
    raise GuardError("Malformed Git status: missing NUL terminator")
  records = iter(output[:-1].split(b"\0"))
  result = []
  valid = {"??", "!!", "DD", "AU", "UD", "UA", "DU", "AA", "UU"}
  valid.update(x + y for x in " MTADRC" for y in " MTD" if x + y != "  ")
  for raw in records:
    if len(raw) < 4 or raw[2:3] != b" ":
      raise GuardError("Malformed Git status record")
    try:
      code = raw[:2].decode("ascii")
    except UnicodeError as exc:
      raise GuardError("Malformed Git status code") from exc
    if code not in valid:
      raise GuardError(f"Malformed Git status code: {code!r}")
    path = _path(raw[3:], directory=code in ("??", "!!"))
    original = None
    if "R" in code or "C" in code:
      try:
        original = _path(next(records))
      except StopIteration as exc:
        raise GuardError("Malformed Git rename/copy status") from exc
    result.append(StatusEntry(code, path, original))
  return tuple(result)


def git(root: Path, *args: str) -> bytes:
  """継承した Git 環境を除去し、shell を使用せず Git を実行します。"""
  env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
  env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
  try:
    result = subprocess.run(
      ["git", "--no-optional-locks", "-C", os.fspath(root), *args],
      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
  except OSError as exc:
    raise GuardError(f"Git execution failed at {root}: {exc}") from exc
  if result.returncode:
    raise GuardError(f"Git failed at {root} ({args[0]}): {os.fsdecode(result.stderr)!r}")
  return result.stdout


def _line(root: Path, *args: str) -> str:
  raw = git(root, *args)
  if not raw.endswith(b"\n") or b"\0" in raw:
    raise GuardError("Malformed Git identity output")
  return os.fsdecode(raw[:-1])


@dataclass(frozen=True)
class Worktrees:
  authoring: Path
  deploy: Path


def identify_worktrees(article_root: Path) -> Worktrees:
  """branch、共通 metadata、相互リンク、相対リンクを検証します。"""
  if article_root is None:
    raise GuardError("Explicit article repository root is required")
  root = Path(article_root).absolute()
  if not root.is_dir() or root.is_symlink():
    raise GuardError(f"Invalid article repository root: {root}")
  root = root.resolve()
  development = Path(__file__).resolve().parents[2]
  if root == development or ((root / "docs" / "architecture.md").is_file() and
                             (root / "publisher" / "converter" / "source_parser.py").is_file()):
    raise GuardError("Development repository cannot be an article target")
  deploy = root / "_deploy"
  if not deploy.is_dir() or deploy.is_symlink():
    raise GuardError("Missing or invalid deploy worktree")
  identities = []
  for target, branches in ((root, {"refs/heads/main", "refs/heads/master"}),
                           (deploy, {"refs/heads/deploy"})):
    if Path(_line(target, "rev-parse", "--show-toplevel")).resolve() != target:
      raise GuardError(f"Incorrect worktree root: {target}")
    if _line(target, "symbolic-ref", "HEAD") not in branches:
      raise GuardError(f"Incorrect worktree branch: {target}")
    common = Path(_line(target, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    metadata = Path(_line(target, "rev-parse", "--absolute-git-dir")).resolve()
    git(target, "rev-parse", "--verify", "HEAD^{commit}")
    identities.append((common, metadata))
  common, authoring_git = identities[0]
  deploy_common, deploy_git = identities[1]
  if common != deploy_common or authoring_git == deploy_git or deploy_git.parent != common / "worktrees":
    raise GuardError("Unrelated or inconsistent worktree metadata")
  try:
    forward = (deploy / ".git").read_bytes()
    backward = (deploy_git / "gitdir").read_bytes()
    if not forward.startswith(b"gitdir: ") or not forward.endswith(b"\n") or not backward.endswith(b"\n"):
      raise GuardError("Invalid worktree links")
    forward_path = Path(os.fsdecode(forward[8:-1]))
    backward_path = Path(os.fsdecode(backward[:-1]))
    if forward_path.is_absolute() or backward_path.is_absolute():
      raise GuardError("Relative worktree links are required")
    if (deploy / forward_path).resolve() != deploy_git or (deploy_git / backward_path).resolve() != deploy / ".git":
      raise GuardError("Inconsistent worktree links")
  except OSError as exc:
    raise GuardError(f"Cannot read worktree metadata: {exc}") from exc
  return Worktrees(root, deploy)


def inspect_status(root: Path) -> tuple[StatusEntry, ...]:
  return parse_status(git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))


def head_paths(root: Path) -> frozenset[str]:
  output = git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD")
  if output and not output.endswith(b"\0"):
    raise GuardError("Malformed Git HEAD paths")
  return frozenset(_path(raw) for raw in output[:-1].split(b"\0")) if output else frozenset()
