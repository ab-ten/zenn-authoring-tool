"""pending を変更せず、discard のローカル復旧前提を検証します。"""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path

from checker.check_pending import _validate_slug_ownership
from checker.local_state import safe_path
from checker.terminal_create import (
  _SLUG, _read_create_pending, _validate_article_id, _validate_observation,
)
from checker.terminal_update import read_update_pending
from checker.terminal_delete import read_delete_pending
from checker.terminal_recreate import read_recreate_pending
from converter.git_inspection import (
  commit_and_parent, commit_tree, identify_worktrees,
  inspect_history_metadata, inspect_index_flags, inspect_status, read_tree_blob,
)


class LocalValidation(StrEnum):
  """remote 確認とは独立したローカル検証状態です。"""

  LOCAL_PREREQUISITES_VERIFIED = "LOCAL_PREREQUISITES_VERIFIED"
  LOCAL_VALIDATION_REJECTED = "LOCAL_VALIDATION_REJECTED"


@dataclass(frozen=True)
class PendingValidation:
  """記事別検証結果と未完了の user verification を保持します。"""

  article_id: str
  operation: str | None
  slug: str | None
  restore_path: str | None
  diagnostics: tuple[str, ...] = ()
  remote_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryPlan:
  """remote 確認後の 03b と host 側復旧に必要な情報です。"""

  deploy_root: Path
  expected_head: str
  source_commit: str
  ledger_sha256: str
  article_paths: tuple[str, ...]
  restore_argv: tuple[str, ...]
  remote_requirements: tuple[str, ...]
  post_discard_guidance: tuple[str, ...]


@dataclass(frozen=True)
class PreflightResult:
  """破棄許可や commit readiness を表さない read-only 結果です。"""

  local_validation: LocalValidation
  deploy_root: Path | None = None
  head_commit: str | None = None
  parent_commit: str | None = None
  ledger_sha256: str | None = None
  pending: tuple[PendingValidation, ...] = ()
  diagnostics: tuple[str, ...] = ()
  snapshot_anomalies: tuple[str, ...] = ()
  recovery_plan: RecoveryPlan | None = None
  remote_confirmation_pending: bool = True
  remote_requirements: tuple[str, ...] = ()


_VALIDATORS = {"CREATE": _read_create_pending, "UPDATE": read_update_pending,
               "DELETE": read_delete_pending, "RECREATE": read_recreate_pending}
_REMOTE = {
  "CREATE": "Pending publication が自ユーザー publication として成立していないことを確認します。",
  "UPDATE": "Current remote observation が baseline / previous successful state と整合することを確認します。",
  "DELETE": "以前の successful publication が削除されていないことを確認します。",
  "RECREATE": "Pending publication が自ユーザー publication として成立していないことを確認します。",
}
_TERMINAL = (
  "Zenn Dashboard 等で対象 deploy が terminal であり、今後 pending transition が適用されないことを確認します。",
  "Article API observation のみでは deployment の terminal 状態を証明できません。",
)
_GUIDANCE = (
  "03b は実行直前に HEAD と ledger SHA-256 を再確認し、remote user confirmation を完了してください。",
  "03b による pending clear 後、host の deploy worktree で restore_argv の Git restore を実行してください。",
  "git status と git diff を確認し、必要な cleanup を完了してください。",
  "通常の state checker check を実行してください。",
  "COMMIT_READY の場合のみ管理・rollback 変更を [skip ci] 付きで commit し、deploy branch を push してください。",
  "NOT_READY の場合は commit / push せず、復旧内容を見直してください。",
  "ローカル検証成功は COMMIT_READY ではなく、pending clear は Git restore の完了ではありません。",
)


def _parse_ledger(content: bytes) -> dict:
  def unique(pairs):
    result = {}
    for key, value in pairs:
      if key in result:
        raise ValueError("Duplicate ledger JSON key")
      result[key] = value
    return result
  ledger = json.loads(content, object_pairs_hook=unique)
  if not isinstance(ledger, dict) or not all(
      isinstance(ledger.get(key), dict) for key in ("articles", "pending_next_state")):
    raise ValueError("Invalid ledger maps")
  return ledger


def _confirmed(ledger: dict) -> None:
  for article_id, entry in ledger["articles"].items():
    _validate_article_id(article_id)
    if not isinstance(entry, dict):
      raise ValueError("Invalid confirmed entry")
    if entry.get("state") not in ("active", "erased"):
      raise ValueError("Invalid confirmed state")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
      raise ValueError("Invalid confirmed slug")
    release = entry.get("successful_release")
    if type(release) is not int or release < 1:
      raise ValueError("Invalid confirmed release")
    _validate_observation(entry.get("successful_zenn_observation"))
  _validate_slug_ownership(ledger)


def _pending(ledger: dict, article_id: str) -> dict:
  _validate_article_id(article_id)
  entry = ledger["pending_next_state"][article_id]
  operation = entry.get("operation") if isinstance(entry, dict) else None
  if not isinstance(operation, str) or operation not in _VALIDATORS:
    raise ValueError("Invalid pending operation")
  pending = _VALIDATORS[operation](ledger, article_id)
  if operation == "CREATE" and pending["release"] != 1:
    raise ValueError("CREATE introduction requires release 1")
  if operation != "UPDATE" and "baseline_observation" in pending:
    raise ValueError("Only UPDATE may store a baseline observation")
  return pending


def _article_path(slug: str) -> str:
  return f"articles/{slug}.md"


def _snapshot_path(article_id: str) -> str:
  return f".zenn-authoring/released/{article_id}.md"


def _file(root: Path, path: str) -> bytes | None:
  target = safe_path(root, *path.split("/"))
  if not target.exists():
    return None
  if not target.is_file():
    raise ValueError(f"Not a regular file: {path}")
  return target.read_bytes()


def _baseline(root, ledger, tree):
  """parent confirmed state の artifact と snapshot の不変条件を確認します。"""
  for article_id, entry in ledger["articles"].items():
    content = read_tree_blob(root, tree, _snapshot_path(article_id))
    path = _article_path(entry["slug"])
    if entry["state"] == "active":
      if read_tree_blob(root, tree, path) != content:
        raise ValueError(f"Parent confirmed artifact/snapshot mismatch: {article_id}")
    elif path in tree:
      raise ValueError(f"Parent erased artifact exists: {article_id}")
  active_paths = {_article_path(entry["slug"]) for entry in ledger["articles"].values()
                  if entry["state"] == "active"}
  snapshots = {_snapshot_path(key) for key in ledger["articles"]}
  for path in tree:
    if path.startswith("articles/") and path.endswith(".md") and path not in active_paths:
      raise ValueError(f"Parent unmanaged article: {path}")
    if path.startswith(".zenn-authoring/released/") and path not in snapshots:
      raise ValueError(f"Parent unmanaged snapshot: {path}")


def _history(root, current, head_ledger, parent_ledger, head_tree, parent_tree):
  """導入 commit と current subset、完了済み terminal changes を検証します。"""
  if parent_ledger["pending_next_state"] or not head_ledger["pending_next_state"]:
    raise ValueError("HEAD is not a pending introduction commit")
  if head_ledger["articles"] != parent_ledger["articles"]:
    raise ValueError("Confirmed state changed in pending introduction commit")
  metadata = lambda ledger: {k: v for k, v in ledger.items()
                             if k not in ("articles", "pending_next_state")}
  if metadata(current) != metadata(head_ledger) or metadata(head_ledger) != metadata(parent_ledger):
    raise ValueError("Unexplained ledger metadata changes")
  if not current["pending_next_state"].keys() <= head_ledger["pending_next_state"].keys():
    raise ValueError("Current pending is not a HEAD pending subset")
  expected_ids = set(head_ledger["articles"])
  allowed_dirty = {".zenn-authoring/ledger.json"}
  for article_id in head_ledger["pending_next_state"]:
    pending = _pending(head_ledger, article_id)
    operation = pending["operation"]
    path = _article_path(pending["slug"])
    old = parent_tree.get(path)
    new = head_tree.get(path)
    if (operation == "UPDATE" and (old is None or new is None or old == new) or
        operation == "DELETE" and (old is None or new is not None) or
        operation in ("CREATE", "RECREATE") and (old is not None or new is None)):
      raise ValueError(f"HEAD article diff contradicts {operation}: {article_id}")
    if operation in ("UPDATE", "RECREATE"):
      previous = parent_ledger["articles"][article_id]["successful_release"]
      same = (read_tree_blob(root, head_tree, path) ==
              read_tree_blob(root, parent_tree, _snapshot_path(article_id)))
      if pending["release"] != previous + (not same):
        raise ValueError(f"HEAD content contradicts pending release: {article_id}")
    if article_id in current["pending_next_state"]:
      if current["pending_next_state"][article_id] != pending:
        raise ValueError(f"Pending differs from HEAD: {article_id}")
      if current["articles"].get(article_id) != head_ledger["articles"].get(article_id):
        raise ValueError(f"Pending confirmed state differs from HEAD: {article_id}")
      continue
    before = head_ledger["articles"].get(article_id)
    after = current["articles"].get(article_id)
    artifact = _file(root, path)
    snapshot = _file(root, _snapshot_path(article_id))
    if operation in ("CREATE", "RECREATE") and after == before and artifact is None:
      # COLLISION は confirmed state と snapshot を保持します。
      expected_snapshot = (read_tree_blob(root, parent_tree, _snapshot_path(article_id))
                           if before else None)
      if snapshot != expected_snapshot:
        raise ValueError(f"Completed collision snapshot mismatch: {article_id}")
      allowed_dirty.add(path)
      continue
    expected = dict(before or {})
    if operation == "DELETE":
      expected["state"] = "erased"
      if artifact is not None or snapshot != read_tree_blob(root, parent_tree, _snapshot_path(article_id)):
        raise ValueError(f"Completed DELETE mismatch: {article_id}")
    else:
      if not isinstance(after, dict):
        raise ValueError(f"Unexplained pending removal: {article_id}")
      expected.update(slug=pending["slug"], state="active",
                      successful_release=pending["release"],
                      successful_zenn_observation=after.get("successful_zenn_observation"))
      if artifact is None or artifact != snapshot or artifact != read_tree_blob(root, head_tree, path):
        raise ValueError(f"Completed terminal artifact/snapshot mismatch: {article_id}")
      allowed_dirty.add(_snapshot_path(article_id))
    if after != expected:
      raise ValueError(f"Unexplained confirmed transition: {article_id}")
    expected_ids.add(article_id)
  if set(current["articles"]) != expected_ids:
    raise ValueError("Unexplained confirmed article additions/removals")
  for article_id, entry in head_ledger["articles"].items():
    if article_id not in head_ledger["pending_next_state"] and current["articles"].get(article_id) != entry:
      raise ValueError(f"Unrelated confirmed state changed: {article_id}")
    if article_id not in head_ledger["pending_next_state"]:
      if _file(root, _snapshot_path(article_id)) != read_tree_blob(root, parent_tree, _snapshot_path(article_id)):
        raise ValueError(f"Unrelated snapshot changed: {article_id}")
      artifact = _file(root, _article_path(entry["slug"]))
      expected = (read_tree_blob(root, parent_tree, _snapshot_path(article_id))
                  if entry["state"] == "active" else None)
      if artifact != expected:
        raise ValueError(f"Unrelated confirmed artifact changed: {article_id}")
  pending_paths = {_article_path(entry["slug"]) for entry in head_ledger["pending_next_state"].values()}
  for path in set(head_tree) | set(parent_tree):
    if path.startswith("articles/") and path not in pending_paths and head_tree.get(path) != parent_tree.get(path):
      raise ValueError(f"Unexplained article change in HEAD: {path}")
  return allowed_dirty


def _snapshots(root, current, parent_ledger, parent_tree, head_tree):
  anomalies = []
  for article_id in current["pending_next_state"]:
    try:
      _validate_article_id(article_id)
      path = _snapshot_path(article_id)
      content = _file(root, path)
      if article_id not in parent_ledger["articles"]:
        if content is not None or path in parent_tree or path in head_tree:
          raise ValueError("Unexpected CREATE snapshot; discard would leave an orphan snapshot")
      else:
        baseline = read_tree_blob(root, parent_tree, path)
        if read_tree_blob(root, head_tree, path) != baseline or content != baseline:
          raise ValueError("Released snapshot differs from parent confirmed content or is missing")
    except (OSError, ValueError, TypeError) as exc:
      anomalies.append(f"{article_id}: {exc}; article restore alone is insufficient; recovery remains unverified")
  base = safe_path(root, ".zenn-authoring", "released")
  if base.exists():
    if not base.is_dir():
      raise ValueError("Invalid released directory")
    for path in base.rglob("*"):
      safe_path(root, *path.relative_to(root).parts)
      if path.is_dir() or path.name not in {f"{key}.md" for key in current["articles"]}:
        if path.name not in {f"{key}.md" for key in current["pending_next_state"]}:
          anomalies.append(f"Unmanaged released snapshot: {path.relative_to(root)}; recovery remains unverified")
  return anomalies


def preflight_discard(article_root: Path) -> PreflightResult:
  """明示した記事 repository の全 pending を検証し、remote 確認待ち計画を返します。

  snapshot anomaly による拒否は、discard の既存 artifact 条件に追加した安全性
  要件です。snapshot は修復せず、通常の terminal retry 等による解決が必要です。
  restore_argv は deploy worktree で実行する引数列です。host path を埋め込まず、
  Windows host でも検証済み commit ID と repository-relative path を利用します。
  """
  root = head = parent = digest = None
  diagnostics, anomalies, results = [], [], []
  plan = None
  try:
    if article_root is None or not str(article_root):
      raise ValueError("Explicit article repository root is required")
    safe_path(Path(article_root).absolute())
    trees = identify_worktrees(article_root)
    root = trees.deploy
    safe_path(trees.authoring, ".git")
    safe_path(root, ".git")
    for target in (trees.authoring, root):
      inspect_history_metadata(target)
      if inspect_index_flags(target):
        raise ValueError("Unreliable index flags")
    # authoring の編集内容は復旧対象ではありません。metadata/index は検証します。
    authoring_status = inspect_status(trees.authoring)
    if any(entry.code not in (" M", " D", "??") for entry in authoring_status):
      raise ValueError("Unsafe staged/conflicting authoring changes")
    for entry in authoring_status:
      safe_path(trees.authoring, *entry.path.rstrip("/").split("/"))
    head, parent = commit_and_parent(root)
    head_tree, parent_tree = commit_tree(root, head), commit_tree(root, parent)
    ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
    before = ledger_path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    current = _parse_ledger(before)
    head_ledger = _parse_ledger(read_tree_blob(root, head_tree, ".zenn-authoring/ledger.json"))
    parent_ledger = _parse_ledger(read_tree_blob(root, parent_tree, ".zenn-authoring/ledger.json"))
    if not current["pending_next_state"]:
      raise ValueError("At least one pending article is required")
    for article_id, entry in current["pending_next_state"].items():
      errors = []
      operation = entry.get("operation") if isinstance(entry, dict) else None
      slug = entry.get("slug") if isinstance(entry, dict) else None
      path = None
      try:
        pending = _pending(current, article_id)
        path = _article_path(pending["slug"])
        content = _file(root, path)
        if (content is None) != (operation == "DELETE"):
          raise ValueError("Deploy artifact presence contradicts pending operation")
      except (OSError, ValueError, TypeError) as exc:
        errors.append(str(exc))
      results.append(PendingValidation(article_id, operation if isinstance(operation, str) else None,
                                      slug if isinstance(slug, str) else None, path, tuple(errors),
                                      (_REMOTE[operation],) if isinstance(operation, str) and operation in _REMOTE else ()))
      diagnostics.extend(f"{article_id}: {error}" for error in errors)
    for ledger in (current, head_ledger, parent_ledger):
      _confirmed(ledger)
    anomalies.extend(_snapshots(root, current, parent_ledger, parent_tree, head_tree))
    _baseline(root, parent_ledger, parent_tree)
    allowed = _history(root, current, head_ledger, parent_ledger, head_tree, parent_tree)
    # HEAD の snapshot 変更や unrelated article 変更も検証します。
    for path in set(parent_tree) | set(head_tree):
      if path.startswith(".zenn-authoring/released/") and parent_tree.get(path) != head_tree.get(path):
        raise ValueError(f"Snapshot changed in deploy commit: {path}")
    for entry in inspect_status(root):
      if entry.path not in allowed or entry.original_path or entry.code not in (" M", " D", "??"):
        diagnostics.append(f"Unexplained deploy change: {entry.code} {entry.path}")
      safe_path(root, *entry.path.split("/"))
    article_dir = safe_path(root, "articles")
    expected_paths = {_article_path(entry["slug"]) for entry in current["articles"].values()
                      if entry["state"] == "active"}
    expected_paths.update(item.restore_path for item in results if item.operation != "DELETE")
    for artifact in article_dir.rglob("*"):
      relative = artifact.relative_to(root).as_posix()
      safe_path(root, *artifact.relative_to(root).parts)
      if artifact.suffix == ".md" and relative not in expected_paths:
        diagnostics.append(f"Unmanaged article artifact: {relative}")
    # pending content と HEAD blob の一致は local artifact の必須条件ではありません。
    # Git が報告する直接編集は上記の diagnostic により安全性未確定とします。
    if ledger_path.read_bytes() != before or commit_and_parent(root) != (head, parent):
      diagnostics.append("HEAD or ledger changed during validation")
    if not diagnostics and not anomalies:
      paths = tuple(item.restore_path for item in results)
      requirements = _TERMINAL + tuple(f"{item.article_id}: {text}" for item in results
                                       for text in item.remote_requirements)
      plan = RecoveryPlan(root, head, parent, digest, paths,
                          ("git", "restore", f"--source={parent}", "--worktree", "--", *paths),
                          requirements, _GUIDANCE)
  except (OSError, ValueError, TypeError, RuntimeError) as exc:
    diagnostics.append(str(exc))
  return PreflightResult(
    LocalValidation.LOCAL_PREREQUISITES_VERIFIED if plan else LocalValidation.LOCAL_VALIDATION_REJECTED,
    root, head, parent, digest, tuple(results), tuple(diagnostics), tuple(anomalies), plan,
    remote_requirements=_TERMINAL + tuple(f"{item.article_id}: {text}" for item in results
                                         for text in item.remote_requirements))
