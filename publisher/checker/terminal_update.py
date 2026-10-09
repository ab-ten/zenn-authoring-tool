"""検証済み UPDATE SUCCESS をローカル状態に適用します。"""

from pathlib import Path

from checker.atomic_write import atomic_replace_bytes
from checker.errors import LocalApplyError
from checker.local_state import ledger_bytes, read_ledger, safe_path
from checker.terminal_create import _SLUG, _validate_article_id, _validate_observation


def read_update_pending(ledger: dict, article_id: str) -> dict:
  """confirmed state と pending UPDATE の整合性を検証します。"""
  _validate_article_id(article_id)
  if not isinstance(ledger, dict) or not all(
      isinstance(ledger.get(key), dict) for key in ("articles", "pending_next_state")):
    raise LocalApplyError("Invalid ledger maps")
  confirmed = ledger["articles"].get(article_id)
  pending = ledger["pending_next_state"].get(article_id)
  if not isinstance(confirmed, dict) or confirmed.get("state") != "active":
    raise LocalApplyError("UPDATE requires an active confirmed article")
  if not isinstance(pending, dict) or pending.get("operation") != "UPDATE":
    raise LocalApplyError("No pending UPDATE for the requested article")
  if pending.get("state") != "active":
    raise LocalApplyError("Pending UPDATE target must be active")
  slug = pending.get("slug")
  if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug != confirmed.get("slug"):
    raise LocalApplyError("Pending UPDATE slug must equal valid confirmed slug")
  previous = confirmed.get("successful_release")
  release = pending.get("release")
  if type(previous) is not int or previous < 1 or type(release) is not int or release != previous + 1:
    raise LocalApplyError("Pending UPDATE release must be the next successful release")
  _validate_observation(confirmed.get("successful_zenn_observation"))
  _validate_observation(pending.get("baseline_observation"))
  return pending


def apply_update_success(deploy_root: Path, article_id: str, observation: dict) -> None:
  """観測済み SUCCESS の snapshot を更新した後、ledger を commit します。

  呼び出し元が自ユーザー記事の SUCCESS を確認済みであることを前提とします。
  pending の削除を commit point とし、中断後は再観測して再実行します。
  snapshot は既に更新済みの場合があるため、release の再判定には使用しません。
  """
  checked_observation = _validate_observation(observation)
  root = Path(deploy_root)
  ledger = read_ledger(root)
  pending = read_update_pending(ledger, article_id)
  ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
  snapshot_path = safe_path(root, ".zenn-authoring", "released", f"{article_id}.md")
  if snapshot_path.exists() and not snapshot_path.is_file():
    raise LocalApplyError("Not a regular released snapshot")
  artifact_path = safe_path(root, "articles", f"{pending['slug']}.md")
  if not artifact_path.is_file():
    raise LocalApplyError("Missing regular deploy artifact")
  content = artifact_path.read_bytes()
  confirmed = dict(ledger["articles"][article_id])
  confirmed["successful_release"] = pending["release"]
  confirmed["successful_zenn_observation"] = checked_observation
  next_ledger = dict(ledger)
  next_ledger["articles"] = dict(ledger["articles"])
  next_ledger["articles"][article_id] = confirmed
  next_ledger["pending_next_state"] = dict(ledger["pending_next_state"])
  del next_ledger["pending_next_state"][article_id]
  target_ledger = ledger_bytes(next_ledger)
  atomic_replace_bytes(snapshot_path, content)
  atomic_replace_bytes(ledger_path, target_ledger)
