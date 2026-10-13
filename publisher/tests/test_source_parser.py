"""candidate content の完全一致、管理ブロック、Front Matter を検証します。"""

import json
import unittest

from converter.planning import plan_articles
from converter.source_parser import parse_source


ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
SLUG = "parser-article-slug"
FRONT = '---\ntitle: "Test" # YAML comment\nemoji: "😀"\ntype: tech\ntopics: [python]\npublished: true\n---\n'


def source(*, state="active", release=1, date=None, body="Body\n", front=FRONT):
  if date is not None:
    front = front[:-4] + f"published_at: {date}\n---\n"
  return (front + f"<!-- #id:{ID} -->\n<!-- #state:{state} -->\n"
          f"<!-- #slug:{SLUG} -->\n<!-- #release:{release} -->\n" + body)


class SourceParserTests(unittest.TestCase):
  def test_candidate_exact_match_preserves_everything_except_comments(self):
    for newline in ("\n", "\r\n"):
      for trailing in ("", "\n", "\n\n"):
        body = ('\n<!-- ordinary -->\n# Heading\n\n```html\n'
                '<!-- #id:not-management -->\n```\n<!-- #state:hold -->' + trailing)
        markdown = source(body=body).replace("\n", newline)
        expected = (FRONT + body).replace("\n", newline)
        with self.subTest(newline=newline, trailing=trailing):
          parsed = parse_source(markdown)
          self.assertEqual(parsed.record.candidate_content, expected)
          self.assertEqual(parsed.record.article_id, ID)

  def test_management_lines_removed_but_explicit_blank_lines_preserved(self):
    markdown = FRONT + f" \t<!-- #id:{ID} -->\r\n\n <!-- #state:hold -->  \nBody"
    self.assertEqual(parse_source(markdown).record.candidate_content,
                     FRONT + "\nBody")

  def test_optional_slug_and_release_before_publish(self):
    parsed = parse_source(FRONT + f"<!-- #id:{ID} -->\n<!-- #state:hold -->\nBody")
    self.assertIsNone(parsed.record.slug)
    self.assertIsNone(parsed.record.release)

  def test_missing_required_management_comments(self):
    for comment in (f"<!-- #id:{ID} -->", "<!-- #state:active -->"):
      with self.subTest(comment=comment), self.assertRaises(ValueError):
        parse_source(source().replace(comment, ""))

  def test_duplicate_management_fields(self):
    for key, value in (("id", ID), ("state", "active"), ("slug", SLUG), ("release", "1"),
                       ("observation-override", '{"published_at":"time"}')):
      markdown = source(body=f"<!-- #{key}:{value} -->\nBody")
      if key == "observation-override":
        markdown = markdown.replace("Body", f"<!-- #{key}:{value} -->\nBody")
      with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Duplicate"):
        parse_source(markdown)

  def test_invalid_management_syntax(self):
    for comment in ("<!-- #state active -->", "<!-- #state: -->", "<!-- #unknown:x -->",
                    "<!-- #release:1", "<!-- #id :x -->"):
      with self.subTest(comment=comment), self.assertRaises(ValueError):
        parse_source(source(body=comment))

  def test_general_comment_ends_management_block(self):
    markdown = source(body="<!-- ordinary -->\n<!-- #release:99 -->\nBody")
    self.assertIn("<!-- #release:99 -->", parse_source(markdown).record.candidate_content)
    with self.assertRaises(ValueError):
      parse_source(FRONT + "<!-- ordinary -->\n" + f"<!-- #id:{ID} -->\n<!-- #state:hold -->")

  def test_general_hash_comment_is_not_management(self):
    body = "<!-- # Heading notes -->\nBody"
    self.assertEqual(parse_source(source(body=body)).record.candidate_content,
                     FRONT + body)

  def test_front_matter_original_yaml_formatting_is_preserved(self):
    front = ('---\npublished: true\ntopics: []\n# Notes\ntype: idea\n'
             "emoji: ''\ntitle: 'Quoted title'  \n---\n")
    self.assertEqual(parse_source(source(front=front, body="")).record.candidate_content,
                     front)

  def test_optional_management_additions_and_removals_do_not_change_content(self):
    for newline in ("\n", "\r\n"):
      baseline = FRONT + f"<!-- #id:{ID} -->\n<!-- #state:hold -->\n\nBody\n"
      for optional in (f"<!-- #slug:{SLUG} -->\n", "<!-- #release:3 -->\n",
                       '<!-- #observation-override:{"published_at":"time"} -->\n'):
        with self.subTest(newline=newline, optional=optional):
          added = baseline.replace("\n\nBody", "\n" + optional + "\nBody")
          self.assertEqual(parse_source(added.replace("\n", newline)).record.candidate_content,
                           parse_source(baseline.replace("\n", newline)).record.candidate_content)

  def test_inline_and_multiple_management_comments_remain_supported(self):
    for newline in ("\n", "\r\n"):
      prefix = FRONT.replace("\n", newline)
      block = f" \t<!-- #id:{ID} --> <!-- #state:hold -->  "
      for ending in (newline, ""):
        with self.subTest(newline=newline, ending=ending):
          self.assertEqual(parse_source(prefix + block + ending).record.candidate_content, prefix)
      self.assertEqual(parse_source(prefix + block + "Body" + newline).record.candidate_content,
                       prefix + " \t   Body" + newline)

  def test_multiline_management_comments_preserve_body_blank_lines(self):
    markdown = FRONT + f"<!--\n #id:{ID}\n-->\n<!-- #state:hold -->\n\n \t\nBody\n\n"
    self.assertEqual(parse_source(markdown).record.candidate_content, FRONT + "\n \t\nBody\n\n")

  def test_override_cleanup_after_update_does_not_plan_another_update(self):
    observation = {key: "time" for key in (
      "body_updated_at", "source_repo_updated_at", "published_at")}
    for newline in ("\n", "\r\n"):
      with self.subTest(newline=newline):
        previous = parse_source(source(release=3, body="Previous\n").replace("\n", newline))
        clean = source(release=4, body="\n \t\nRevised\n\n").replace("\n", newline)
        override_line = f"<!-- #observation-override:{json.dumps(observation)} -->" + newline
        overridden = clean.replace(newline * 2 + " \t",
                                   newline + override_line + newline + " \t", 1)
        parsed = parse_source(overridden)
        ledger = {"articles": {ID: {"state": "active", "slug": SLUG,
                  "successful_release": 3}}, "pending_next_state": {}}
        update = plan_articles([parsed.record], ledger, {ID: previous.record.candidate_content})
        self.assertEqual(update.articles[0].operation, "UPDATE")
        self.assertEqual(dict(update.articles[0].observation_override), observation)
        snapshot = update.articles[0].candidate_content
        cleaned = parse_source(clean)
        self.assertEqual(cleaned.record.candidate_content, snapshot)
        ledger["articles"][ID]["successful_release"] = 4
        self.assertEqual(plan_articles([cleaned.record], ledger, {ID: snapshot}).articles, ())

  def test_invalid_uuid_is_passed_to_planner(self):
    parsed = parse_source(source().replace(ID, "not-a-uuid"))
    self.assertEqual(parsed.record.article_id, "not-a-uuid")
    with self.assertRaisesRegex(ValueError, "UUID"):
      plan_articles([parsed.record], {"articles": {}, "pending_next_state": {}}, {})

  def test_invalid_release_representation(self):
    for value in ("-1", "+1", "1.0", "one", "true", "", "1 2"):
      with self.subTest(value=value), self.assertRaises(ValueError):
        parse_source(source(release=value))

  def test_override_json_and_deferred_operation_validation(self):
    override = {key: "time" for key in (
      "body_updated_at", "source_repo_updated_at", "published_at")}
    parsed = parse_source(source(body=f"<!-- #observation-override:{json.dumps(override)} -->\nBody"))
    self.assertEqual(parsed.record.observation_override, override)
    with self.assertRaisesRegex(ValueError, "Only UPDATE"):
      plan_articles([parsed.record], {"articles": {}, "pending_next_state": {}}, {})
    for value in ("{", "NaN", "null"):
      with self.subTest(value=value), self.assertRaises(ValueError):
        parse_source(source(body=f"<!-- #observation-override:{value} -->"))

  def test_front_matter_delimiters(self):
    for markdown in ("", "Body", source().replace("---", "..."), "---\ntitle: x\n"):
      with self.subTest(markdown=markdown), self.assertRaises(ValueError):
        parse_source(markdown)

  def test_invalid_yaml_and_unsafe_tags(self):
    for front in ("---\ntitle: [\n---\n", "---\n- item\n---\n",
                  "---\n!!python/object/apply:os.system ['echo unsafe']\n---\n"):
      with self.subTest(front=front), self.assertRaises(ValueError):
        parse_source(source(front=front))

  def test_duplicate_yaml_keys(self):
    with self.assertRaisesRegex(ValueError, "Duplicate"):
      parse_source(source(front=FRONT.replace("type: tech", "type: tech\ntype: idea")))

  def test_required_front_matter_fields(self):
    for line in FRONT.splitlines()[1:-1]:
      with self.subTest(line=line), self.assertRaises(ValueError):
        parse_source(source(front=FRONT.replace(line + "\n", "")))

  def test_front_matter_basic_types(self):
    for old, new in (("title: \"Test\" # YAML comment", "title: 1"),
                     ('emoji: "😀"', "emoji: []"), ("type: tech", "type: other"),
                     ("topics: [python]", "topics: python"),
                     ("topics: [python]", "topics: [1]"),
                     ("published: true", 'published: "true"'),
                     ("published: true", "published: null")):
      with self.subTest(new=new), self.assertRaises(ValueError):
        parse_source(source(front=FRONT.replace(old, new)))

  def test_published_state_rules(self):
    for state in ("active", "hold", "erased"):
      for published in ("true", "false"):
        markdown = source(state=state, front=FRONT.replace("published: true", f"published: {published}"))
        with self.subTest(state=state, published=published):
          if state == "active" and published == "false":
            with self.assertRaises(ValueError):
              parse_source(markdown)
          else:
            self.assertEqual(parse_source(markdown).record.state, state)

  def test_title_utf16_boundaries_and_whitespace_in_all_states(self):
    for state in ("hold", "active", "erased"):
      for title in (" ", "\t", "a" * 70, "あ" * 70, "😀" * 35,
                    "a" * 68 + "😀", "\ud800" * 70):
        with self.subTest(state=state, title=repr(title)):
          front = FRONT.replace('"Test"', json.dumps(title))
          parsed = parse_source(source(state=state, front=front))
          self.assertEqual(parsed.record.candidate_content, front + "Body\n")
      for title in ("", "a" * 71, "あ" * 71, "😀" * 36, "a" * 69 + "😀"):
        with self.subTest(state=state, title=repr(title)), self.assertRaisesRegex(
            ValueError, "Front Matter title"):
          parse_source(source(state=state, front=FRONT.replace('"Test"', json.dumps(title))))

  def test_candidate_metadata_changes_do_not_change_content(self):
    first = parse_source(source(state="hold", release=1))
    second = parse_source(source(state="active", release=2))
    self.assertEqual(first.record.candidate_content, second.record.candidate_content)


if __name__ == "__main__":
  unittest.main()
