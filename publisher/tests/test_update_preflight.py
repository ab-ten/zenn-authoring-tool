"""UPDATE preflight の drift、override、baseline と不変性を検証します。"""

from dataclasses import FrozenInstanceError, replace
import json
from unittest.mock import Mock, patch
import unittest

from checker.create_observation import ArticleResponse
from checker.create_preflight import ArticlePlan, PublishPlan, PreflightStatus, preflight_create
from checker.update_preflight import UpdateArticlePlan, preflight_update
import test_create_preflight as fixtures
from test_create_preflight import ID, OTHER, SLUG, SECOND


BASE = {key: "2026-10-10T00:00:00+09:00" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}
CURRENT = {**BASE, "body_updated_at": "changed"}


def response(observation=BASE, slug=SLUG, owner=2):
  return ArticleResponse(200, {"article": {
    "slug": slug, "user": {"id": owner, "username": "same-name"},
    **observation, "extra": "excluded",
  }})


class UpdatePreflightTests(unittest.TestCase):
  tree = fixtures.CreatePreflightTests.tree
  write_ledger = fixtures.CreatePreflightTests.write_ledger

  def setUp(self):
    fixtures.CreatePreflightTests.setUp(self)
    self.ledger["articles"][ID] = self.confirmed(SLUG)
    self.write_ledger()
    self.plan = PublishPlan((UpdateArticlePlan(ID, "UPDATE", SLUG, "active", 4),))

  def confirmed(self, slug):
    return {"state": "active", "slug": slug, "successful_release": 3,
            "successful_zenn_observation": dict(BASE)}

  def check(self, expected, observed=None, *, plan=None, failure=None, calls=1):
    plan = self.plan if plan is None else plan
    before, original = self.tree(), repr(plan)
    entries = plan.articles
    fetch = Mock(return_value=response() if observed is None else observed, side_effect=failure)
    result = preflight_update(self.root, plan, fetch)
    self.assertEqual(result.result, expected)
    self.assertEqual(fetch.call_count, calls)
    self.assertEqual(self.tree(), before)
    self.assertEqual(repr(plan), original)
    self.assertIs(plan.articles, entries)
    for item in result.articles:
      if item.result == PreflightStatus.ERROR:
        self.assertIsNone(item.baseline_observation)
    return result

  def override_plan(self, value):
    return PublishPlan((replace(self.plan.articles[0], observation_override=value),))

  def test_normal_update(self):
    item = self.check(PreflightStatus.OK).articles[0]
    self.assertEqual(item.baseline_observation, BASE)
    self.assertEqual(set(item.baseline_observation), set(BASE))

  def test_drift_and_json_round_trip(self):
    item = self.check(PreflightStatus.ERROR, response(CURRENT)).articles[0]
    self.assertIn("Remote drift", item.detail)
    self.assertEqual(item.ledger_observation, BASE)
    self.assertEqual(item.current_observation, CURRENT)
    copied = json.loads(item.observation_override_json)
    self.assertEqual(copied, CURRENT)
    self.check(PreflightStatus.OK, response(CURRENT), plan=self.override_plan(copied))

  def test_valid_override(self):
    item = self.check(PreflightStatus.OK, response(CURRENT),
                      plan=self.override_plan(CURRENT)).articles[0]
    self.assertEqual(item.baseline_observation, CURRENT)

  def test_override_mismatch(self):
    item = self.check(PreflightStatus.ERROR, response(),
                      plan=self.override_plan(CURRENT)).articles[0]
    self.assertEqual(item.override_observation, CURRENT)
    self.assertEqual(item.current_observation, BASE)
    self.assertIn("changed again", item.detail)

  def test_stale_override(self):
    item = self.check(PreflightStatus.ERROR, plan=self.override_plan(BASE)).articles[0]
    self.assertIn("stale or unnecessary", item.detail)

  def test_pending_guard(self):
    self.ledger["pending_next_state"][OTHER] = {"operation": "CREATE"}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)
    self.check(PreflightStatus.ERROR, plan=PublishPlan(), calls=0)

  def test_missing_confirmed(self):
    self.ledger["articles"].clear()
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_confirmed(self):
    for value in (None, [], "invalid", {}, {**self.confirmed(SLUG), "state": "erased"}):
      with self.subTest(value=value):
        self.ledger["articles"][ID] = value
        self.write_ledger()
        self.check(PreflightStatus.ERROR, calls=0)

  def test_confirmed_invalid_fields(self):
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

  def test_plan_invalid_fields(self):
    for key, values in {
      "article_id": ("../invalid", ID.upper(), None),
      "state": (None, "hold", "erased"),
      "slug": (None, "short", SECOND, SLUG + "\n"),
      "release": (None, True, 0, 3, 5, "4", 4.0),
    }.items():
      for value in values:
        with self.subTest(key=key, value=value):
          entry = replace(self.plan.articles[0], **{key: value})
          self.check(PreflightStatus.ERROR, plan=PublishPlan((entry,)), calls=0)

  def test_invalid_override_schema(self):
    for value in ({}, [], "invalid", {"body_updated_at": "time"}):
      with self.subTest(value=value):
        self.check(PreflightStatus.ERROR, plan=self.override_plan(value), calls=0)

  def test_invalid_observation_timestamps(self):
    for key in BASE:
      for value in (None, "", True, 123):
        with self.subTest(key=key, value=value):
          bad = {**BASE, key: value}
          self.check(PreflightStatus.ERROR, plan=self.override_plan(bad), calls=0)
          self.ledger["articles"][ID]["successful_zenn_observation"] = bad
          self.write_ledger()
          self.check(PreflightStatus.ERROR, calls=0)
          self.ledger["articles"][ID]["successful_zenn_observation"] = dict(BASE)
          self.write_ledger()
          self.check(PreflightStatus.ERROR, response(bad))

  def test_all_remote_statuses_except_200_error(self):
    for status in (404, 201, 302, 401, 403, 408, 429, 500, True, "200", None):
      with self.subTest(status=status):
        self.check(PreflightStatus.ERROR, ArticleResponse(status))

  def test_owner_never_falls_back_to_username(self):
    for owner in (3, None, True, 0, -1, "2", 2.0):
      with self.subTest(owner=owner):
        self.check(PreflightStatus.ERROR, response(owner=owner))

  def test_invalid_remote_schema_and_slug(self):
    for payload in (None, [], {}, {"article": []}, {"article": {"slug": SLUG}},
                    {"article": {"slug": SLUG, "user": {"username": "same-name"}, **BASE}}):
      self.check(PreflightStatus.ERROR, ArticleResponse(200, payload))
    self.check(PreflightStatus.ERROR, response(slug=SECOND))
    self.check(PreflightStatus.ERROR, {"status": 200})

  def test_transport_failures(self):
    for failure in (OSError("network"), TimeoutError("timeout"),
                    ValueError("JSON"), RuntimeError("adapter")):
      self.check(PreflightStatus.ERROR, failure=failure)

  def multi_plan(self):
    self.ledger["articles"][OTHER] = self.confirmed(SECOND)
    self.write_ledger()
    return PublishPlan((*self.plan.articles,
                        UpdateArticlePlan(OTHER, "UPDATE", SECOND, "active", 4)))

  def test_multiple_all_ok(self):
    result = self.check(PreflightStatus.OK, plan=self.multi_plan(), calls=2,
                        failure=[response(), response(slug=SECOND)])
    self.assertTrue(all(item.baseline_observation == BASE for item in result.articles))

  def test_multiple_partial_failure(self):
    result = self.check(PreflightStatus.ERROR, plan=self.multi_plan(), calls=2,
                        failure=[response(), response(CURRENT, slug=SECOND)])
    self.assertEqual(result.articles[0].baseline_observation, BASE)
    self.assertIsNone(result.articles[1].baseline_observation)
    self.assertEqual(result.articles[1].article_id, OTHER)

  def test_transport_failure_does_not_skip_other_entries(self):
    result = self.check(PreflightStatus.ERROR, plan=self.multi_plan(), calls=2,
                        failure=[OSError("failure"), response(slug=SECOND)])
    self.assertEqual(result.articles[1].baseline_observation, BASE)

  def test_all_local_checks_before_any_http(self):
    plan = self.multi_plan()
    invalid = replace(plan.articles[1], release=5)
    self.check(PreflightStatus.ERROR,
               plan=PublishPlan((plan.articles[0], invalid)), calls=0)

  def test_duplicate_id_and_slug(self):
    self.check(PreflightStatus.ERROR,
               plan=PublishPlan((self.plan.articles[0], self.plan.articles[0])), calls=0)
    plan = self.multi_plan()
    self.ledger["articles"][OTHER]["slug"] = SLUG
    self.write_ledger()
    self.check(PreflightStatus.ERROR,
               plan=PublishPlan((plan.articles[0], replace(plan.articles[1], slug=SLUG))), calls=0)

  def test_other_confirmed_slug_overlap_outside_plan(self):
    self.ledger["articles"][OTHER] = self.confirmed(SLUG)
    self.write_ledger()
    result = self.check(PreflightStatus.ERROR, calls=0)
    self.assertIn(OTHER, result.articles[0].detail)

  def test_other_confirmed_non_dictionary_entry(self):
    for value in (None, [], "invalid", 1):
      with self.subTest(value=value):
        self.ledger["articles"][OTHER] = value
        self.write_ledger()
        result = self.check(PreflightStatus.ERROR, calls=0)
        self.assertIn(OTHER, result.articles[0].detail)

  def test_other_confirmed_missing_or_invalid_slug(self):
    for value in ({}, *({"slug": slug} for slug in (
        None, True, 123, [], "", "a" * 11, "a" * 51,
        "UPPERCASE-SLUG", "../unsafe-slug", SECOND + "\n"))):
      with self.subTest(value=value):
        self.ledger["articles"][OTHER] = value
        self.write_ledger()
        result = self.check(PreflightStatus.ERROR, calls=0)
        self.assertIn(OTHER, result.articles[0].detail)

  def test_valid_other_confirmed_article_outside_plan(self):
    self.ledger["articles"][OTHER] = self.confirmed(SECOND)
    self.write_ledger()
    result = self.check(PreflightStatus.OK)
    self.assertEqual(result.articles[0].baseline_observation, BASE)

  def test_unsupported_mixed_operations(self):
    for operation in ("CREATE", "DELETE", "RECREATE", "", None):
      self.check(PreflightStatus.ERROR,
                 plan=PublishPlan((*self.plan.articles, ArticlePlan(OTHER, operation, SECOND))),
                 calls=0)

  def test_empty_skips_config_and_remote(self):
    self.config.unlink()
    self.check(PreflightStatus.OK, plan=PublishPlan(), calls=0)

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

  def test_unsafe_local_paths(self):
    for path in (self.config, self.meta / "ledger.json"):
      content = path.read_bytes()
      target = self.root / "target.json"
      target.write_bytes(content)
      path.unlink()
      path.symlink_to(target)
      self.check(PreflightStatus.ERROR, calls=0)
      path.unlink()
      path.write_bytes(content)

  def test_baseline_independent_and_immutable(self):
    payload = response(CURRENT)
    original_override = dict(CURRENT)
    plan = self.override_plan(original_override)
    original_override["published_at"] = "mutated before call"
    item = self.check(PreflightStatus.OK, payload, plan=plan).articles[0]
    payload.payload["article"]["body_updated_at"] = "mutated after call"
    self.assertEqual(item.baseline_observation, CURRENT)
    with self.assertRaises(TypeError):
      item.baseline_observation["published_at"] = "bad"
    with self.assertRaises(TypeError):
      plan.articles[0].observation_override["published_at"] = "bad"
    with self.assertRaises(FrozenInstanceError):
      item.baseline_observation = None
    with self.assertRaises(FrozenInstanceError):
      plan.articles[0].release = 5

  def test_string_comparison_has_no_datetime_interpretation(self):
    self.ledger["articles"][ID]["successful_zenn_observation"] = dict(CURRENT)
    self.write_ledger()
    self.check(PreflightStatus.OK, response(CURRENT))
    alternate = {**CURRENT, "published_at": "2026-10-09T15:00:00Z"}
    self.check(PreflightStatus.ERROR, response(alternate))

  def test_structural_plan_with_extra_converter_fields(self):
    class ConverterPlan:
      candidate_content = "converter-owned"
      @property
      def articles(inner):
        return self.plan.articles
    self.check(PreflightStatus.OK, plan=ConverterPlan())

  def test_default_adapter_uses_mocked_http(self):
    before = self.tree()
    with patch("checker.zenn_http.urlopen", side_effect=OSError("mock")) as http:
      result = preflight_update(self.root, self.plan)
    self.assertEqual(result.result, PreflightStatus.ERROR)
    http.assert_called_once()
    self.assertEqual(self.tree(), before)

  def test_create_regression_interface(self):
    plan = PublishPlan((ArticlePlan(OTHER, "CREATE", SECOND),))
    before = self.tree()
    result = preflight_create(self.root, plan, Mock(return_value=ArticleResponse(404)))
    self.assertEqual(result.result, PreflightStatus.OK)
    self.assertFalse(hasattr(result.articles[0], "baseline_observation"))
    self.assertEqual(self.tree(), before)


if __name__ == "__main__":
  unittest.main()
