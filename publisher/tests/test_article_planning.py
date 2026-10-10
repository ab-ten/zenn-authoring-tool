"""メモリ上の fixture で記事 planning の中心契約を検証します。"""

from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from checker import create_preflight, update_preflight, delete_preflight, recreate_preflight
from converter.models import ArticlePlanningResult, PreparedSourceRecord
from converter.planning import plan_articles


ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "planning-article-slug"
SECOND = "another-article-slug"
SNAPSHOT = "---\npublished: true\n---\narticle\n"
OBSERVATION = {key: "timestamp" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}


def confirmed(state="active", slug=SLUG):
  return {"state": state, "slug": slug, "successful_release": 3,
          "successful_zenn_observation": dict(OBSERVATION)}


class ArticlePlanningTests(unittest.TestCase):
  def setUp(self):
    self.source = PreparedSourceRecord(ID, "active", SLUG, 4, SNAPSHOT + "changed")
    self.ledger = {"articles": {ID: confirmed()}, "pending_next_state": {}}
    self.snapshots = {ID: SNAPSHOT}

  def plan(self, source=None):
    return plan_articles([self.source if source is None else source],
                         self.ledger, self.snapshots)

  def test_all_state_combinations_and_content_comparison(self):
    for prior in (None, "active", "erased"):
      for state in ("active", "hold", "erased"):
        for changed in (False, True):
          with self.subTest(prior=prior, state=state, changed=changed):
            self.ledger["articles"] = {} if prior is None else {ID: confirmed(prior)}
            expected = {
              (None, "active"): "CREATE", ("active", "erased"): "DELETE",
              ("erased", "active"): "RECREATE",
            }.get((prior, state))
            if prior == "active" and state == "active" and changed:
              expected = "UPDATE"
            release = 1 if prior is None else 3 + int(
              state == "active" and changed)
            source = replace(self.source, state=state, release=release,
                             candidate_content=SNAPSHOT + ("changed" if changed else ""))
            result = self.plan(source)
            self.assertEqual(tuple(e.operation for e in result.articles),
                             () if expected is None else (expected,))
            if expected:
              entry = result.articles[0]
              self.assertEqual(entry.state, state)
              self.assertEqual(entry.release, release)
              self.assertEqual(entry.candidate_content,
                               None if expected == "DELETE" else source.candidate_content)

  def test_invalid_release_transitions(self):
    for operation, prior, state, changed, valid in (
        ("CREATE", None, "active", True, 1),
        ("UPDATE", "active", "active", True, 4),
        ("no-op", "active", "active", False, 3),
        ("RECREATE", "erased", "active", True, 4),
        ("RECREATE", "erased", "active", False, 3),
        ("DELETE", "active", "erased", True, 3)):
      self.ledger["articles"] = {} if prior is None else {ID: confirmed(prior)}
      for release in (None, True, False, 0, -1, "1", 1.0, valid - 1, valid + 1):
        with self.subTest(operation=operation, changed=changed, release=release):
          with self.assertRaises(ValueError):
            self.plan(replace(self.source, state=state, release=release,
                              candidate_content=SNAPSHOT + ("changed" if changed else "")))

  def test_hold_uncommitted_style_fixture(self):
    source = replace(self.source, state="hold", slug=None, release=None,
                     candidate_content="unfinished Markdown with local edits")
    self.assertEqual(self.plan(source).articles, ())

  def test_erased_without_confirmed_needs_no_publish_fields(self):
    self.ledger["articles"] = {}
    self.assertEqual(self.plan(PreparedSourceRecord(ID, "erased")).articles, ())

  def test_uuid_normalization_and_ledger_lookup(self):
    self.assertEqual(self.plan(replace(self.source, article_id=ID.upper())).articles[0].article_id, ID)
    for bad in (None, True, 1, "invalid", "", "../article.md"):
      with self.subTest(bad=bad), self.assertRaises(ValueError):
        self.plan(replace(self.source, article_id=bad))
    with self.assertRaisesRegex(ValueError, "Duplicate normalized"):
      plan_articles([self.source, replace(self.source, article_id=ID.upper())],
                    self.ledger, self.snapshots)

  def test_ledger_ids_are_not_silently_normalized(self):
    self.ledger["articles"] = {ID.upper(): confirmed()}
    with self.assertRaises(ValueError):
      self.plan()

  def test_invalid_source_state(self):
    for state in (None, "", "ACTIVE", "deleted", []):
      with self.subTest(state=state), self.assertRaises(ValueError):
        self.plan(replace(self.source, state=state))

  def test_invalid_confirmed_state_and_release(self):
    for value in (None, {}, [], {**confirmed(), "state": "hold"},
                  {**confirmed(), "state": "unknown"}):
      self.ledger["articles"][ID] = value
      with self.subTest(value=value), self.assertRaises(ValueError):
        self.plan()
    for value in (None, True, 0, -1, "3", 3.0):
      self.ledger["articles"][ID] = {**confirmed(), "successful_release": value}
      with self.subTest(value=value), self.assertRaises(ValueError):
        self.plan()

  def test_missing_snapshot_for_every_managed_state(self):
    self.snapshots.clear()
    for prior in ("active", "erased"):
      for state in ("active", "hold", "erased"):
        self.ledger["articles"][ID] = confirmed(prior)
        with self.subTest(prior=prior, state=state), self.assertRaisesRegex(
            ValueError, "released snapshot"):
          self.plan(replace(self.source, state=state))

  def test_delete_does_not_compare_or_retain_content(self):
    for content in (None, SNAPSHOT, "completely different"):
      entry = self.plan(replace(self.source, state="erased", release=3,
                                candidate_content=content)).articles[0]
      self.assertEqual(entry.operation, "DELETE")
      self.assertIsNone(entry.candidate_content)

  def test_content_whitespace_and_line_endings_are_significant(self):
    for content in (SNAPSHOT.replace("\n", "\r\n"), SNAPSHOT.rstrip(), SNAPSHOT + " "):
      with self.subTest(content=content):
        self.assertEqual(self.plan(replace(self.source, candidate_content=content)).articles[0].operation,
                         "UPDATE")

  def test_invalid_slug(self):
    for slug in (None, "", "short", "a" * 51, "A" * 12, "invalid/slug", SLUG + "\n", []):
      with self.subTest(slug=slug), self.assertRaises(ValueError):
        self.plan(replace(self.source, slug=slug))

  def test_slug_length_boundaries(self):
    self.ledger["articles"] = {}
    for slug in ("a" * 12, "a" * 50, "abc_123-xyz90"):
      with self.subTest(slug=slug):
        self.assertEqual(self.plan(replace(self.source, release=1, slug=slug)).articles[0].slug,
                         slug)

  def test_invalid_input_maps_and_content(self):
    for ledger in (None, {}, {"articles": {}, "pending_next_state": []},
                   {"articles": [], "pending_next_state": {}}):
      with self.subTest(ledger=ledger), self.assertRaises(ValueError):
        plan_articles([self.source], ledger, self.snapshots)
    for content in (None, [], b"article"):
      with self.subTest(content=content), self.assertRaises(ValueError):
        self.plan(replace(self.source, candidate_content=content))
      with self.subTest(snapshot=content), self.assertRaises(ValueError):
        plan_articles([self.source], self.ledger, {ID: content})

  def test_invalid_confirmed_slugs(self):
    for slug in (None, "short", SLUG + "\n"):
      self.ledger["articles"][ID] = {**confirmed(), "slug": slug}
      with self.subTest(slug=slug), self.assertRaises(ValueError):
        self.plan()
    self.ledger["articles"] = {ID: confirmed(), OTHER: confirmed()}
    self.snapshots[OTHER] = SNAPSHOT
    with self.assertRaisesRegex(ValueError, "Duplicate confirmed slug"):
      self.plan()

  def test_update_delete_and_noop_slug_changes(self):
    for state, release, content in (("active", 4, "changed"),
                                    ("active", 3, SNAPSHOT), ("erased", 3, "changed")):
      with self.subTest(state=state, release=release), self.assertRaises(ValueError):
        self.plan(replace(self.source, state=state, release=release,
                          candidate_content=content, slug=SECOND))

  def test_recreate_alternate_slug(self):
    self.ledger["articles"][ID] = confirmed("erased")
    entry = self.plan(replace(self.source, slug=SECOND)).articles[0]
    self.assertEqual((entry.operation, entry.slug), ("RECREATE", SECOND))

  def test_candidate_slug_duplicates(self):
    self.ledger["articles"] = {}
    source = replace(self.source, release=1)
    with self.assertRaisesRegex(ValueError, "Duplicate candidate slug"):
      plan_articles([source, replace(source, article_id=OTHER)], self.ledger, {})

  def test_collision_with_another_confirmed_article_even_on_hold(self):
    self.ledger["articles"][OTHER] = confirmed("erased", SECOND)
    self.snapshots[OTHER] = SNAPSHOT
    hold = PreparedSourceRecord(OTHER, "hold")
    with self.assertRaisesRegex(ValueError, "overlaps another"):
      plan_articles([replace(self.source, article_id="9c8daef1-b196-4a6f-a213-000000000001",
                             release=1, slug=SECOND), self.source, hold],
                    self.ledger, self.snapshots)

  def test_ledger_managed_source_missing(self):
    with self.assertRaisesRegex(ValueError, "has no source"):
      plan_articles([], self.ledger, self.snapshots)

  def test_override_only_update_including_noops(self):
    for prior, state, changed in ((None, "active", True), (None, "erased", False),
                                  ("active", "hold", True), ("active", "erased", True),
                                  ("active", "active", False), ("erased", "active", True),
                                  ("erased", "erased", False)):
      self.ledger["articles"] = {} if prior is None else {ID: confirmed(prior)}
      source = replace(self.source, state=state, observation_override=OBSERVATION,
                       candidate_content=SNAPSHOT + ("changed" if changed else ""))
      with self.subTest(prior=prior, state=state), self.assertRaisesRegex(ValueError, "Only UPDATE"):
        self.plan(source)

  def test_override_schema_matches_checker(self):
    for value in ({}, [], "invalid", {**OBSERVATION, "published_at": ""},
                  {**OBSERVATION, "published_at": 1}):
      with self.subTest(value=value), self.assertRaises(ValueError):
        self.plan(replace(self.source, observation_override=value))
    entry = self.plan(replace(self.source, observation_override={**OBSERVATION, "extra": []})).articles[0]
    self.assertEqual(dict(entry.observation_override), OBSERVATION)
    update_preflight._validate_entry(entry, self.ledger)

  def test_pending_guard_precedes_source_iteration(self):
    def sources():
      self.fail("Source processing must not begin")
      yield self.source
    self.ledger["pending_next_state"][OTHER] = {"operation": "CREATE"}
    with self.assertRaisesRegex(ValueError, "Unresolved pending"):
      plan_articles(sources(), self.ledger, self.snapshots)
    with self.assertRaisesRegex(ValueError, "Unresolved pending"):
      plan_articles([], self.ledger, self.snapshots)

  def test_immutable_result_and_input_mutations(self):
    override = dict(OBSERVATION)
    sources = [replace(self.source, observation_override=override)]
    result = plan_articles(sources, self.ledger, self.snapshots)
    entry = result.articles[0]
    before = repr(result)
    override["published_at"] = "mutated"
    sources.clear()
    self.ledger["articles"][ID]["slug"] = SECOND
    self.snapshots[ID] = "mutated"
    self.assertEqual(repr(result), before)
    self.assertIsInstance(result.articles, tuple)
    with self.assertRaises(FrozenInstanceError):
      entry.release = 9
    with self.assertRaises(FrozenInstanceError):
      result.articles = ()
    with self.assertRaises(TypeError):
      entry.observation_override["published_at"] = "mutated"
    with self.assertRaises(TypeError):
      ArticlePlanningResult([])

  def test_classification_runs_once(self):
    from converter import planning
    with patch("converter.planning._classify", wraps=planning._classify) as classify:
      self.plan()
    classify.assert_called_once()

  def test_entries_satisfy_existing_local_checker_contracts(self):
    for operation, prior, state, release in (("CREATE", None, "active", 1),
        ("UPDATE", "active", "active", 4), ("DELETE", "active", "erased", 3),
        ("RECREATE", "erased", "active", 4)):
      self.ledger["articles"] = {} if prior is None else {ID: confirmed(prior)}
      entry = self.plan(replace(self.source, state=state, release=release)).articles[0]
      with self.subTest(operation=operation):
        {"CREATE": create_preflight, "UPDATE": update_preflight,
         "DELETE": delete_preflight, "RECREATE": recreate_preflight}[operation]._validate_entry(
           entry, self.ledger)


if __name__ == "__main__":
  unittest.main()
