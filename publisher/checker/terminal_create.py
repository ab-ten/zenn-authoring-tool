"""P0-CHECK-01: local terminal application for a confirmed CREATE SUCCESS."""

import json
from pathlib import Path
import re
from uuid import UUID

from checker.atomic_write import atomic_replace_bytes


_SLUG = re.compile(r"[a-z0-9_-]{12,50}\Z")
_OBSERVATION_KEYS = ("body_updated_at", "source_repo_updated_at", "published_at")


class LocalApplyError(ValueError):
  """The local state is not eligible for a CREATE SUCCESS application."""


def _validate_article_id(article_id: str) -> None:
  try:
    valid = str(UUID(article_id)) == article_id
  except (ValueError, AttributeError, TypeError):
    valid = False
  if not valid:
    raise LocalApplyError("article_id must be a canonical lowercase UUID")


def _validate_observation(observation: dict) -> dict:
  if not isinstance(observation, dict):
    raise LocalApplyError("A normalized Zenn observation is required")
  if not all(isinstance(observation.get(key), str) and observation[key]
             for key in _OBSERVATION_KEYS):
    raise LocalApplyError("Zenn observation is missing required timestamps")
  return {key: observation[key] for key in _OBSERVATION_KEYS}


def _read_create_pending(ledger: dict, article_id: str) -> dict:
  if not isinstance(ledger, dict):
    raise LocalApplyError("Invalid ledger root")
  articles = ledger.get("articles")
  pendings = ledger.get("pending_next_state")
  if not isinstance(articles, dict) or not isinstance(pendings, dict):
    raise LocalApplyError("Invalid ledger maps")
  if article_id in articles:
    raise LocalApplyError("CREATE already has a confirmed article")
  pending = pendings.get(article_id)
  if not isinstance(pending, dict) or pending.get("operation") != "CREATE":
    raise LocalApplyError("No pending CREATE for the requested article")
  if pending.get("state") != "active":
    raise LocalApplyError("Pending CREATE target must be active")
  slug = pending.get("slug")
  release = pending.get("release")
  if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
    raise LocalApplyError("Invalid pending slug")
  if isinstance(release, bool) or not isinstance(release, int) or release < 1:
    raise LocalApplyError("Invalid pending release number")
  return pending


def apply_create_success(deploy_root: Path, article_id: str,
                         observation: dict) -> None:
  """Apply an already-confirmed CREATE SUCCESS to local deploy state.

  Preconditions: the caller has completed all remote observations and
  classified this article as SUCCESS for its own Zenn publication. This
  function neither accesses Zenn nor checks global COMMIT_READY status.

  After validating the pending entry and deploy artifact, it atomically
  replaces the released snapshot, then atomically replaces the ledger.
  Removing the pending entry in the ledger replacement is the commit point.
  If interrupted before that point, retry with a freshly confirmed SUCCESS.
  """
  _validate_article_id(article_id)
  checked_observation = _validate_observation(observation)
  root = Path(deploy_root)
  ledger_path = root / ".zenn-authoring" / "ledger.json"
  snapshot_path = root / ".zenn-authoring" / "released" / f"{article_id}.md"

  with ledger_path.open("r", encoding="utf-8") as stream:
    ledger = json.load(stream)
  pending = _read_create_pending(ledger, article_id)

  artifact_path = root / "articles" / f"{pending['slug']}.md"
  if not artifact_path.is_file() or artifact_path.is_symlink():
    raise LocalApplyError(f"Missing regular deploy artifact: {artifact_path}")
  artifact_bytes = artifact_path.read_bytes()

  confirmed = {
    "slug": pending["slug"],
    "state": "active",
    "successful_release": pending["release"],
    "successful_zenn_observation": checked_observation,
  }
  next_ledger = dict(ledger)
  next_ledger["articles"] = dict(ledger["articles"])
  next_ledger["pending_next_state"] = dict(ledger["pending_next_state"])
  next_ledger["articles"][article_id] = confirmed
  del next_ledger["pending_next_state"][article_id]
  ledger_bytes = (json.dumps(next_ledger, ensure_ascii=False,
                             sort_keys=True, indent=2) + "\n").encode("utf-8")

  atomic_replace_bytes(snapshot_path, artifact_bytes)
  atomic_replace_bytes(ledger_path, ledger_bytes)
