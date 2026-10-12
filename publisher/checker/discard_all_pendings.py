"""検証済みの全 pending を破棄し、host 側の復旧計画を返します。"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
from typing import Callable

from checker.atomic_write import atomic_replace_bytes
from checker.create_observation import (
  ArticleResponse, CreateResult, classify_create_response, load_configured_user_id,
)
from checker.delete_observation import classify_delete_response
from checker.discard_preflight import RecoveryPlan, preflight_discard
from checker.errors import LocalApplyError
from checker.local_state import ledger_bytes, safe_path
from checker.recreate_observation import classify_recreate_response
from checker.update_observation import classify_update_response
from checker.zenn_http import fetch_article as http_fetch_article
from converter.git_inspection import commit_and_parent


@dataclass(frozen=True)
class DiscardResult:
  """clear 完了時の検証済み復旧計画と remote 診断を保持します。"""

  recovery_plan: RecoveryPlan
  diagnostics: tuple[str, ...]


def _verified_preflight(article_root):
  result = preflight_discard(article_root)
  if result.recovery_plan is None or result.snapshot_anomalies or result.diagnostics:
    raise LocalApplyError("Discard local preflight rejected: " + "; ".join(
      (*result.diagnostics, *result.snapshot_anomalies)))
  return result


def _classify(response, pending, owner):
  if not isinstance(response, ArticleResponse):
    raise LocalApplyError("Invalid transport response")
  operation = pending["operation"]
  if operation == "CREATE":
    result = classify_create_response(response, pending["slug"], owner)
  elif operation == "UPDATE":
    result = classify_update_response(response, pending, owner)
  elif operation == "DELETE":
    result = classify_delete_response(response, pending, owner)
  else:
    result = classify_recreate_response(response, pending, owner)
  allowed = (CreateResult.PENDING, CreateResult.COLLISION) if operation in (
    "CREATE", "RECREATE") else (CreateResult.PENDING,)
  return result, result.result in allowed


def discard_all_pendings(
    article_root: Path, *, confirmed_deploy_terminal: bool,
    confirmed_remote_safe: bool, confirmed_abandon_all: bool,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> DiscardResult:
  """明示された記事 root の全 pending を1回の atomic replacement で削除します。

  3つの confirmation は True が必要です。ユーザーは対象 deploy が後から適用されない
  terminal 状態、operation 別 remote 整合性、全 pending の破棄を確認してください。
  fetch_article は slug ごとに1回呼び出す GET adapter です。Dashboard は検証しません。
  成功時は frozen DiscardResult に host 復旧用 RecoveryPlan と全件の診断を返します。
  confirmed state とその他 metadata は保持し、ledger 以外は書き込みません。
  Git restore、commit、push は実行しません。終了は commit readiness を意味しません。

  confirmation 不足、preflight 拒否、remote 不確実性、計画・owner・local state の変化、
  置換の失敗・no-op・事後検証不一致は例外を送出します。中断も呼び出し元へ伝播します。
  置換後の例外では pending が既に空の場合があります。ledger と Git status を確認し、
  空の場合は discard を再実行せず、Git 履歴と記事状態から手動復旧してください。
  並列 writer は非対応です。直前検証と atomic replacement は inter-process CAS では
  なく、検証と置換の間の競合を完全には防止しません。
  """
  if any(value is not True for value in (
      confirmed_deploy_terminal, confirmed_remote_safe, confirmed_abandon_all)):
    raise LocalApplyError(
      "対象 deploy の terminal 状態、operation 別 remote 整合性、全 pending 破棄の"
      "3つの confirmation が必要です。Article API は terminal 状態の証明ではありません。")
  first = _verified_preflight(article_root)
  plan = first.recovery_plan
  root = plan.deploy_root
  path = safe_path(root, ".zenn-authoring", "ledger.json")
  before = path.read_bytes()
  if hashlib.sha256(before).hexdigest() != plan.ledger_sha256:
    raise LocalApplyError("Ledger changed after preflight")
  ledger = json.loads(before)
  config_path = safe_path(root, ".zenn-authoring", "config.json")
  config_before = config_path.read_bytes()
  owner = load_configured_user_id(root)
  diagnostics, rejected = [], False
  for article_id, pending in ledger["pending_next_state"].items():
    try:
      result, allowed = _classify(fetch_article(pending["slug"]), pending, owner)
      diagnostics.append(
        f"{article_id}: {pending['operation']} {result.result}: {result.detail}")
      rejected |= not allowed
    except Exception as exc:
      diagnostics.append(f"{article_id}: remote verification failed ({type(exc).__name__})")
      rejected = True
  if rejected:
    raise LocalApplyError("Remote discard safety rejected: " + "; ".join(diagnostics))

  second = _verified_preflight(article_root)
  if second.recovery_plan != plan or second.pending != first.pending:
    raise LocalApplyError("RecoveryPlan or pending identity changed during observation")
  after_ledger = dict(ledger)
  after_ledger["pending_next_state"] = {}
  replacement = ledger_bytes(after_ledger)
  mode = stat.S_IMODE(safe_path(root, ".zenn-authoring", "ledger.json").stat().st_mode)
  if (load_configured_user_id(root) != owner or
      safe_path(root, ".zenn-authoring", "config.json").read_bytes() != config_before):
    raise LocalApplyError("Config owner identity or config bytes changed")
  if commit_and_parent(root) != (plan.expected_head, plan.source_commit):
    raise LocalApplyError("HEAD or parent changed before replacement")
  path = safe_path(root, ".zenn-authoring", "ledger.json")
  if path.read_bytes() != before:
    raise LocalApplyError("Ledger changed before replacement")
  if not atomic_replace_bytes(path, replacement, mode=mode):
    raise LocalApplyError("Unexpected ledger replacement no-op; inspect recovery state")
  actual = safe_path(root, ".zenn-authoring", "ledger.json").read_bytes()
  if actual != replacement or json.loads(actual) != after_ledger:
    raise LocalApplyError("Post-replacement ledger verification failed; inspect recovery state")
  if (commit_and_parent(root) != (plan.expected_head, plan.source_commit) or
      load_configured_user_id(root) != owner or
      safe_path(root, ".zenn-authoring", "config.json").read_bytes() != config_before):
    raise LocalApplyError("Local state changed after replacement; inspect recovery state")
  return DiscardResult(plan, tuple(diagnostics))
