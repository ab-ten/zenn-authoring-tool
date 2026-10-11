"""Atomic replacement helpers for management files and released snapshots."""

import os
from pathlib import Path
import tempfile


def atomic_replace_bytes(target: Path, content: bytes, *, mode: int | None = None) -> bool:
  """Atomically replace an existing file or create a new one.

  The target's parent directory must exist. The temporary file is created
  beside the target so that os.replace() does not cross filesystems.
  Returns False if the target already contains identical bytes.
  On failure before replacement, the previous target is left unchanged.
  mode を指定した場合、一時ファイルに指定の権限を設定してから置換します。
  省略した場合は既存の動作を維持します。
  """
  target = Path(target)
  if target.is_symlink():
    raise ValueError(f"Refusing to replace symlink: {target}")

  try:
    if target.read_bytes() == content:
      return False
  except FileNotFoundError:
    pass

  fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.",
                                   suffix=".tmp", dir=target.parent)
  try:
    with os.fdopen(fd, "wb") as stream:
      if mode is not None:
        os.fchmod(stream.fileno(), mode)
      stream.write(content)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(tmp_name, target)
    return True
  finally:
    if os.path.exists(tmp_name):
      os.unlink(tmp_name)
