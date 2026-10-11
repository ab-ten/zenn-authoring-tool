"""検査済み画像入力を全件検証し、読み取り専用の mirror diff を計画します。"""

import os
from pathlib import Path
import stat

from converter.entry_guards import CheckedInputs
from converter.git_inspection import GuardError, head_entries
from converter.models import ImagePlanEntry, ImagePlanningResult


EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
MAX_BYTES = 3_000_000


def _safe_image_path(root: Path, path: Path) -> str:
  """親を含めて通常ディレクトリを確認し、画像 tree 外への参照を拒否します。"""
  relative = path.relative_to(root)
  parts = relative.parts
  if len(parts) < 2 or parts[0] != "images" or any(p in (".", "..") for p in parts):
    raise GuardError(f"Unsafe image path: {path}")
  for parent in (root, *(root.joinpath(*parts[:i]) for i in range(1, len(parts)))):
    if not stat.S_ISDIR(parent.lstat().st_mode):
      raise GuardError(f"Unsafe image directory: {parent}")
  return relative.as_posix()


def _read_image(root: Path, path: Path, *, limit: int | None = None) -> bytes:
  _safe_image_path(root, path)
  if not stat.S_ISREG(path.lstat().st_mode):
    raise GuardError(f"Unsafe filesystem image: {path}")
  # 最終 object の差し替え時にも symlink を辿らず、FIFO で待機しません。
  descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
  with os.fdopen(descriptor, "rb") as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode):
      raise GuardError(f"Unsafe filesystem image: {path}")
    if limit is not None and info.st_size > limit:
      raise GuardError(f"Image exceeds {limit} bytes: {path}")
    content = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(content) > limit:
      raise GuardError(f"Image exceeds {limit} bytes: {path}")
    return content


def _validate_images(inputs: CheckedInputs) -> dict[str, bytes]:
  entries = head_entries(inputs.authoring_root)
  for path, entry in entries.items():
    if (path == "images" or path.startswith("images/")) and entry not in (
        ("100644", "blob"), ("100755", "blob")):
      raise GuardError(f"Image requires normal Git blob: {path!r}")
  contents = {}
  for path in inputs.images:
    relative = _safe_image_path(inputs.authoring_root, path)
    if relative in contents:
      raise GuardError(f"Duplicate image input: {relative!r}")
    if entries.get(relative) not in (("100644", "blob"), ("100755", "blob")):
      raise GuardError(f"Image requires normal Git blob: {relative!r}")
    if path.suffix not in EXTENSIONS:
      raise GuardError(f"Unsupported image extension: {relative!r}")
    contents[relative] = _read_image(inputs.authoring_root, path, limit=MAX_BYTES)
  return contents


def _deploy_images(root: Path) -> dict[str, bytes]:
  for path, entry in head_entries(root).items():
    if (path == "images" or path.startswith("images/")) and entry not in (
        ("100644", "blob"), ("100755", "blob")):
      raise GuardError(f"Unsafe deploy image Git entry (requires normal blob): {path!r}")
  base = root / "images"
  if not stat.S_ISDIR(root.lstat().st_mode):
    raise GuardError(f"Unsafe deploy root: {root}")
  try:
    mode = base.lstat().st_mode
  except FileNotFoundError:
    return {}
  if not stat.S_ISDIR(mode):
    raise GuardError(f"Unsafe deploy images directory: {base}")
  contents = {}

  def walk(directory):
    for path in sorted(directory.iterdir()):
      mode = path.lstat().st_mode
      if stat.S_ISDIR(mode):
        walk(path)
      elif stat.S_ISREG(mode):
        relative = _safe_image_path(root, path)
        contents[relative] = _read_image(root, path)
      else:
        raise GuardError(f"Unsafe deploy filesystem image: {path}")

  walk(base)
  return contents


def _mirror_diff(authoring: dict[str, bytes], deploy: dict[str, bytes]) -> ImagePlanningResult:
  changes = []
  for path in sorted(authoring.keys() | deploy.keys()):
    if path not in authoring:
      changes.append(ImagePlanEntry("DELETE", path))
    elif path not in deploy:
      changes.append(ImagePlanEntry("ADD", path, authoring[path]))
    elif authoring[path] != deploy[path]:
      changes.append(ImagePlanEntry("UPDATE", path, authoring[path]))
  return ImagePlanningResult(tuple(changes))


def plan_images(inputs: CheckedInputs) -> ImagePlanningResult:
  """CheckedInputs の画像だけを検証し、全件成功後に deploy を比較します。

  authoring filesystem の再探索や Guard の再実行は行いません。
  """
  try:
    authoring = _validate_images(inputs)
    deploy = _deploy_images(inputs.deploy_root)
    return _mirror_diff(authoring, deploy)
  except GuardError:
    raise
  except (OSError, ValueError) as exc:
    raise GuardError(f"Image planning failed: {exc}") from exc
