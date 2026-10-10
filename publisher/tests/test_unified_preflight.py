"""混在 preflight の全件 local guard、結果保持と不変性を検証します。"""

from dataclasses import FrozenInstanceError, replace
import itertools
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from checker import create_preflight, update_preflight, delete_preflight, recreate_preflight
from checker.create_observation import ArticleResponse
from checker.create_preflight import ArticlePlan, PublishPlan, PreflightStatus
from checker.delete_preflight import DeleteArticlePlan
from checker.recreate_preflight import RecreateArticlePlan
from checker.update_preflight import UpdateArticlePlan
from checker.unified_preflight import preflight_plan
import test_create_preflight as fixtures
from test_create_preflight import ID, OTHER, SLUG, SECOND, response as other_response
from test_update_preflight import BASE, CURRENT, response

DELETE_ID = "9c8daef1-b196-4a6f-a213-000000000001"
RECREATE_ID = "9c8daef1-b196-4a6f-a213-000000000002"
DELETE_SLUG = "delete-article-slug"
OLD_SLUG = "recreate-old-slug"
NEW_SLUG = "recreate-new-slug"


class UnifiedPreflightTests(unittest.TestCase):
  tree = fixtures.CreatePreflightTests.tree
  write_ledger = fixtures.CreatePreflightTests.write_ledger

  def setUp(self):
    fixtures.CreatePreflightTests.setUp(self)
    self.ledger["articles"] = {
      OTHER: self.confirmed(SECOND, "active"),
      DELETE_ID: self.confirmed(DELETE_SLUG, "active"),
      RECREATE_ID: self.confirmed(OLD_SLUG, "erased"),
    }
    self.write_ledger()
    self.plan = PublishPlan((
      ArticlePlan(ID, "CREATE", SLUG),
      UpdateArticlePlan(OTHER, "UPDATE", SECOND, "active", 4),
      DeleteArticlePlan(DELETE_ID, "DELETE", DELETE_SLUG, "erased", 3),
      RecreateArticlePlan(RECREATE_ID, "RECREATE", OLD_SLUG, "active", 3),
    ))

  def confirmed(self, slug, state):
    return {"state": state, "slug": slug, "successful_release": 3,
            "successful_zenn_observation": dict(BASE)}

  def ok_responses(self):
    return [ArticleResponse(404), response(slug=SECOND), ArticleResponse(404), ArticleResponse(404)]

  def check(self, expected, *, plan=None, responses=None, requested=None):
    plan = self.plan if plan is None else plan
    before, original, entries = self.tree(), repr(plan), plan.articles
    identities = tuple(id(entry) for entry in entries)
    fetch = Mock(side_effect=self.ok_responses() if responses is None else responses)
    result = preflight_plan(self.root, plan, fetch)
    self.assertEqual(result.result, expected)
    self.assertEqual(self.tree(), before)
    self.assertEqual(repr(plan), original)
    self.assertIs(plan.articles, entries)
    self.assertEqual(tuple(id(entry) for entry in plan.articles), identities)
    if requested is not None:
      self.assertEqual(fetch.call_args_list, [call(slug) for slug in requested])
    else:
      self.assertEqual(fetch.call_args_list, [call(entry.slug) for entry in entries])
    if expected != PreflightStatus.ERROR or fetch.call_count:
      self.assertEqual([item.article_id for item in result.articles],
                       [entry.article_id for entry in entries])
    return result

  def test_mixed_all_ok_preserves_baseline_warning(self):
    result = self.check(PreflightStatus.OK)
    self.assertEqual(result.articles[1].baseline_observation, BASE)
    self.assertTrue(result.articles[2].warnings)
    with self.assertRaises(TypeError):
      result.articles[1].baseline_observation["published_at"] = "bad"

  def test_late_local_failure_blocks_all_http(self):
    for index in range(4):
      with self.subTest(index=index):
        entries = list(self.plan.articles)
        entries[index] = replace(entries[index], operation="invalid")
        self.check(PreflightStatus.ERROR, plan=PublishPlan(tuple(entries)), requested=[])
    entries = (*self.plan.articles[:3], replace(self.plan.articles[3], release=5))
    self.check(PreflightStatus.ERROR, plan=PublishPlan(entries), requested=[])

  def test_each_operation_specific_local_validation(self):
    invalid = (
      replace(self.plan.articles[0], slug="short"),
      replace(self.plan.articles[1], release=3),
      replace(self.plan.articles[2], state="active"),
      replace(self.plan.articles[3], state="erased"),
    )
    for index, entry in enumerate(invalid):
      entries = list(self.plan.articles)
      entries[index] = entry
      self.check(PreflightStatus.ERROR, plan=PublishPlan(tuple(entries)), requested=[])

  def test_pending_guard_including_empty(self):
    self.ledger["pending_next_state"][ID] = {"operation": "CREATE"}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, requested=[])
    self.check(PreflightStatus.ERROR, plan=PublishPlan(), requested=[])

  def test_invalid_or_missing_ledger(self):
    path = self.meta / "ledger.json"
    for content in ('invalid', '[]', '{}', '{"articles":[],"pending_next_state":{}}'):
      path.write_text(content)
      self.check(PreflightStatus.ERROR, requested=[])
    path.unlink()
    self.check(PreflightStatus.ERROR, requested=[])

  def test_invalid_or_missing_config(self):
    for owner in (None, True, "2", 0, -1, 2.0):
      self.config.write_text(json.dumps({"zenn_user": {"id": owner, "username": "same-name"}}))
      self.check(PreflightStatus.ERROR, requested=[])
    self.config.unlink()
    self.check(PreflightStatus.ERROR, requested=[])

  def test_invalid_operation_identity_and_tuple(self):
    for operation in (None, "", "OTHER", [], 1):
      self.check(PreflightStatus.ERROR, plan=PublishPlan((
        *self.plan.articles, ArticlePlan(ID, operation, SLUG))), requested=[])
    for article_id in (ID.upper(), None, "../invalid"):
      self.check(PreflightStatus.ERROR, plan=PublishPlan((
        replace(self.plan.articles[0], article_id=article_id), *self.plan.articles[1:])), requested=[])
    self.check(PreflightStatus.ERROR,
               plan=SimpleNamespace(articles=list(self.plan.articles)), requested=[])

  def test_cross_operation_duplicate_ids_all_pairs(self):
    for left, right in itertools.combinations(self.plan.articles, 2):
      with self.subTest(operations=(left.operation, right.operation)):
        result = self.check(PreflightStatus.ERROR,
          plan=PublishPlan((left, replace(right, article_id=left.article_id))), requested=[])
        self.assertTrue(any("Duplicate plan article ID" in item.detail for item in result.articles))

  def test_cross_operation_duplicate_slugs_all_pairs(self):
    for left, right in itertools.combinations(self.plan.articles, 2):
      with self.subTest(operations=(left.operation, right.operation)):
        result = self.check(PreflightStatus.ERROR,
          plan=PublishPlan((left, replace(right, slug=left.slug))), requested=[])
        self.assertTrue(any("Duplicate plan candidate slug" in item.detail for item in result.articles))

  def test_create_confirmed_slug_collision(self):
    self.check(PreflightStatus.ERROR, plan=PublishPlan((
      replace(self.plan.articles[0], slug=SECOND),)), requested=[])

  def test_delete_does_not_free_confirmed_slug(self):
    self.check(PreflightStatus.ERROR, plan=PublishPlan((self.plan.articles[2],
      replace(self.plan.articles[0], slug=DELETE_SLUG))), requested=[])

  def test_recreate_old_and_new_confirmed_collision(self):
    for slug in (SECOND, DELETE_SLUG):
      self.check(PreflightStatus.ERROR, plan=PublishPlan((
        replace(self.plan.articles[3], slug=slug),)), requested=[])
    self.ledger["articles"][DELETE_ID]["slug"] = OLD_SLUG
    self.write_ledger()
    self.check(PreflightStatus.ERROR, plan=PublishPlan((
      replace(self.plan.articles[3], slug=NEW_SLUG),)), requested=[])

  def test_other_confirmed_malformed(self):
    for value in (None, [], {}, {"slug": "short"}):
      self.ledger["articles"][DELETE_ID] = value
      self.write_ledger()
      self.check(PreflightStatus.ERROR, requested=[])

  def test_nonupdate_override_rejected(self):
    for index in (0, 2, 3):
      for override in ({}, BASE, False, 0, "invalid"):
        entries = list(self.plan.articles)
        entries[index] = SimpleNamespace(**vars(entries[index]), observation_override=override)
        self.check(PreflightStatus.ERROR, plan=PublishPlan(tuple(entries)), requested=[])

  def test_empty_skips_owner_and_http(self):
    self.config.unlink()
    with patch("checker.unified_preflight.load_configured_user_id") as load:
      self.check(PreflightStatus.OK, plan=PublishPlan(), requested=[])
    load.assert_not_called()

  def test_create_collision_update_ok(self):
    plan = PublishPlan(self.plan.articles[:2])
    result = self.check(PreflightStatus.COLLISION, plan=plan,
                        responses=[other_response(), response(slug=SECOND)])
    self.assertEqual(result.articles[1].baseline_observation, BASE)

  def test_collision_drift_error_and_json(self):
    result = self.check(PreflightStatus.ERROR, plan=PublishPlan(self.plan.articles[:2]),
                        responses=[other_response(), response(CURRENT, slug=SECOND)])
    self.assertEqual(result.articles[0].result, PreflightStatus.COLLISION)
    self.assertEqual(json.loads(result.articles[1].observation_override_json), CURRENT)
    self.assertEqual(result.articles[1].ledger_observation, BASE)

  def test_create_ok_delete_warning(self):
    result = self.check(PreflightStatus.OK, plan=PublishPlan((self.plan.articles[0], self.plan.articles[2])),
                        responses=[ArticleResponse(404), ArticleResponse(404)])
    self.assertTrue(result.articles[1].warnings)

  def test_delete_other_user_warning_retained_with_error(self):
    responses = self.ok_responses()
    responses[0] = OSError("network")
    responses[2] = other_response(slug=DELETE_SLUG)
    result = self.check(PreflightStatus.ERROR, responses=responses)
    self.assertTrue(result.articles[2].warnings)
    self.assertEqual(result.articles[1].baseline_observation, BASE)

  def test_recreate_changed_slug_order(self):
    plan = PublishPlan((*self.plan.articles[:3], replace(self.plan.articles[3], slug=NEW_SLUG)))
    self.check(PreflightStatus.OK, plan=plan,
               responses=[*self.ok_responses()[:3], other_response(slug=OLD_SLUG), ArticleResponse(404)],
               requested=[SLUG, SECOND, DELETE_SLUG, OLD_SLUG, NEW_SLUG])

  def test_recreate_old_available_short_circuit_and_continue(self):
    plan = PublishPlan((replace(self.plan.articles[3], slug=NEW_SLUG), self.plan.articles[0]))
    result = self.check(PreflightStatus.ERROR, plan=plan, responses=[ArticleResponse(404), ArticleResponse(404)],
                        requested=[OLD_SLUG, SLUG])
    self.assertIn("use S_old=" + OLD_SLUG, result.articles[0].detail)
    self.assertEqual(result.articles[1].result, PreflightStatus.OK)

  def test_recreate_collision_other_error(self):
    plan = PublishPlan((self.plan.articles[3], self.plan.articles[0]))
    self.check(PreflightStatus.ERROR, plan=plan,
               responses=[other_response(slug=OLD_SLUG), OSError("network")])

  def test_multiple_remote_errors_retained_in_order(self):
    result = self.check(PreflightStatus.ERROR, responses=[OSError("one"), OSError("two"),
                                                        OSError("three"), OSError("four")])
    self.assertEqual(len(result.articles), 4)
    self.assertTrue(all(item.result == PreflightStatus.ERROR and item.detail for item in result.articles))

  def test_update_override_valid_mismatch_stale_in_mixed_plan(self):
    for override, current, status in ((CURRENT, CURRENT, PreflightStatus.OK),
                                      (CURRENT, BASE, PreflightStatus.ERROR),
                                      (BASE, BASE, PreflightStatus.ERROR)):
      with self.subTest(override=override, current=current):
        plan = PublishPlan((self.plan.articles[0], replace(self.plan.articles[1], observation_override=override),
                            *self.plan.articles[2:]))
        responses = self.ok_responses()
        responses[1] = response(current, slug=SECOND)
        result = self.check(status, plan=plan, responses=responses)
        self.assertEqual(result.articles[1].override_observation, override)
        self.assertEqual(result.articles[1].current_observation, current)
        self.assertEqual(result.articles[1].baseline_observation, current if status == PreflightStatus.OK else None)

  def test_single_operation_matches_existing(self):
    functions = (create_preflight.preflight_create, update_preflight.preflight_update,
                 delete_preflight.preflight_delete, recreate_preflight.preflight_recreate)
    for entry, function in zip(self.plan.articles, functions):
      for observed in (ArticleResponse(404), response(slug=entry.slug),
                       other_response(slug=entry.slug), ArticleResponse(500), ArticleResponse(200, {})):
        with self.subTest(operation=entry.operation, observed=observed):
          plan = PublishPlan((entry,))
          expected = function(self.root, plan, Mock(return_value=observed))
          actual = self.check(expected.result, plan=plan, responses=[observed])
          self.assertEqual(actual, expected)

  def test_same_entry_and_result_objects_used_for_dispatch(self):
    plan = PublishPlan((self.plan.articles[1],))
    original = update_preflight.preflight_update
    captured = []
    def dispatch(root, view, fetch):
      self.assertIs(view.articles[0], plan.articles[0])
      result = original(root, view, fetch)
      captured.append(result.articles[0])
      return result
    with patch("checker.update_preflight.preflight_update", side_effect=dispatch):
      result = self.check(PreflightStatus.OK, plan=plan, responses=[response(slug=SECOND)])
    self.assertIs(result.articles[0], captured[0])

  def test_all_validators_complete_before_first_http(self):
    events = []
    modules = (create_preflight, update_preflight, delete_preflight, recreate_preflight)
    from contextlib import ExitStack
    with ExitStack() as stack:
      for module in modules:
        original = module._validate_entry
        def validate(entry, ledger, original=original):
          events.append(entry.operation)
          return original(entry, ledger)
        stack.enter_context(patch.object(module, "_validate_entry", side_effect=validate))
      answers = iter(self.ok_responses())
      def fetch(slug):
        self.assertTrue(all(operation in events for operation in ("CREATE", "UPDATE", "DELETE", "RECREATE")))
        return next(answers)
      result = preflight_plan(self.root, self.plan, fetch)
    self.assertEqual(result.result, PreflightStatus.OK)

  def test_frozen_plan_and_result(self):
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles = ()
    for entry in self.plan.articles:
      with self.assertRaises(FrozenInstanceError):
        entry.operation = "DELETE"

  def test_structural_plan(self):
    self.check(PreflightStatus.OK, plan=SimpleNamespace(articles=self.plan.articles, images_changed=True))

  def test_invalid_response_and_transport_fail_closed(self):
    for observed in ({"status": 404}, ArticleResponse(True), ArticleResponse(200, {}),
                     other_response(owner="2"), RuntimeError("transport")):
      self.check(PreflightStatus.ERROR, plan=PublishPlan((self.plan.articles[0],)), responses=[observed])

  def test_default_adapter_mocked(self):
    before = self.tree()
    with patch("checker.zenn_http.urlopen", side_effect=OSError("mock")) as http:
      result = preflight_plan(self.root, self.plan)
    self.assertEqual(result.result, PreflightStatus.ERROR)
    self.assertEqual(http.call_count, 4)
    self.assertEqual(self.tree(), before)

  def test_dispatch_diagnostics_without_article_result(self):
    from checker.create_preflight import PreflightResult
    with patch("checker.create_preflight.preflight_create", return_value=PreflightResult(
        PreflightStatus.ERROR, diagnostics=("local read failed",))):
      result = self.check(PreflightStatus.ERROR, plan=PublishPlan((self.plan.articles[0],)), requested=[])
    self.assertEqual(result.diagnostics, ("local read failed",))
    self.assertEqual(result.articles[0].article_id, ID)


if __name__ == "__main__":
  unittest.main()
