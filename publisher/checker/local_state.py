"""ローカル状態の読み込みと安全なファイルパスの検証を提供します。"""

import json
from pathlib import Path
import stat

from checker.errors import LocalApplyError


def safe_path(root: Path, *parts: str) -> Path:
  """親ディレクトリを含め、symlink と特殊ファイルを拒否します。"""
  root = Path(root).absolute()
  target = root.joinpath(*parts)
  if not target.is_relative_to(root) or any(
      part in (".", "..") or "/" in part or "\\" in part for part in parts):
    raise LocalApplyError("Unsafe local path")
  for path in (*reversed(target.parents), target):
    try:
      mode = path.lstat().st_mode
    except FileNotFoundError:
      continue
    if stat.S_ISLNK(mode):
      raise LocalApplyError(f"Refusing symlink: {path}")
    if path == target:
      if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise LocalApplyError(f"Refusing special file: {path}")
    elif not stat.S_ISDIR(mode):
      raise LocalApplyError(f"Not a directory: {path}")
  return target


def read_ledger(root: Path) -> dict:
  """既存 ledger の必須 map を検証して読み込みます。"""
  path = safe_path(root, ".zenn-authoring", "ledger.json")
  ledger = json.loads(path.read_bytes())
  if not isinstance(ledger, dict) or not all(
      isinstance(ledger.get(key), dict) for key in ("articles", "pending_next_state")):
    raise LocalApplyError("Invalid ledger maps")
  return ledger


def ledger_bytes(ledger: dict) -> bytes:
  """atomic replacement 用の JSON を生成します。"""
  return (json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2)
          + "\n").encode("utf-8")
