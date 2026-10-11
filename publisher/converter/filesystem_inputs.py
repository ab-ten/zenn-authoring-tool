"""検査済み source、ledger、released snapshot を安全に読み込みます。"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from checker.local_state import read_ledger, safe_path
from checker.terminal_create import _validate_article_id, _validate_observation
from converter.entry_guards import CheckedInputs


def _read_markdown(root: Path, path: Path) -> str:
  """repository 内の通常ファイルを UTF-8 として読み、改行を保持します。"""
  try:
    relative = path.relative_to(root)
    target = safe_path(root, *relative.parts)
    if not target.is_file():
      raise ValueError("Missing regular Markdown file")
    return target.read_bytes().decode("utf-8")
  except (OSError, ValueError) as exc:
    raise ValueError(f"Cannot load Markdown {path}: {exc}") from exc


def load_ledger(deploy_root: Path) -> dict:
  """pending を再確認し、snapshot path 用 UUID と confirmed observation を検証します。

  confirmed state、release、slug と記事間の整合性は既存 planner が検証します。
  返した ledger は orchestration 内でのみ使用し、成功結果には公開しません。
  """
  try:
    ledger = read_ledger(deploy_root)
    if ledger["pending_next_state"]:
      raise ValueError("Unresolved pending_next_state")
    for article_id, confirmed in ledger["articles"].items():
      _validate_article_id(article_id)
      if not isinstance(confirmed, dict):
        raise ValueError(f"Invalid confirmed article: {article_id}")
      try:
        _validate_observation(confirmed.get("successful_zenn_observation"))
      except ValueError as exc:
        raise ValueError(f"Invalid confirmed observation for {article_id}: {exc}") from exc
    return ledger
  except (OSError, ValueError) as exc:
    raise ValueError(f"Cannot load ledger at {deploy_root}: {exc}") from exc


def load_sources(inputs: CheckedInputs) -> tuple[str, ...]:
  """検査済み source 集合を読み込みます。filesystem の再探索は行いません。"""
  return tuple(_read_markdown(inputs.authoring_root, source.path) for source in inputs.sources)


def load_released_snapshots(deploy_root: Path, ledger: Mapping) -> Mapping[str, str]:
  """active/erased 全 confirmed article の最新 snapshot を読み込みます。"""
  snapshots = {}
  for article_id in ledger["articles"]:
    _validate_article_id(article_id)
    path = safe_path(deploy_root, ".zenn-authoring", "released", f"{article_id}.md")
    snapshots[article_id] = _read_markdown(deploy_root, path)
  return MappingProxyType(snapshots)
