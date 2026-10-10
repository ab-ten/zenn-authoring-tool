"""CREATE / RECREATE COLLISION の復旧可能な cleanup を提供します。"""

from pathlib import Path

from checker.atomic_write import atomic_replace_bytes
from checker.local_state import ledger_bytes, read_ledger, safe_path
from checker.terminal_create import (
  LocalApplyError, _read_create_pending, _validate_article_id,
)
from checker.terminal_recreate import read_recreate_pending


def apply_collision(deploy_root: Path, article_id: str) -> None:
  """artifact 削除後に pending のみを atomic replacement で除去します。

  呼び出し元は全 observation を完了し、COLLISION を確認する必要があります。
  削除済み artifact は no-op とし、ledger 更新前の中断から復旧できます。
  confirmed state、snapshot、authoring source は変更しません。
  """
  _validate_article_id(article_id)
  root = Path(deploy_root)
  ledger = read_ledger(root)
  pending = ledger["pending_next_state"].get(article_id)
  if not isinstance(pending, dict):
    raise LocalApplyError("Missing pending collision entry")
  if pending.get("operation") == "CREATE":
    pending = _read_create_pending(ledger, article_id)
  elif pending.get("operation") == "RECREATE":
    pending = read_recreate_pending(ledger, article_id)
  else:
    raise LocalApplyError("COLLISION supports only CREATE / RECREATE")

  slug = pending["slug"]
  for confirmed_id, entry in ledger["articles"].items():
    if not isinstance(entry, dict):
      raise LocalApplyError("Invalid confirmed article")
    if entry.get("slug") == slug and confirmed_id != article_id:
      raise LocalApplyError("Collision slug overlaps confirmed state")
  for other_id, entry in ledger["pending_next_state"].items():
    if not isinstance(entry, dict):
      raise LocalApplyError("Invalid pending entry")
    if other_id != article_id and entry.get("slug") == slug:
      raise LocalApplyError("Collision slug overlaps another pending")

  artifact = safe_path(root, "articles", f"{slug}.md")
  ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
  if artifact.exists() and not artifact.is_file():
    raise LocalApplyError("Collision artifact must be a regular file")
  next_ledger = dict(ledger)
  next_ledger["pending_next_state"] = dict(ledger["pending_next_state"])
  del next_ledger["pending_next_state"][article_id]
  content = ledger_bytes(next_ledger)
  artifact.unlink(missing_ok=True)
  atomic_replace_bytes(ledger_path, content)
