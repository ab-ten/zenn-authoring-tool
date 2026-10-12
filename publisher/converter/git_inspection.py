"""明示された worktree の Git 情報を読み取り専用で取得します。"""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


class GuardError(ValueError):
  """Converter entry guard の ERROR を表します。"""

  def __init__(self, message: str, *, returncode: int | None = None):
    super().__init__(message)
    self.returncode = returncode


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
  # git-status の XY 表に従い、intent-to-add と worktree rename/copy も許容します。
  valid.update(" " + y for y in "AMDRC")
  valid.add("D ")
  valid.update(x + y for x in "MTARC" for y in " MTD")
  valid.add(" T")
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
    raise GuardError(f"Git failed at {root} ({args[0]}): {os.fsdecode(result.stderr)!r}",
                     returncode=result.returncode)
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


def identify_worktrees(article_root: Path, *, allow_unborn_deploy: bool = False) -> Worktrees:
  """明示した記事 root の branch、metadata、相対リンクを読み取り専用で検証します。

  通常は両 worktree の HEAD commit を要求します。init 用オプションでは、
  refs/heads/deploy の不存在を Git が確認した場合のみ unborn を許容します。
  検証済み Worktrees を返し、不正な状態や Git 実行失敗では GuardError を送出します。
  """
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
    unborn = False
    if target == deploy and allow_unborn_deploy:
      try:
        git(target, "show-ref", "--exists", "refs/heads/deploy")
      except GuardError as exc:
        # --exists の 2 は不存在、1 やその他の終了コードは検証失敗です。
        if exc.returncode != 2:
          raise
        unborn = True
    if not unborn:
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


def head_entries(root: Path) -> dict[str, tuple[str, str]]:
  """NUL 区切りの HEAD tree から path ごとの mode と object type を取得します。"""
  output = git(root, "ls-tree", "-r", "-z", "HEAD")
  if output and not output.endswith(b"\0"):
    raise GuardError("Malformed Git HEAD entries")
  entries = {}
  for raw in output[:-1].split(b"\0") if output else ():
    header, separator, name = raw.partition(b"\t")
    fields = header.split(b" ")
    if not separator or len(fields) != 3:
      raise GuardError("Malformed Git HEAD entry")
    try:
      mode, kind, oid = (field.decode("ascii") for field in fields)
    except UnicodeError as exc:
      raise GuardError("Malformed Git HEAD entry") from exc
    if (len(mode) != 6 or any(c not in "01234567" for c in mode) or
        kind not in ("blob", "tree", "commit") or len(oid) not in (40, 64) or
        any(c not in "0123456789abcdef" for c in oid)):
      raise GuardError("Malformed Git HEAD entry")
    path = _path(name)
    if path in entries:
      raise GuardError("Duplicate Git HEAD entry")
    entries[path] = (mode, kind)
  return entries


def parse_index_flags(output: bytes) -> frozenset[str]:
  """ls-files -v -z から cleanliness を隠す index flags の path を取得します。"""
  if not output:
    return frozenset()
  if not output.endswith(b"\0"):
    raise GuardError("Malformed Git index flags: missing NUL terminator")
  flagged = set()
  for raw in output[:-1].split(b"\0"):
    if len(raw) < 3 or raw[1:2] != b" " or raw[:1] not in (
        b"H", b"S", b"M", b"R", b"C", b"K", b"?", b"U",
        b"h", b"s", b"m", b"r", b"c", b"k", b"u"):
      raise GuardError("Malformed Git index flag record")
    path = _path(raw[2:])
    if raw[:1].islower() or raw[:1] == b"S":
      flagged.add(path)
  return frozenset(flagged)


def inspect_index_flags(root: Path) -> frozenset[str]:
  return parse_index_flags(git(root, "ls-files", "-v", "-z"))


def head_source(root: Path, path: str) -> str:
  """検証済み repository-relative path の HEAD content を読み込みます。"""
  _path(os.fsencode(path))
  return git(root, "show", "HEAD:" + path).decode("utf-8")


def commit_and_parent(root: Path) -> tuple[str, str]:
  """HEAD と唯一の parent commit を検証して取得します。"""
  head = _line(root, "rev-parse", "--verify", "HEAD^{commit}")
  def valid(value):
    return len(value) in (40, 64) and all(c in "0123456789abcdef" for c in value)
  if not valid(head):
    raise GuardError("Malformed HEAD commit ID")
  fields = _line(root, "rev-list", "--parents", "-n", "1", head).split(" ")
  if len(fields) != 2 or fields[0] != head or not valid(fields[1]):
    raise GuardError("HEAD requires exactly one parent; root/merge commits refused")
  parent = _line(root, "rev-parse", "--verify", fields[1] + "^{commit}")
  if parent != fields[1]:
    raise GuardError("Invalid parent commit")
  return head, parent


def commit_tree(root: Path, commit: str) -> dict[str, tuple[str, str]]:
  """commit tree の regular blob path と object ID を取得します。"""
  if len(commit) not in (40, 64) or any(c not in "0123456789abcdef" for c in commit):
    raise GuardError("Invalid commit ID")
  output = git(root, "ls-tree", "-r", "-z", commit)
  if output and not output.endswith(b"\0"):
    raise GuardError("Malformed commit tree")
  result = {}
  verified_objects = set()
  for raw in output[:-1].split(b"\0") if output else ():
    header, separator, name = raw.partition(b"\t")
    fields = header.split(b" ")
    if not separator or len(fields) != 3:
      raise GuardError("Malformed commit tree entry")
    mode, kind, oid = fields
    if (mode not in (b"100644", b"100755") or kind != b"blob" or
        len(oid) not in (40, 64) or any(c not in b"0123456789abcdef" for c in oid)):
      raise GuardError("Unsafe or malformed commit tree entry")
    path = _path(name)
    if "\\" in path or path in result:
      raise GuardError("Unsafe or duplicate commit tree path")
    if oid not in verified_objects:
      if git(root, "cat-file", "-e", oid.decode("ascii") + "^{blob}"):
        raise GuardError("Malformed blob existence output")
      verified_objects.add(oid)
    result[path] = (mode.decode("ascii"), oid.decode("ascii"))
  return result


def read_tree_blob(root: Path, tree: dict[str, tuple[str, str]], path: str) -> bytes:
  """取得済み tree の blob を読み込み、欠落 object を拒否します。"""
  _path(os.fsencode(path))
  if path not in tree:
    raise GuardError(f"Missing tree blob: {path}")
  return git(root, "cat-file", "blob", tree[path][1])


def inspect_history_metadata(root: Path) -> None:
  """履歴の代替や進行中の Git 操作を検出し、通常の commit 解釈を要求します。"""
  from checker.local_state import safe_path

  metadata = Path(_line(root, "rev-parse", "--absolute-git-dir"))
  common = Path(_line(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
  for base in (metadata, common):
    safe_path(base)
    for name in ("index", "HEAD", "gitdir", "commondir"):
      path = safe_path(base, name)
      if path.exists() and not path.is_file():
        raise GuardError(f"Invalid Git metadata file: {path}")
    for name in ("index.lock", "HEAD.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                 "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer"):
      path = safe_path(base, name)
      if path.exists():
        raise GuardError(f"Git operation in progress: {name}")
  grafts = safe_path(common, "info", "grafts")
  if grafts.exists() or git(root, "for-each-ref", "--format=%(refname)", "refs/replace"):
    raise GuardError("Alternate Git history (grafts/replace refs) refused")
