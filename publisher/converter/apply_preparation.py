"""固定済み PreflightOutcome の適用対象と pending bytes を読み取り専用で準備します。"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from checker import create_preflight, update_preflight, delete_preflight, recreate_preflight
from checker.create_preflight import ArticlePreflight, PreflightResult, PreflightStatus
from checker.delete_preflight import DeleteArticlePreflight
from checker.errors import LocalApplyError
from checker.local_state import ledger_bytes, read_ledger, safe_path
from checker.terminal_create import _validate_observation
from checker.update_preflight import UpdateArticlePreflight
from converter.models import ArticlePlanEntry, ImagePlanEntry, PublishPlan
from converter.preflight_integration import PreflightOutcome


@dataclass(frozen=True)
class ArticleTarget:
  """元の記事 entry と slug に対応する検証済み artifact path です。"""

  entry: ArticlePlanEntry
  path: Path


@dataclass(frozen=True)
class ImageTarget:
  """元の画像 entry と、型変更を考慮した適用先です。"""

  entry: ImagePlanEntry
  path: Path


@dataclass(frozen=True)
class ApplyPreparation:
  """05b が直接使用する不変の準備結果です。永続状態ではありません。

  画像は image_deletes、image_directories_to_remove、image_writes の順で
  適用してください。ディレクトリは空の場合のみ除去し、再帰削除しません。
  pending_ledger_bytes は全 artifact 操作の成功後にのみ永続化できます。
  単一ユーザーの逐次実行を前提とし、並行変更へのロックは提供しません。
  """

  outcome: PreflightOutcome
  article_targets: tuple[ArticleTarget, ...]
  image_deletes: tuple[ImageTarget, ...]
  image_directories_to_remove: tuple[Path, ...]
  image_writes: tuple[ImageTarget, ...]
  pending_ledger_bytes: bytes | None


def _authorize(outcome: PreflightOutcome) -> None:
  if type(outcome) is not PreflightOutcome or type(outcome.plan) is not PublishPlan:
    raise LocalApplyError("A final immutable PublishPlan outcome is required")
  plan, result = outcome.plan, outcome.preflight
  if (type(result) is not PreflightResult or result.result is not PreflightStatus.OK or
      not isinstance(result.articles, tuple) or
      not isinstance(result.diagnostics, tuple) or
      not all(isinstance(item, str) for item in result.diagnostics)):
    raise LocalApplyError("Overall valid PreflightStatus.OK is required")
  plan.__post_init__()
  if len(plan.articles) != len(result.articles):
    raise LocalApplyError("Preflight article results must match the plan one-to-one")
  ids, slugs, paths = set(), set(), set()
  for entry, item in zip(plan.articles, result.articles):
    if type(entry) is not ArticlePlanEntry:
      raise LocalApplyError("Immutable article entry is required")
    if entry.operation not in ("CREATE", "UPDATE", "DELETE", "RECREATE"):
      raise LocalApplyError("Unsupported article operation")
    if ((entry.operation == "DELETE" and entry.candidate_content is not None) or
        (entry.operation != "DELETE" and not isinstance(entry.candidate_content, str))):
      raise LocalApplyError("Invalid candidate content contract")
    if entry.operation != "UPDATE" and entry.observation_override is not None:
      raise LocalApplyError("Only UPDATE accepts observation override")
    expected = {"CREATE": ArticlePreflight, "UPDATE": UpdateArticlePreflight,
                "DELETE": DeleteArticlePreflight, "RECREATE": ArticlePreflight}[entry.operation]
    if (type(item) is not expected or item.result is not PreflightStatus.OK or
        item.article_id != entry.article_id or item.slug != entry.slug or
        not isinstance(item.detail, str)):
      raise LocalApplyError("Invalid or mismatched article preflight result")
    if entry.article_id in ids or entry.slug in slugs:
      raise LocalApplyError("Duplicate article ID or slug")
    ids.add(entry.article_id)
    slugs.add(entry.slug)
  for entry in plan.images:
    if type(entry) is not ImagePlanEntry:
      raise LocalApplyError("Immutable image entry is required")
    entry.__post_init__()
    if entry.path in paths:
      raise LocalApplyError("Duplicate image operation")
    paths.add(entry.path)
  writes = [entry.path for entry in plan.images if entry.operation != "DELETE"]
  if any(other.startswith(path + "/") for path in writes for other in writes):
    raise LocalApplyError("Conflicting image write targets")


def _article_targets(outcome: PreflightOutcome, ledger: dict):
  validators = {"CREATE": create_preflight._validate_entry,
                "UPDATE": update_preflight._validate_entry,
                "DELETE": delete_preflight._validate_entry,
                "RECREATE": recreate_preflight._validate_entry}
  targets, pending = [], {}
  for entry, result in zip(outcome.plan.articles, outcome.preflight.articles):
    validators[entry.operation](entry, ledger)
    if (entry.state != ("erased" if entry.operation == "DELETE" else "active") or
        type(entry.release) is not int or entry.release < 1):
      raise LocalApplyError("Invalid article target state or release")
    path = safe_path(outcome.deploy_root, "articles", f"{entry.slug}.md")
    if entry.operation in ("CREATE", "RECREATE"):
      if path.exists():
        raise LocalApplyError(f"Refusing existing unrelated article artifact: {path}")
    elif not path.is_file():
      raise LocalApplyError(f"Missing regular confirmed article artifact: {path}")
    value = {"operation": entry.operation, "slug": entry.slug,
             "state": entry.state, "release": entry.release}
    if entry.operation == "UPDATE":
      baseline = result.baseline_observation
      if not isinstance(baseline, Mapping):
        raise LocalApplyError("UPDATE requires baseline observation")
      value["baseline_observation"] = _validate_observation(dict(baseline))
    pending[entry.article_id] = value
    targets.append(ArticleTarget(entry, path))
  return tuple(targets), pending


def _image_targets(outcome: PreflightOutcome):
  root = outcome.deploy_root
  base = safe_path(root, "images")
  if base.exists() and not base.is_dir():
    raise LocalApplyError("Deploy images root must be a directory")
  deletes, writes, directories = [], [], set()
  delete_paths = {root.joinpath(*entry.path.split("/")) for entry in outcome.plan.images
                  if entry.operation == "DELETE"}

  def removable(directory: Path):
    # 全既存ファイルが固定済み DELETE に含まれる場合のみ型変更を許可します。
    directories.add(directory)
    for child in directory.iterdir():
      checked = safe_path(root, *child.relative_to(root).parts)
      if checked.is_dir():
        removable(checked)
      elif checked not in delete_paths:
        raise LocalApplyError(f"Nonempty image directory would remain: {directory}")

  for entry in outcome.plan.images:
    parts = entry.path.split("/")
    path = root.joinpath(*parts)
    blocked = False
    for length in range(1, len(parts)):
      parent = safe_path(root, *parts[:length])
      if parent.is_file():
        if parent not in delete_paths or entry.operation != "ADD":
          raise LocalApplyError(f"Image parent is not a directory: {parent}")
        blocked = True
        break
    if not blocked:
      path = safe_path(root, *parts)
    if entry.operation == "DELETE":
      if not path.is_file():
        raise LocalApplyError(f"DELETE requires an existing regular image: {path}")
      deletes.append(ImageTarget(entry, path))
    elif entry.operation == "UPDATE":
      if not path.is_file():
        raise LocalApplyError(f"UPDATE requires an existing regular image: {path}")
      writes.append(ImageTarget(entry, path))
    else:
      if not blocked and path.exists():
        if not path.is_dir():
          raise LocalApplyError(f"ADD would overwrite an existing image: {path}")
        removable(path)
      writes.append(ImageTarget(entry, path))
  removal_order = tuple(sorted(directories, key=lambda path: (-len(path.parts), str(path))))
  return tuple(deletes), removal_order, tuple(writes)


def prepare_apply(outcome: PreflightOutcome) -> ApplyPreparation:
  """認可、ledger、操作と対象を検証し、書き込みなしで全 pending を構築します。

  source の再読込、再分類、画像差分の再計算、remote 観測を行いません。
  元の outcome、plan、preflight と各 entry を保持します。
  """
  try:
    _authorize(outcome)
    if not isinstance(outcome.deploy_root, Path) or not outcome.deploy_root.is_absolute():
      raise LocalApplyError("Verified absolute deploy root is required")
    safe_path(outcome.deploy_root)
    ledger = read_ledger(outcome.deploy_root)
    if ledger["pending_next_state"]:
      raise LocalApplyError("Unresolved pending_next_state")
    articles, pending = _article_targets(outcome, ledger)
    deletes, directories, writes = _image_targets(outcome)
    content = None
    if articles:
      next_ledger = dict(ledger)
      next_ledger["pending_next_state"] = pending
      content = ledger_bytes(next_ledger)
      if not isinstance(content, bytes):
        raise LocalApplyError("Invalid pending ledger serialization")
    return ApplyPreparation(outcome, articles, deletes, directories, writes, content)
  except LocalApplyError:
    raise
  except (OSError, ValueError, TypeError, AttributeError) as exc:
    raise LocalApplyError(f"Apply preparation failed: {exc}") from exc
