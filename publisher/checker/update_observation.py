"""pending UPDATE の remote result を書き込みなしで判定します。"""

from checker.create_observation import (
  ArticleResponse, CreateObservation, CreateResult, classify_create_response,
)
from checker.terminal_create import _validate_observation


def classify_update_response(response: ArticleResponse, pending: dict,
                             own_user_id: int) -> CreateObservation:
  """検証済み pending の固定 baseline と現在の observation を比較します。

  pending の ledger 整合性は read_update_pending() で事前検証します。
  CREATE の response model と owner/schema 検証を再利用します。
  """
  try:
    baseline = _validate_observation(pending.get("baseline_observation"))
  except (ValueError, TypeError, AttributeError) as exc:
    return CreateObservation(CreateResult.ERROR, detail=str(exc))
  if not isinstance(response, ArticleResponse):
    return CreateObservation(CreateResult.ERROR, detail="Invalid transport response")
  if response.status == 200:
    try:
      article = response.payload.get("article")
      _validate_observation(article)
    except (ValueError, TypeError, AttributeError) as exc:
      return CreateObservation(CreateResult.ERROR, detail=str(exc))
  result = classify_create_response(response, pending["slug"], own_user_id)
  if result.result == CreateResult.COLLISION or (
      result.result == CreateResult.PENDING and response.status == 404):
    return CreateObservation(CreateResult.UNKNOWN,
                             detail="Expected own-user UPDATE publication is absent")
  if result.result != CreateResult.SUCCESS:
    return result
  if result.observation == baseline:
    return CreateObservation(CreateResult.PENDING)
  return result
