"""P0-CHECK-02: observe and apply exactly one pending CREATE transition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
from typing import Callable

from checker.terminal_create import (
  LocalApplyError,
  _read_create_pending,
  _validate_article_id,
  apply_create_success,
)


class CreateResult(StrEnum):
  """One remote classification or a local failure preventing its application."""

  SUCCESS = "SUCCESS"
  PENDING = "PENDING"
  COLLISION = "COLLISION"
  UNKNOWN = "UNKNOWN"
  ERROR = "ERROR"


@dataclass(frozen=True)
class ArticleResponse:
  """One HTTP observation, with JSON-decoded payload for HTTP 200."""

  status: int
  payload: object = None


@dataclass(frozen=True)
class CreateObservation:
  """Classification of one candidate slug; observation exists only on SUCCESS."""

  result: CreateResult
  observation: dict[str, str] | None = None
  detail: str = ""


def _valid_user_id(user_id: object) -> bool:
  return type(user_id) is int and user_id > 0


def load_configured_user_id(deploy_root: Path) -> int:
  """Read numeric owner identity from configured repository state, without writing."""
  config_path = Path(deploy_root) / ".zenn-authoring" / "config.json"
  with config_path.open("r", encoding="utf-8") as stream:
    config = json.load(stream)
  if not isinstance(config, dict) or not isinstance(config.get("zenn_user"), dict):
    raise ValueError("Missing zenn_user config")
  user_id = config["zenn_user"].get("id")
  if not _valid_user_id(user_id):
    raise ValueError("Missing or invalid numeric zenn_user.id; run state checker init")
  return user_id


def classify_create_response(response: ArticleResponse, expected_slug: str,
                             own_user_id: int) -> CreateObservation:
  """Classify one CREATE result without applying changes to local state.

  404 remains PENDING, 200 with a valid own-user article is SUCCESS, and
  200 with a different numeric owner is COLLISION. Invalid owner/schema is
  ERROR. Unexpected but well-formed HTTP statuses are UNKNOWN; server or
  access errors are ERROR. Never infer ownership from username.
  """
  if not _valid_user_id(own_user_id):
    return CreateObservation(CreateResult.ERROR, detail="Invalid configured owner ID")
  if type(response.status) is not int:
    return CreateObservation(CreateResult.ERROR, detail="Invalid HTTP status")
  if response.status == 404:
    return CreateObservation(CreateResult.PENDING)
  if response.status in (401, 403, 408, 429) or response.status >= 500:
    return CreateObservation(CreateResult.ERROR,
                             detail=f"Remote API returned HTTP {response.status}")
  if response.status != 200:
    return CreateObservation(CreateResult.UNKNOWN,
                             detail=f"Unexpected HTTP status {response.status}")

  payload = response.payload
  article = payload.get("article") if isinstance(payload, dict) else None
  if not isinstance(article, dict):
    return CreateObservation(CreateResult.ERROR, detail="Missing article object")
  if article.get("slug") != expected_slug:
    return CreateObservation(CreateResult.ERROR, detail="Unexpected article slug")
  user = article.get("user")
  user_id = user.get("id") if isinstance(user, dict) else None
  if not _valid_user_id(user_id):
    return CreateObservation(CreateResult.ERROR, detail="Missing or invalid article.user.id")
  if user_id != own_user_id:
    return CreateObservation(CreateResult.COLLISION)

  keys = ("body_updated_at", "source_repo_updated_at", "published_at")
  if not all(isinstance(article.get(key), str) and article[key] for key in keys):
    return CreateObservation(CreateResult.ERROR, detail="Missing observation metadata")
  observation = {key: article[key] for key in keys}
  return CreateObservation(CreateResult.SUCCESS, observation=observation)


def check_single_create(deploy_root: Path, article_id: str,
                        fetch_article: Callable[[str], ArticleResponse]) -> CreateObservation:
  """Observe and locally apply one pending CREATE for a P0 vertical-slice test.

  A full checker must observe ALL pending articles before applying ANY result.
  Therefore this helper rejects ledgers containing more than one pending
  article. It does not apply COLLISION cleanup, calculate COMMIT_READY, or
  initiate deployments. The supplied fetch function makes fixtures and a
  future HTTP adapter interchangeable.

  On confirmed SUCCESS, the existing idempotent terminal apply is invoked.
  If observation or local apply fails, report ERROR and preserve pending;
  snapshot may already have been replaced in an interrupted local apply.
  """
  root = Path(deploy_root)
  try:
    _validate_article_id(article_id)
    owner_id = load_configured_user_id(root)
    with (root / ".zenn-authoring" / "ledger.json").open(
        "r", encoding="utf-8") as stream:
      ledger = json.load(stream)
    if not isinstance(ledger, dict) or not isinstance(ledger.get("pending_next_state"), dict):
      raise LocalApplyError("Invalid ledger")
    if len(ledger["pending_next_state"]) != 1:
      raise LocalApplyError("P0 single-CREATE helper requires exactly one pending article")
    pending = _read_create_pending(ledger, article_id)
  except (OSError, ValueError, TypeError) as exc:
    return CreateObservation(CreateResult.ERROR, detail=str(exc))

  try:
    response = fetch_article(pending["slug"])
  except (OSError, ValueError) as exc:
    return CreateObservation(CreateResult.ERROR, detail=f"Remote observation failed: {exc}")

  if not isinstance(response, ArticleResponse):
    return CreateObservation(CreateResult.ERROR, detail="Invalid transport response")
  result = classify_create_response(response, pending["slug"], owner_id)
  if result.result != CreateResult.SUCCESS:
    return result

  try:
    apply_create_success(root, article_id, result.observation)
  except (OSError, ValueError, TypeError) as exc:
    return CreateObservation(CreateResult.ERROR,
                             detail=f"CREATE local apply failed: {exc}")
  return result
