"""初期管理状態を分類し、既存ファイルを保持して初期化します。"""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable

from checker.atomic_write import atomic_replace_bytes
from checker.check_pending import _validate_slug_ownership
from checker.create_observation import load_configured_user_id
from checker.local_state import ledger_bytes, read_ledger, safe_path
from checker.terminal_create import _SLUG, _read_create_pending, _validate_article_id, _validate_observation
from checker.terminal_delete import read_delete_pending
from checker.terminal_recreate import read_recreate_pending
from checker.terminal_update import read_update_pending
from checker.zenn_user_http import ZennUser, fetch_user, validate_username
from converter.git_inspection import identify_worktrees, inspect_index_flags, inspect_status, git


_EMPTY_LEDGER = ledger_bytes({"articles": {}, "pending_next_state": {}})
_LEDGER_PATH = ".zenn-authoring/ledger.json"


@dataclass(frozen=True)
class InitResult:
  """初期化先、解決済み owner、今回の管理ファイル作成有無を保持します。"""

  deploy_root: Path
  user: ZennUser
  changed: bool


def _validate_ledger(ledger: dict) -> None:
  """既存 validator を用いて管理状態の schema を検証します。artifact は比較しません。"""
  for article_id, entry in ledger["articles"].items():
    _validate_article_id(article_id)
    if not isinstance(entry, dict) or entry.get("state") not in ("active", "erased"):
      raise ValueError("Invalid confirmed article state")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
      raise ValueError("Invalid confirmed slug")
    release = entry.get("successful_release")
    if type(release) is not int or release < 1:
      raise ValueError("Invalid successful release")
    _validate_observation(entry.get("successful_zenn_observation"))
  _validate_slug_ownership(ledger)
  validators = {"CREATE": _read_create_pending, "UPDATE": read_update_pending,
                "DELETE": read_delete_pending, "RECREATE": read_recreate_pending}
  for article_id, entry in ledger["pending_next_state"].items():
    operation = entry.get("operation") if isinstance(entry, dict) else None
    if not isinstance(operation, str) or operation not in validators:
      raise ValueError("Invalid pending operation")
    validators[operation](ledger, article_id)


def initialize(article_root: Path, username: str,
               lookup_user: Callable[[str], ZennUser] = fetch_user) -> InitResult:
  """明示された記事 repository に owner 設定と canonical empty ledger を確立します。

  Authoring HEAD、linked deploy worktree、相対リンク、準備済み管理 directory を要求します。
  Deploy は正当な unborn を許容しますが、cleanliness と index flags を検証します。
  中断由来の canonical empty ledger のみの変更は再開できます。GET lookup 後に ledger、
  config の順で atomic replacement を行い、config 作成を完了点とします。
  同一 ID の初期化済み repository では既存ファイルを変更しません。
  InitResult を返し、不正な状態や lookup・書き込み失敗では例外を送出します。
  途中失敗時も rollback、Git metadata の変更、commit/push は行いません。
  """
  validate_username(username)
  trees = identify_worktrees(article_root, allow_unborn_deploy=True)
  root = trees.deploy
  management = safe_path(root, ".zenn-authoring")
  if not management.is_dir():
    raise ValueError("Missing management directory; prepare _deploy/.zenn-authoring on the host")
  config_path = safe_path(root, ".zenn-authoring", "config.json")
  ledger_path = safe_path(root, ".zenn-authoring", "ledger.json")
  for path in (config_path, ledger_path):
    if path.exists() and not path.is_file():
      raise ValueError("Management files must be regular files")
  if inspect_index_flags(root):
    raise ValueError("Unreliable deploy index flags")
  entries = inspect_status(root)
  config_exists, ledger_exists = config_path.exists(), ledger_path.exists()
  resume = not config_exists and ledger_exists and ledger_path.read_bytes() == _EMPTY_LEDGER
  if entries:
    allowed = (resume and len(entries) == 1 and entries[0].path == _LEDGER_PATH and
               entries[0].original_path is None and entries[0].code in ("??", "A "))
    if not allowed:
      raise ValueError("Dirty deploy worktree; only an init-produced canonical empty ledger may remain")
    if entries[0].code == "A " and git(root, "show", ":" + _LEDGER_PATH) != _EMPTY_LEDGER:
      raise ValueError("Staged ledger is not canonical empty state")
  if config_exists and not ledger_exists:
    raise ValueError("Config exists but ledger is missing; restore ledger from Git on the host")
  if ledger_exists:
    try:
      ledger = read_ledger(root)
      _validate_ledger(ledger)
    except (OSError, ValueError, TypeError) as exc:
      raise ValueError("Invalid ledger; inspect or restore it from Git on the host") from exc
    if not config_exists and not resume:
      raise ValueError("Ledger is not canonical empty state; inspect repository state on the host")
  owner_id = None
  if config_exists:
    try:
      owner_id = load_configured_user_id(root)
      config = json.loads(config_path.read_bytes())
      validate_username(config["zenn_user"].get("username"))
    except (OSError, ValueError, TypeError) as exc:
      raise ValueError("Invalid config; inspect or restore it from Git on the host") from exc
  user = lookup_user(username)
  if not isinstance(user, ZennUser):
    raise ValueError("Invalid user lookup result")
  # Adapter を置換する場合にも、永続化する値を検証します。
  user = ZennUser(user.id, user.username)
  if owner_id is not None:
    if owner_id != user.id:
      raise ValueError("Different Zenn user ID; automatic account replacement is not supported")
    return InitResult(root, user, changed=False)
  if not ledger_exists:
    atomic_replace_bytes(ledger_path, _EMPTY_LEDGER)
  config = {"zenn_user": {"id": user.id, "username": user.username}}
  atomic_replace_bytes(config_path, ledger_bytes(config))
  return InitResult(root, user, changed=True)
