"""検証済み DELETE SUCCESS をローカル状態に適用します。"""

from pathlib import Path

from checker.atomic_write import atomic_replace_bytes
from checker.errors import LocalApplyError
from checker.local_state import ledger_bytes, read_ledger, safe_path
from checker.terminal_create import _SLUG, _validate_article_id, _validate_observation


def read_delete_pending(ledger: dict, article_id: str) -> dict:
  """confirmed state と pending DELETE の整合性を検証します。"""
  _validate_article_id(article_id)
  if not isinstance(ledger, dict) or not all(
      isinstance(ledger.get(key), dict) for key in ("articles", "pending_next_state")):
    raise LocalApplyError("Invalid ledger maps")
  confirmed = ledger["articles"].get(article_id)
  pending = ledger["pending_next_state"].get(article_id)
  if not isinstance(confirmed, dict) or confirmed.get("state") != "active":
    raise LocalApplyError("DELETE requires an active confirmed article")
  if not isinstance(pending, dict) or pending.get("operation") != "DELETE":
    raise LocalApplyError("No pending DELETE for the requested article")
  if pending.get("state") != "erased":
    raise LocalApplyError("Pending DELETE target must be erased")
  slug = pending.get("slug")
  if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug != confirmed.get("slug"):
    raise LocalApplyError("Pending DELETE slug must equal valid confirmed slug")
  previous = confirmed.get("successful_release")
  release = pending.get("release")
  if type(previous) is not int or previous < 1 or type(release) is not int or release != previous:
    raise LocalApplyError("Pending DELETE release must equal successful release")
  _validate_observation(confirmed.get("successful_zenn_observation"))
  return pending


def apply_delete_success(deploy_root: Path, article_id: str) -> None:
  """再観測で確認済みの SUCCESS を ledger の atomic replacement で確定します。

  snapshot は保持し、pending 削除を article-level commit point とします。
  commit 前の中断後は再観測して再実行します。commit 後の再適用は拒否します。
  """
  root = Path(deploy_root)
  ledger = read_ledger(root)
  pending = read_delete_pending(ledger, article_id)
  artifact_path = safe_path(root, "articles", f"{pending['slug']}.md")
  if artifact_path.exists():
    raise LocalApplyError("DELETE requires an absent deploy artifact")
  snapshot_path = safe_path(root, ".zenn-authoring", "released", f"{article_id}.md")
  if not snapshot_path.is_file():
    raise LocalApplyError("Missing regular released snapshot")
  ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
  confirmed = dict(ledger["articles"][article_id])
  confirmed["state"] = "erased"
  next_ledger = dict(ledger)
  next_ledger["articles"] = dict(ledger["articles"])
  next_ledger["articles"][article_id] = confirmed
  next_ledger["pending_next_state"] = dict(ledger["pending_next_state"])
  del next_ledger["pending_next_state"][article_id]
  atomic_replace_bytes(ledger_path, ledger_bytes(next_ledger))
