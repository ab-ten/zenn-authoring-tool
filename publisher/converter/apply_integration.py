"""固定済み計画の artifact を適用し、最後に準備済み pending を永続化します。"""

from pathlib import Path
import stat

from checker.atomic_write import atomic_replace_bytes
from checker.errors import LocalApplyError
from checker.local_state import safe_path
from converter.apply_preparation import ApplyPreparation, prepare_apply
from converter.preflight_integration import PreflightOutcome


def apply_publish(outcome: PreflightOutcome) -> None:
  """一度だけ読み取り専用の準備を行い、認可済みの対象を逐次適用します。"""
  _apply_prepared(prepare_apply(outcome))


def _checked(root: Path, path: Path, *, absent: bool = False) -> Path:
  """実行直前に親と対象を検証し、操作の存在条件を確認します。"""
  checked = safe_path(root, *path.relative_to(root).parts)
  if absent:
    if checked.exists():
      raise LocalApplyError(f"Refusing unexpected existing target: {checked}")
  elif not checked.is_file():
    raise LocalApplyError(f"Expected existing regular file: {checked}")
  return checked


def _parents(root: Path, path: Path, tree: str) -> None:
  """管理 tree 内だけに、通常ディレクトリを一段ずつ作成します。"""
  parts = path.relative_to(root).parts
  if len(parts) < 2 or parts[0] != tree:
    raise LocalApplyError(f"Outside managed {tree} tree: {path}")
  for length in range(1, len(parts)):
    parent = safe_path(root, *parts[:length])
    if parent.exists():
      if not parent.is_dir():
        raise LocalApplyError(f"Expected parent directory: {parent}")
    else:
      parent.mkdir()


def _apply_prepared(prepared: ApplyPreparation) -> None:
  """準備済みの順序と内容を保持し、失敗時は後続操作を実行しません。

  自動 rollback は行いません。エラーには完了した操作数を含めますが、
  失敗した操作自体もディレクトリ作成等の部分変更を残す可能性があります。
  ledger 置換が commit point であり、その後の処理は行いません。
  """
  root = prepared.outcome.deploy_root
  completed = 0
  context = "Apply"
  try:
    for target in prepared.article_targets:
      entry, path = target.entry, target.path
      context = f"Article {entry.operation} {path}"
      if entry.operation == "DELETE":
        _checked(root, path).unlink()
      else:
        content = entry.candidate_content.encode("utf-8")
        absent = entry.operation in ("CREATE", "RECREATE")
        _checked(root, path, absent=absent)
        _parents(root, path, "articles")
        atomic_replace_bytes(_checked(root, path, absent=absent), content)
      completed += 1

    for target in prepared.image_deletes:
      context = f"Image DELETE {target.path}"
      _checked(root, target.path).unlink()
      completed += 1

    for path in prepared.image_directories_to_remove:
      context = f"Image directory removal {path}"
      checked = safe_path(root, *path.relative_to(root).parts)
      if not checked.is_dir():
        raise LocalApplyError(f"Expected existing image directory: {checked}")
      checked.rmdir()
      completed += 1

    for target in prepared.image_writes:
      entry, path = target.entry, target.path
      context = f"Image {entry.operation} {path}"
      absent = entry.operation == "ADD"
      _checked(root, path, absent=absent)
      _parents(root, path, "images")
      checked = _checked(root, path, absent=absent)
      # 既存画像の通常権限を置換前に保持し、実行ビットの変更を防ぎます。
      mode = stat.S_IMODE(checked.stat().st_mode) if not absent else None
      atomic_replace_bytes(checked, entry.content, mode=mode)
      completed += 1

    if prepared.pending_ledger_bytes is not None:
      path = root / ".zenn-authoring" / "ledger.json"
      context = f"Pending ledger replacement {path}"
      atomic_replace_bytes(_checked(root, path), prepared.pending_ledger_bytes)
  except (OSError, ValueError, TypeError) as exc:
    raise LocalApplyError(
      f"{context} failed; {completed} preceding operations completed; "
      f"the failed operation may have left changes: {exc}") from exc
