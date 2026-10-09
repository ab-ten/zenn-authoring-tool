"""Minimal, read-only Zenn article HTTP adapter for the P0-CHECK-02 boundary."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from checker.create_observation import ArticleResponse


_SLUG = re.compile(r"[a-z0-9_-]{12,50}\Z")
_MAX_RESPONSE = 8 * 1024 * 1024


def fetch_article(slug: str) -> ArticleResponse:
  """GET Zenn article metadata and return its HTTP status plus JSON payload.

  Uses no credentials and performs no write requests. A 404 is returned as
  such; other HTTP failures are passed to the classifier. Transport errors,
  invalid JSON, and oversized responses raise ValueError/OSError.
  This unofficial endpoint can change without notice.
  """
  if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
    raise ValueError("Invalid article slug")

  req = Request(f"https://zenn.dev/api/articles/{slug}",
                headers={"Accept": "application/json", "User-Agent": "zenn-authoring-tool/0"})
  try:
    try:
      with urlopen(req, timeout=15) as response:
        status = response.status
        body = response.read(_MAX_RESPONSE + 1)
    except HTTPError as exc:
      status = exc.code
      body = b""
  except URLError as exc:
    raise OSError(f"Cannot observe article: {exc.reason}") from exc

  if status != 200:
    return ArticleResponse(status=status)
  if len(body) > _MAX_RESPONSE:
    raise ValueError("Zenn API response exceeds size limit")
  try:
    payload = json.loads(body)
  except (UnicodeError, json.JSONDecodeError) as exc:
    raise ValueError("Zenn API returned invalid JSON") from exc
  return ArticleResponse(status=status, payload=payload)
