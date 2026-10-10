"""pending DELETE の remote result を書き込みなしで判定します。"""

from typing import Callable

from checker.create_observation import (
  ArticleResponse, CreateObservation, CreateResult, _valid_user_id,
  classify_create_response,
)


def classify_delete_response(response: ArticleResponse, pending: dict,
                             own_user_id: int) -> CreateObservation:
  """検証済み pending の publication 不存在を数値 owner ID で判定します。

  DELETE は現在の observation timestamps を要求しません。
  HTTP エラーの分類は既存の共通規則を使用します。
  """
  if not _valid_user_id(own_user_id):
    return CreateObservation(CreateResult.ERROR, detail="Invalid configured owner ID")
  if not isinstance(response, ArticleResponse) or type(response.status) is not int:
    return CreateObservation(CreateResult.ERROR, detail="Invalid transport response")
  if response.status == 404:
    return CreateObservation(CreateResult.SUCCESS)
  if response.status != 200:
    return classify_create_response(response, pending["slug"], own_user_id)
  payload = response.payload
  article = payload.get("article") if isinstance(payload, dict) else None
  if not isinstance(article, dict):
    return CreateObservation(CreateResult.ERROR, detail="Missing article object")
  if article.get("slug") != pending["slug"]:
    return CreateObservation(CreateResult.ERROR, detail="Unexpected article slug")
  user = article.get("user")
  owner = user.get("id") if isinstance(user, dict) else None
  if not _valid_user_id(owner):
    return CreateObservation(CreateResult.ERROR, detail="Missing or invalid article.user.id")
  return CreateObservation(CreateResult.PENDING if owner == own_user_id else CreateResult.SUCCESS)


def observe_delete_result(pending: dict, own_user_id: int,
                          fetch_article: Callable[[str], ArticleResponse]) -> CreateObservation:
  """検証済み pending を一度観測し、通信障害を ERROR として返します。"""
  try:
    response = fetch_article(pending["slug"])
  except (OSError, ValueError) as exc:
    return CreateObservation(CreateResult.ERROR, detail=f"Remote observation failed: {exc}")
  return classify_delete_response(response, pending, own_user_id)
