"""検証済み RECREATE SUCCESS をローカル状態に適用します。"""

from pathlib import Path

from checker.atomic_write import atomic_replace_bytes
from checker.errors import LocalApplyError
from checker.local_state import ledger_bytes, read_ledger, safe_path
from checker.terminal_create import _SLUG, _validate_article_id, _validate_observation


def read_recreate_pending(ledger: dict, article_id: str) -> dict:
  """confirmed erased state と確定済み pending transition を検証します。"""
  _validate_article_id(article_id)
  if not isinstance(ledger, dict) or not all(
      isinstance(ledger.get(key), dict) for key in ("articles", "pending_next_state")):
    raise LocalApplyError("Invalid ledger maps")
  confirmed = ledger["articles"].get(article_id)
  pending = ledger["pending_next_state"].get(article_id)
  if not isinstance(confirmed, dict) or confirmed.get("state") != "erased":
    raise LocalApplyError("RECREATE requires an erased confirmed article")
  if not isinstance(pending, dict) or pending.get("operation") != "RECREATE":
    raise LocalApplyError("No pending RECREATE for the requested article")
  if pending.get("state") != "active":
    raise LocalApplyError("Pending RECREATE target must be active")
  for slug in (confirmed.get("slug"), pending.get("slug")):
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
      raise LocalApplyError("Invalid RECREATE slug")
  previous = confirmed.get("successful_release")
  release = pending.get("release")
  if (type(previous) is not int or previous < 1 or type(release) is not int
      or release not in (previous, previous + 1)):
    raise LocalApplyError("Pending RECREATE release must be N or N+1")
  _validate_observation(confirmed.get("successful_zenn_observation"))
  for entries in (ledger["articles"], ledger["pending_next_state"]):
    for other_id, entry in entries.items():
      if not isinstance(entry, dict):
        raise LocalApplyError("Invalid local article entry")
      if other_id != article_id and entry.get("slug") in (confirmed["slug"], pending["slug"]):
        raise LocalApplyError("RECREATE slug overlaps another managed article")
  return pending


def apply_recreate_success(deploy_root: Path, article_id: str, observation: dict) -> None:
  """snapshot 更新後に confirmed state 更新と pending 削除を commit します。

  呼び出し元が pending slug の自ユーザー publication を SUCCESS と確認済みで
  あることを前提とします。release と slug 変更の内容検証は converter / preflight
  の責務です。中断復旧では snapshot の内容から release を再判定しません。
  """
  checked_observation = _validate_observation(observation)
  root = Path(deploy_root)
  ledger = read_ledger(root)
  pending = read_recreate_pending(ledger, article_id)
  artifact = safe_path(root, "articles", f"{pending['slug']}.md")
  if not artifact.is_file():
    raise LocalApplyError("Missing regular deploy artifact")
  content = artifact.read_bytes()
  snapshot = safe_path(root, ".zenn-authoring", "released", f"{article_id}.md")
  if not snapshot.is_file():
    raise LocalApplyError("Missing regular released snapshot")
  ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
  confirmed = dict(ledger["articles"][article_id])
  confirmed.update(state="active", slug=pending["slug"],
                   successful_release=pending["release"],
                   successful_zenn_observation=checked_observation)
  next_ledger = dict(ledger)
  next_ledger["articles"] = dict(ledger["articles"])
  next_ledger["articles"][article_id] = confirmed
  next_ledger["pending_next_state"] = dict(ledger["pending_next_state"])
  del next_ledger["pending_next_state"][article_id]
  target_ledger = ledger_bytes(next_ledger)
  atomic_replace_bytes(snapshot, content)
  atomic_replace_bytes(ledger_path, target_ledger)
