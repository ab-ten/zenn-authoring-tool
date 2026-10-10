"""pending RECREATE の candidate slug を書き込みなしで観測します。"""

from typing import Callable

from checker.create_observation import (
  ArticleResponse, CreateObservation, CreateResult, classify_create_response,
)
from checker.terminal_create import _validate_observation


def classify_recreate_response(response: ArticleResponse, pending: dict,
                               own_user_id: int) -> CreateObservation:
  """CREATE 判定を再利用し、SUCCESS の current observation を返します。

  pending のローカル整合性は read_recreate_pending() で事前検証します。
  旧 confirmed slug の remote preflight は実行しません。
  """
  if not isinstance(response, ArticleResponse):
    return CreateObservation(CreateResult.ERROR, detail="Invalid transport response")
  if response.status == 200:
    try:
      article = response.payload.get("article")
      _validate_observation(article)
    except (ValueError, TypeError, AttributeError) as exc:
      return CreateObservation(CreateResult.ERROR, detail=str(exc))
  return classify_create_response(response, pending["slug"], own_user_id)


def observe_recreate_result(pending: dict, own_user_id: int,
                            fetch_article: Callable[[str], ArticleResponse]) -> CreateObservation:
  """pending slug のみを一度観測し、通信障害を ERROR として返します。"""
  try:
    response = fetch_article(pending["slug"])
  except (OSError, ValueError) as exc:
    return CreateObservation(CreateResult.ERROR, detail=f"Remote observation failed: {exc}")
  return classify_recreate_response(response, pending, own_user_id)
