"""既存の計画・Preflight・Apply を逐次接続します。"""

from datetime import datetime
from pathlib import Path
from typing import Callable

from checker.create_observation import ArticleResponse
from checker.create_preflight import PreflightStatus
from checker.zenn_http import fetch_article as http_fetch_article
from converter.apply_integration import apply_publish
from converter.preflight_integration import PreflightOutcome, preflight_publish


def run_convert(
    article_root: Path,
    *,
    now: datetime | None = None,
    fetch_article: Callable[[str], ArticleResponse] = http_fetch_article,
) -> PreflightOutcome:
  """明示された記事 repository root を変換し、元の PreflightOutcome を返します。

  preflight_publish を一度だけ呼び、全体 OK の場合のみ同じ outcome と
  不変の計画を apply_publish に一度だけ渡します。再計画・再分類・追加の
  remote 観測は行いません。COLLISION と ERROR では書き込みません。
  OK では deploy の記事・画像を変更し、全変更後に pending ledger を
  原子的に置換します。authoring source と成功済み状態は変更しません。
  検証・実行の例外は伝播します。Apply の途中変更は rollback しません。
  """
  outcome = preflight_publish(article_root, now=now, fetch_article=fetch_article)
  if outcome.preflight.result is PreflightStatus.OK:
    apply_publish(outcome)
  return outcome
