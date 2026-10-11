"""Markdown 文字列を解析します。ledger、Git、filesystem は参照しません。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

import yaml

from converter.models import PreparedSourceRecord


JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ParsedSource:
  """source record と、当該 content から解析した日時を対応付けます。"""

  record: PreparedSourceRecord
  published_at: datetime | None


class _FrontMatterLoader(yaml.SafeLoader):
  """日時の元表記を保持し、mapping の重複 key を拒否します。"""

  yaml_implicit_resolvers = {
    key: [(tag, regex) for tag, regex in values
          if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
  }

  def construct_mapping(self, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
      key = self.construct_object(key_node, deep=deep)
      if not isinstance(key, str):
        raise ValueError("Front Matter keys must be strings")
      if key in result:
        raise ValueError(f"Duplicate Front Matter key: {key}")
      result[key] = self.construct_object(value_node, deep=deep)
    return result


def parse_published_at(value: object) -> datetime:
  """許容書式と実在日時を検証し、JST の意味値を返します。"""
  if not isinstance(value, str) or re.fullmatch(
      r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2})?", value) is None:
    raise ValueError("Invalid published_at format")
  try:
    return datetime.strptime(value, "%Y-%m-%d" if len(value) == 10 else
                             "%Y-%m-%d %H:%M").replace(tzinfo=JST)
  except ValueError as exc:
    raise ValueError("Invalid published_at datetime") from exc


def _front_matter(markdown: str) -> tuple[dict, int]:
  if not isinstance(markdown, str):
    raise ValueError("Markdown must be a string")
  opening = re.match(r"---(?:\r\n|\n)", markdown)
  if opening is None:
    raise ValueError("Missing opening Front Matter delimiter")
  closing = re.search(r"^---(?:\r\n|\n|$)", markdown[opening.end():], re.MULTILINE)
  if closing is None:
    raise ValueError("Missing closing Front Matter delimiter")
  start = opening.end()
  try:
    fields = yaml.load(markdown[start:start + closing.start()], Loader=_FrontMatterLoader)
  except yaml.YAMLError as exc:
    raise ValueError("Invalid Front Matter YAML") from exc
  if not isinstance(fields, dict):
    raise ValueError("Front Matter must be a mapping")
  return fields, start + closing.end()


def _validate_front_matter(fields: dict, state: str | None = None) -> datetime | None:
  for key in ("title", "emoji", "type"):
    if not isinstance(fields.get(key), str):
      raise ValueError(f"Front Matter requires string {key}")
  if fields["type"] not in ("tech", "idea"):
    raise ValueError("Front Matter type must be tech or idea")
  if not isinstance(fields.get("topics"), list) or not all(
      isinstance(topic, str) for topic in fields["topics"]):
    raise ValueError("Front Matter topics must be a list of strings")
  if type(fields.get("published")) is not bool:
    raise ValueError("Front Matter published must be a boolean")
  if state == "active" and fields["published"] is not True:
    raise ValueError("Active source requires published: true")
  return parse_published_at(fields["published_at"]) if "published_at" in fields else None


def snapshot_published_at(content: str) -> datetime | None:
  """メモリ上の snapshot の Front Matter と日時を検証します。"""
  fields, _ = _front_matter(content)
  return _validate_front_matter(fields)


def parse_source(markdown: str) -> ParsedSource:
  """先頭管理ブロックのみを解析し、コメントの文字範囲だけを除去します。

  Front Matter 後と管理コメント間の空白は許容してそのまま保持します。
  最初の本文または一般 HTML コメントで管理ブロックを終了します。
  本文中の管理コメント風文字列も本文として保持します。
  UUID、state、slug、release 遷移と override representation は planner が検証します。
  この API は実 Converter entry ではなく、entry guards は後続工程の責務です。
  """
  fields, offset = _front_matter(markdown)
  values, spans = {}, []
  while True:
    whitespace = re.match(r"[ \t\r\n]*", markdown[offset:])
    start = offset + whitespace.end()
    if re.match(
        r"<!--\s*#(?:(?:id|slug|state|release|observation-override)\b|[A-Za-z][\w-]*\s*:)",
        markdown[start:]) is None:
      break
    end = markdown.find("-->", start + 4)
    if end == -1:
      raise ValueError("Unterminated management comment")
    end += 3
    match = re.fullmatch(
      r"<!--\s*#(id|slug|state|release|observation-override):([\s\S]*?)\s*-->",
      markdown[start:end])
    if match is None:
      raise ValueError("Invalid management comment syntax")
    key, value = match.group(1), match.group(2).strip()
    if key in values:
      raise ValueError(f"Duplicate management comment: {key}")
    if not value:
      raise ValueError(f"Empty management comment: {key}")
    values[key] = value
    spans.append((start, end))
    offset = end
  if not {"id", "state"} <= values.keys():
    raise ValueError("Missing required id or state management comment")
  release = None
  if "release" in values:
    if re.fullmatch(r"[0-9]+", values["release"]) is None:
      raise ValueError("Invalid release representation")
    release = int(values["release"])
  override = None
  if "observation-override" in values:
    def reject_constant(value):
      raise ValueError(f"Invalid JSON constant: {value}")
    try:
      override = json.loads(values["observation-override"], parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
      raise ValueError("Invalid observation override JSON") from exc
    if override is None:
      raise ValueError("Observation override cannot be null")
  published_at = _validate_front_matter(fields, values["state"])
  pieces, offset = [], 0
  for start, end in spans:
    pieces.append(markdown[offset:start])
    offset = end
  pieces.append(markdown[offset:])
  record = PreparedSourceRecord(values["id"], values["state"], values.get("slug"),
                                release, "".join(pieces), override)
  return ParsedSource(record, published_at)
