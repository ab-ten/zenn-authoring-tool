"""DELETE preflight の警告、drift、local guard と不変性を検証します。"""

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from checker.create_observation import ArticleResponse
from checker.create_preflight import ArticlePlan, PublishPlan, PreflightStatus, preflight_create
from checker.delete_preflight import DeleteArticlePlan, DeleteArticlePreflight, preflight_delete
from checker.update_preflight import UpdateArticlePlan, preflight_update
import test_create_preflight as fixtures
from test_create_preflight import ID, OTHER, SLUG, SECOND
from test_update_preflight import BASE, CURRENT, response


class DeletePreflightTests(unittest.TestCase):
  tree = fixtures.CreatePreflightTests.tree
  write_ledger = fixtures.CreatePreflightTests.write_ledger

  def setUp(self):
    fixtures.CreatePreflightTests.setUp(self)
    self.ledger["articles"][ID] = self.confirmed(SLUG)
    self.write_ledger()
    (self.meta / "released" / f"{ID}.md").write_bytes(b"confirmed snapshot\n")
    self.plan = PublishPlan((DeleteArticlePlan(ID, "DELETE", SLUG, "erased", 3),))

  def confirmed(self, slug):
    return {"state": "active", "slug": slug, "successful_release": 3,
            "successful_zenn_observation": dict(BASE)}

  def check(self, expected, observed=None, *, plan=None, failure=None, calls=1):
    plan = self.plan if plan is None else plan
    before, original, entries = self.tree(), repr(plan), plan.articles
    fetch = Mock(return_value=response() if observed is None else observed, side_effect=failure)
    result = preflight_delete(self.root, plan, fetch)
    self.assertEqual(result.result, expected)
    self.assertEqual(fetch.call_count, calls)
    self.assertEqual(self.tree(), before)
    self.assertEqual(repr(plan), original)
    self.assertIs(plan.articles, entries)
    self.assertFalse(hasattr(result, "baseline_observation"))
    for item in result.articles:
      self.assertIsInstance(item, DeleteArticlePreflight)
      self.assertIn(item.result, (PreflightStatus.OK, PreflightStatus.ERROR))
      self.assertIsInstance(item.warnings, tuple)
      self.assertFalse(hasattr(item, "baseline_observation"))
    return result

  def test_404_warning_without_payload(self):
    item = self.check(PreflightStatus.OK, ArticleResponse(404)).articles[0]
    self.assertIn("already been deleted", item.warnings[0])
    self.assertIsNone(item.current_observation)
    self.check(PreflightStatus.OK, ArticleResponse(404, {"invalid": True}))

  def test_own_user_matching_without_warning(self):
    item = self.check(PreflightStatus.OK).articles[0]
    self.assertEqual(item.warnings, ())
    self.assertEqual(item.ledger_observation, BASE)
    self.assertEqual(item.current_observation, BASE)
    self.assertEqual(set(item.current_observation), set(BASE))

  def test_own_user_drift_diagnostics(self):
    item = self.check(PreflightStatus.ERROR, response(CURRENT)).articles[0]
    self.assertIn("Remote drift", item.detail)
    self.assertEqual(item.ledger_observation, BASE)
    self.assertEqual(item.current_observation, CURRENT)
    self.assertEqual(item.warnings, ())

  def test_other_user_warning(self):
    item = self.check(PreflightStatus.OK, response(owner=3)).articles[0]
    self.assertIn("reused by another user", item.warnings[0])
    self.assertIsNone(item.current_observation)

  def test_other_user_requires_no_timestamps(self):
    for observation in ({}, {key: None for key in BASE}, {key: True for key in BASE}):
      with self.subTest(observation=observation):
        item = self.check(PreflightStatus.OK, response(observation, owner=3)).articles[0]
        self.assertTrue(item.warnings)

  def test_invalid_remote_owner_no_username_fallback(self):
    for owner in (None, True, False, 0, -1, "2", 2.0):
      with self.subTest(owner=owner):
        self.check(PreflightStatus.ERROR, response(owner=owner))

  def test_invalid_remote_schema_slug_and_transport(self):
    for payload in (None, [], {}, {"article": []}, {"article": {"slug": SLUG}},
                    {"article": {"slug": SLUG, "user": {"username": "same-name"}, **BASE}}):
      with self.subTest(payload=payload):
        self.check(PreflightStatus.ERROR, ArticleResponse(200, payload))
    self.check(PreflightStatus.ERROR, response(slug=SECOND))
    self.check(PreflightStatus.ERROR, {"status": 404})

  def test_own_user_invalid_observation(self):
    self.check(PreflightStatus.ERROR, response({}))
    for key in BASE:
      for value in (None, "", True, 123):
        with self.subTest(key=key, value=value):
          self.check(PreflightStatus.ERROR, response({**BASE, key: value}))

  def test_unexpected_http_status(self):
    for status in (201, 302, 401, 403, 408, 429, 500, True, "404", None):
      with self.subTest(status=status):
        self.check(PreflightStatus.ERROR, ArticleResponse(status))

  def test_transport_failure(self):
    for failure in (OSError("network"), TimeoutError("timeout"),
                    ValueError("JSON"), RuntimeError("adapter")):
      with self.subTest(failure=failure):
        self.check(PreflightStatus.ERROR, failure=failure)

  def test_pending_guard_including_empty(self):
    self.ledger["pending_next_state"][OTHER] = {"operation": "CREATE"}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)
    self.check(PreflightStatus.ERROR, plan=PublishPlan(), calls=0)

  def test_missing_confirmed(self):
    self.ledger["articles"].clear()
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_malformed_or_nonactive_confirmed(self):
    for value in (None, [], "invalid", {},
                  *({**self.confirmed(SLUG), "state": state} for state in ("erased", "hold", None))):
      with self.subTest(value=value):
        self.ledger["articles"][ID] = value
        self.write_ledger()
        self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_confirmed_fields(self):
    for key, values in {
      "slug": (None, "short", SECOND),
      "successful_release": (None, True, 0, -1, "3", 3.0),
      "successful_zenn_observation": (None, {}, [], {**BASE, "published_at": ""}),
    }.items():
      for value in values:
        with self.subTest(key=key, value=value):
          self.ledger["articles"][ID] = {**self.confirmed(SLUG), key: value}
          self.write_ledger()
          self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_confirmed_observation_fields(self):
    for key in BASE:
      for value in (None, "", True, 123):
        with self.subTest(key=key, value=value):
          self.ledger["articles"][ID] = {
            **self.confirmed(SLUG), "successful_zenn_observation": {**BASE, key: value}}
          self.write_ledger()
          self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_plan_fields(self):
    for key, values in {
      "article_id": ("../invalid", ID.upper(), None),
      "state": (None, "hold", "active"),
      "slug": (None, "short", SECOND, SLUG + "\n"),
      "release": (None, True, 0, 2, 4, "3", 3.0),
    }.items():
      for value in values:
        with self.subTest(key=key, value=value):
          entry = replace(self.plan.articles[0], **{key: value})
          self.check(PreflightStatus.ERROR, plan=PublishPlan((entry,)), calls=0)

  def test_observation_override_rejected_unless_none(self):
    entry = self.plan.articles[0]
    for value in (BASE, {}, [], "invalid", False, 0):
      with self.subTest(value=value):
        overridden = SimpleNamespace(**vars(entry), observation_override=value)
        result = self.check(PreflightStatus.ERROR, plan=PublishPlan((overridden,)), calls=0)
        self.assertIn("override", result.articles[0].detail)
    allowed = SimpleNamespace(**vars(entry), observation_override=None)
    self.check(PreflightStatus.OK, plan=PublishPlan((allowed,)))

  def test_other_confirmed_collision_outside_plan(self):
    self.ledger["articles"][OTHER] = self.confirmed(SLUG)
    self.write_ledger()
    result = self.check(PreflightStatus.ERROR, calls=0)
    self.assertIn(OTHER, result.articles[0].detail)

  def test_other_confirmed_malformed_dictionary(self):
    for value in (None, [], "invalid", 1):
      with self.subTest(value=value):
        self.ledger["articles"][OTHER] = value
        self.write_ledger()
        result = self.check(PreflightStatus.ERROR, calls=0)
        self.assertIn(OTHER, result.articles[0].detail)

  def test_other_confirmed_invalid_slug(self):
    for value in ({}, *({"slug": slug} for slug in (
        None, True, 123, [], "", "a" * 11, "a" * 51,
        "UPPERCASE-SLUG", "../unsafe-slug", SECOND + "\n"))):
      with self.subTest(value=value):
        self.ledger["articles"][OTHER] = value
        self.write_ledger()
        self.check(PreflightStatus.ERROR, calls=0)

  def test_valid_other_confirmed(self):
    self.ledger["articles"][OTHER] = self.confirmed(SECOND)
    self.write_ledger()
    self.check(PreflightStatus.OK)

  def multi_plan(self):
    self.ledger["articles"][OTHER] = self.confirmed(SECOND)
    self.write_ledger()
    return PublishPlan((*self.plan.articles,
                        DeleteArticlePlan(OTHER, "DELETE", SECOND, "erased", 3)))

  def test_duplicate_article_id(self):
    entry = self.plan.articles[0]
    self.check(PreflightStatus.ERROR, plan=PublishPlan((entry, entry)), calls=0)

  def test_duplicate_slug(self):
    plan = self.multi_plan()
    self.ledger["articles"][OTHER]["slug"] = SLUG
    self.write_ledger()
    self.check(PreflightStatus.ERROR,
               plan=PublishPlan((plan.articles[0], replace(plan.articles[1], slug=SLUG))), calls=0)

  def test_multiple_all_ok(self):
    result = self.check(PreflightStatus.OK, plan=self.multi_plan(), calls=2,
                        failure=[response(), response(slug=SECOND)])
    self.assertEqual(len(result.articles), 2)
    self.assertTrue(all(item.warnings == () for item in result.articles))

  def test_multiple_warnings(self):
    result = self.check(PreflightStatus.OK, plan=self.multi_plan(), calls=2,
                        failure=[ArticleResponse(404), response({}, slug=SECOND, owner=3)])
    self.assertTrue(all(item.warnings for item in result.articles))
    self.assertIn("deleted", result.articles[0].warnings[0])
    self.assertIn("reused", result.articles[1].warnings[0])

  def test_partial_error_retains_each_warning(self):
    plan = self.multi_plan()
    for warning in (ArticleResponse(404), response({}, owner=3)):
      for responses in ([warning, response(CURRENT, slug=SECOND)],
                        [OSError("network"), replace(warning, payload={"article": {
                          "slug": SECOND, "user": {"id": 3}}})]):
        with self.subTest(responses=responses):
          result = self.check(PreflightStatus.ERROR, plan=plan, calls=2, failure=responses)
          self.assertEqual(len(result.articles), 2)
          self.assertEqual(sum(bool(item.warnings) for item in result.articles), 1)
          self.assertTrue(any(item.detail for item in result.articles if item.result == PreflightStatus.ERROR))

  def test_all_local_entries_validated_before_http(self):
    plan = self.multi_plan()
    self.check(PreflightStatus.ERROR,
               plan=PublishPlan((plan.articles[0], replace(plan.articles[1], release=4))), calls=0)
    result = self.check(PreflightStatus.ERROR, plan=PublishPlan(tuple(
      replace(entry, release=4) for entry in plan.articles)), calls=0)
    self.assertEqual(len(result.articles), 2)

  def test_unsupported_mixed_operations(self):
    for operation in ("CREATE", "UPDATE", "RECREATE", "", None):
      with self.subTest(operation=operation):
        self.check(PreflightStatus.ERROR, plan=PublishPlan((
          *self.plan.articles, ArticlePlan(OTHER, operation, SECOND))), calls=0)

  def test_empty_skips_config_and_http(self):
    self.config.unlink()
    result = self.check(PreflightStatus.OK, plan=PublishPlan(), calls=0)
    self.assertEqual(result.articles, ())

  def test_plan_requires_tuple(self):
    self.check(PreflightStatus.ERROR, plan=SimpleNamespace(articles=list(self.plan.articles)), calls=0)

  def test_invalid_or_missing_config(self):
    for content in ('{}', '{"zenn_user":{"id":true,"username":"same-name"}}', 'invalid'):
      self.config.write_text(content)
      self.check(PreflightStatus.ERROR, calls=0)
    self.config.unlink()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_or_missing_ledger(self):
    path = self.meta / "ledger.json"
    for content in ('[]', '{}', 'invalid'):
      path.write_text(content)
      self.check(PreflightStatus.ERROR, calls=0)
    path.unlink()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_symlink_paths_rejected(self):
    for path in (self.config, self.meta / "ledger.json"):
      content = path.read_bytes()
      target = self.root / "target.json"
      target.write_bytes(content)
      path.unlink()
      path.symlink_to(target)
      self.check(PreflightStatus.ERROR, calls=0)
      path.unlink()
      path.write_bytes(content)

  def test_frozen_models_and_independent_diagnostics(self):
    payload = response(CURRENT)
    item = self.check(PreflightStatus.ERROR, payload).articles[0]
    payload.payload["article"]["published_at"] = "mutated"
    self.assertEqual(item.current_observation, CURRENT)
    with self.assertRaises(TypeError):
      item.current_observation["published_at"] = "bad"
    with self.assertRaises(TypeError):
      item.ledger_observation["published_at"] = "bad"
    with self.assertRaises(FrozenInstanceError):
      item.warnings = ("bad",)
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles[0].release = 4
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles = ()

  def test_observation_string_comparison(self):
    self.ledger["articles"][ID]["successful_zenn_observation"] = dict(CURRENT)
    self.write_ledger()
    self.check(PreflightStatus.OK, response(CURRENT))
    self.check(PreflightStatus.ERROR, response({**CURRENT, "published_at": "2026-10-09T15:00:00Z"}))

  def test_structural_converter_plan(self):
    class ConverterPlan:
      candidate_content = "converter-owned"
      @property
      def articles(inner):
        return self.plan.articles
    self.check(PreflightStatus.OK, plan=ConverterPlan())

  def test_default_adapter_with_mocked_http(self):
    before = self.tree()
    with patch("checker.zenn_http.urlopen", side_effect=OSError("mock")) as http:
      result = preflight_delete(self.root, self.plan)
    self.assertEqual(result.result, PreflightStatus.ERROR)
    http.assert_called_once()
    self.assertEqual(self.tree(), before)

  def test_create_update_regression(self):
    before = self.tree()
    created = preflight_create(self.root, PublishPlan((ArticlePlan(OTHER, "CREATE", SECOND),)),
                               Mock(return_value=ArticleResponse(404)))
    updated = preflight_update(self.root, PublishPlan((
      UpdateArticlePlan(ID, "UPDATE", SLUG, "active", 4),)), Mock(return_value=response()))
    self.assertEqual(created.result, PreflightStatus.OK)
    self.assertEqual(updated.result, PreflightStatus.OK)
    self.assertEqual(updated.articles[0].baseline_observation, BASE)
    self.assertFalse(hasattr(created.articles[0], "warnings"))
    self.assertEqual(self.tree(), before)


if __name__ == "__main__":
  unittest.main()
