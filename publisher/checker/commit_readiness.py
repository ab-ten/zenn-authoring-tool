"""confirmed state と deploy artifacts に基づく commit readiness を判定します。"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from checker.local_state import read_ledger, safe_path
from checker.terminal_create import (
  LocalApplyError, _SLUG, _validate_article_id, _validate_observation,
)


class CommitReadiness(StrEnum):
  """管理変更を review 後に commit できるかを示します。"""

  COMMIT_READY = "COMMIT_READY"
  NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class ReadinessResult:
  """判定と、不整合または未解決状態の診断を保持します。"""

  result: CommitReadiness
  diagnostics: tuple[str, ...] = ()


def check_commit_readiness(deploy_root: Path) -> ReadinessResult:
  """pending が空の場合に全 confirmed article と artifact を検証します。"""
  root = Path(deploy_root)
  diagnostics = []
  try:
    ledger = read_ledger(root)
    if ledger["pending_next_state"]:
      return ReadinessResult(CommitReadiness.NOT_READY, (
        f"Unresolved pending articles: {len(ledger['pending_next_state'])}",))
    articles_dir = safe_path(root, "articles")
    if not articles_dir.is_dir():
      raise LocalApplyError("Missing articles directory")
    active_names = set()
    slugs = set()
    for article_id, entry in ledger["articles"].items():
      try:
        _validate_article_id(article_id)
        if not isinstance(entry, dict):
          raise LocalApplyError("Invalid confirmed article")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
          raise LocalApplyError("Invalid confirmed slug")
        if slug in slugs:
          raise LocalApplyError("Duplicate confirmed slug")
        slugs.add(slug)
        if entry.get("state") not in ("active", "erased"):
          raise LocalApplyError("Invalid confirmed state")
        release = entry.get("successful_release")
        if type(release) is not int or release < 1:
          raise LocalApplyError("Invalid successful release")
        _validate_observation(entry.get("successful_zenn_observation"))
        snapshot = safe_path(root, ".zenn-authoring", "released", f"{article_id}.md")
        if not snapshot.is_file():
          raise LocalApplyError("Missing regular released snapshot")
        artifact = safe_path(root, "articles", f"{slug}.md")
        if entry["state"] == "active":
          active_names.add(artifact.name)
          if not artifact.is_file():
            raise LocalApplyError("Missing regular active artifact")
          if artifact.read_bytes() != snapshot.read_bytes():
            raise LocalApplyError("Artifact differs from released snapshot")
        elif artifact.exists():
          raise LocalApplyError("Erased article artifact still exists")
      except (OSError, ValueError, TypeError) as exc:
        diagnostics.append(f"{article_id}: {exc}")
    for artifact in articles_dir.rglob("*"):
      if artifact.suffix == ".md" and (
          artifact.parent != articles_dir or artifact.name not in active_names):
        diagnostics.append(f"Ledger-unmanaged article artifact: {artifact.relative_to(root)}")
      try:
        safe_path(root, *artifact.relative_to(root).parts)
      except (OSError, ValueError) as exc:
        diagnostics.append(str(exc))
  except (OSError, ValueError, TypeError) as exc:
    diagnostics.append(str(exc))
  return ReadinessResult(
    CommitReadiness.NOT_READY if diagnostics else CommitReadiness.COMMIT_READY,
    tuple(diagnostics))
