"""RECREATE preflight の旧 slug 優先、観測順序と不変性を検証します。"""

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from checker.create_observation import ArticleResponse
from checker.create_preflight import ArticlePlan, PublishPlan, PreflightStatus, preflight_create
from checker.delete_preflight import DeleteArticlePlan, preflight_delete
from checker.recreate_preflight import RecreateArticlePlan, preflight_recreate
from checker.update_preflight import UpdateArticlePlan, preflight_update
import test_create_preflight as fixtures
from test_create_preflight import ID, OTHER, SLUG, SECOND, response
from test_update_preflight import BASE, response as own_response

THIRD = "third-candidate-slug"
FOURTH = "fourth-candidate-slug"


class RecreatePreflightTests(unittest.TestCase):
  tree = fixtures.CreatePreflightTests.tree
  write_ledger = fixtures.CreatePreflightTests.write_ledger

  def setUp(self):
    fixtures.CreatePreflightTests.setUp(self)
    self.ledger["articles"][ID] = self.confirmed(SLUG)
    self.write_ledger()
    (self.meta / "released" / f"{ID}.md").write_bytes(b"snapshot\n")
    self.plan = PublishPlan((RecreateArticlePlan(ID, "RECREATE", SLUG, "active", 3),))

  def confirmed(self, slug):
    return {"state": "erased", "slug": slug, "successful_release": 3,
            "successful_zenn_observation": dict(BASE)}

  def changed_plan(self):
    return PublishPlan((replace(self.plan.articles[0], slug=SECOND),))

  def check(self, expected, responses=None, *, plan=None, requested=None):
    plan = self.plan if plan is None else plan
    responses = [ArticleResponse(404)] if responses is None else responses
    requested = [SLUG] if requested is None else requested
    before, original, entries = self.tree(), repr(plan), plan.articles
    fetch = Mock(side_effect=responses)
    result = preflight_recreate(self.root, plan, fetch)
    self.assertEqual(result.result, expected)
    self.assertEqual(fetch.call_args_list, [call(slug) for slug in requested])
    self.assertEqual(self.tree(), before)
    self.assertEqual(repr(plan), original)
    self.assertIs(plan.articles, entries)
    self.assertFalse(hasattr(result, "baseline_observation"))
    for item in result.articles:
      self.assertFalse(hasattr(item, "baseline_observation"))
      self.assertIn(item.result, tuple(PreflightStatus))
      if item.result != PreflightStatus.OK:
        self.assertTrue(item.detail)
    return result

  def test_same_slug_404_once(self):
    self.check(PreflightStatus.OK)

  def test_same_slug_other_user_collision_without_timestamps(self):
    self.check(PreflightStatus.COLLISION, [response()])

  def test_same_slug_own_user_error_without_timestamps(self):
    self.check(PreflightStatus.ERROR, [response(owner=2)])

  def test_same_slug_unexpected_status(self):
    for status in (201, 302, 401, 403, 408, 429, 500, True, "404", None):
      with self.subTest(status=status):
        self.check(PreflightStatus.ERROR, [ArticleResponse(status)])

  def test_old_available_rejects_changed_slug_without_new_request(self):
    item = self.check(PreflightStatus.ERROR, plan=self.changed_plan()).articles[0]
    self.assertIn("use S_old=" + SLUG, item.detail)
    self.assertIn("old slug", item.detail)
    self.assertIn(SECOND, item.detail)

  def test_old_own_user_short_circuits(self):
    self.check(PreflightStatus.ERROR, [response(owner=2)], plan=self.changed_plan())

  def test_changed_slug_ok_in_order(self):
    self.check(PreflightStatus.OK, [response(), ArticleResponse(404)],
               plan=self.changed_plan(), requested=[SLUG, SECOND])

  def test_changed_slug_collision(self):
    self.check(PreflightStatus.COLLISION, [response(), response(slug=SECOND)],
               plan=self.changed_plan(), requested=[SLUG, SECOND])

  def test_changed_slug_new_own_user_error(self):
    item = self.check(PreflightStatus.ERROR, [response(), response(owner=2, slug=SECOND)],
                      plan=self.changed_plan(), requested=[SLUG, SECOND]).articles[0]
    self.assertIn("candidate slug", item.detail)

  def malformed_responses(self, slug):
    return [ArticleResponse(200, payload) for payload in (
      None, [], {}, {"article": []}, {"article": {"slug": slug}},
      {"article": {"slug": slug, "user": {"username": "same-name"}}})] + [
      response(owner=owner, slug=slug) for owner in (None, True, False, 0, -1, "2", 2.0)] + [
      response(slug=THIRD), {"status": 404},
      OSError("network"), TimeoutError("timeout"), ValueError("JSON"), RuntimeError("adapter")]

  def test_old_invalid_response_short_circuits(self):
    for observed in self.malformed_responses(SLUG):
      with self.subTest(observed=observed):
        item = self.check(PreflightStatus.ERROR, [observed], plan=self.changed_plan()).articles[0]
        self.assertIn("old slug", item.detail)

  def test_old_unexpected_status_short_circuits(self):
    for status in (201, 302, 401, 403, 429, 500, True, "404", None):
      with self.subTest(status=status):
        self.check(PreflightStatus.ERROR, [ArticleResponse(status)], plan=self.changed_plan())

  def test_new_invalid_response(self):
    for observed in self.malformed_responses(SECOND):
      with self.subTest(observed=observed):
        self.check(PreflightStatus.ERROR, [response(), observed],
                   plan=self.changed_plan(), requested=[SLUG, SECOND])

  def test_new_unexpected_status(self):
    for status in (201, 302, 401, 403, 429, 500, True, "404", None):
      with self.subTest(status=status):
        self.check(PreflightStatus.ERROR, [response(), ArticleResponse(status)],
                   plan=self.changed_plan(), requested=[SLUG, SECOND])

  def test_same_slug_invalid_response(self):
    for observed in self.malformed_responses(SLUG):
      with self.subTest(observed=observed):
        self.check(PreflightStatus.ERROR, [observed])

  def test_pending_guard_including_empty(self):
    self.ledger["pending_next_state"][OTHER] = {"operation": "CREATE"}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, requested=[])
    self.check(PreflightStatus.ERROR, plan=PublishPlan(), requested=[])

  def test_missing_confirmed(self):
    self.ledger["articles"].clear()
    self.write_ledger()
    self.check(PreflightStatus.ERROR, requested=[])

  def test_malformed_or_wrong_state_confirmed(self):
    for value in (None, [], "invalid", {},
                  *({**self.confirmed(SLUG), "state": state} for state in ("active", "hold", None))):
      with self.subTest(value=value):
        self.ledger["articles"][ID] = value
        self.write_ledger()
        self.check(PreflightStatus.ERROR, requested=[])

  def test_invalid_confirmed_slug_release_observation(self):
    for key, values in {
      "slug": (None, True, "short", SLUG + "\n", "UPPERCASE-SLUG"),
      "successful_release": (None, True, 0, -1, "3", 3.0),
      "successful_zenn_observation": (None, {}, [], {**BASE, "published_at": ""}),
    }.items():
      for value in values:
        with self.subTest(key=key, value=value):
          self.ledger["articles"][ID] = {**self.confirmed(SLUG), key: value}
          self.write_ledger()
          self.check(PreflightStatus.ERROR, requested=[])

  def test_invalid_confirmed_observation_timestamps(self):
    for key in BASE:
      for value in (None, "", True, 123):
        with self.subTest(key=key, value=value):
          self.ledger["articles"][ID] = {
            **self.confirmed(SLUG), "successful_zenn_observation": {**BASE, key: value}}
          self.write_ledger()
          self.check(PreflightStatus.ERROR, requested=[])

  def test_invalid_plan_fields(self):
    for key, values in {
      "article_id": ("../invalid", ID.upper(), None),
      "state": (None, "hold", "erased"),
      "slug": (None, True, "short", SLUG + "\n", "../unsafe-slug"),
      "release": (None, True, 0, 2, 5, "3", 3.0),
    }.items():
      for value in values:
        with self.subTest(key=key, value=value):
          self.check(PreflightStatus.ERROR, plan=PublishPlan((
            replace(self.plan.articles[0], **{key: value}),)), requested=[])

  def test_release_n_and_n_plus_one(self):
    for release in (3, 4):
      with self.subTest(release=release):
        self.check(PreflightStatus.OK, plan=PublishPlan((
          replace(self.plan.articles[0], release=release),)))
        self.check(PreflightStatus.OK, [response(), ArticleResponse(404)], plan=PublishPlan((
          replace(self.plan.articles[0], release=release, slug=SECOND),)), requested=[SLUG, SECOND])

  def test_override_rejected_unless_none(self):
    entry = self.plan.articles[0]
    for value in (BASE, {}, [], "invalid", False, 0):
      with self.subTest(value=value):
        self.check(PreflightStatus.ERROR, plan=PublishPlan((
          SimpleNamespace(**vars(entry), observation_override=value),)), requested=[])
    self.check(PreflightStatus.OK, plan=PublishPlan((
      SimpleNamespace(**vars(entry), observation_override=None),)))

  def test_duplicate_ids(self):
    self.check(PreflightStatus.ERROR, plan=PublishPlan(self.plan.articles * 2), requested=[])

  def test_duplicate_candidate_slugs(self):
    self.ledger["articles"][OTHER] = self.confirmed(THIRD)
    self.write_ledger()
    entries = (replace(self.plan.articles[0], slug=SECOND),
               RecreateArticlePlan(OTHER, "RECREATE", SECOND, "active", 3))
    self.check(PreflightStatus.ERROR, plan=PublishPlan(entries), requested=[])

  def test_candidate_collision_with_other_confirmed(self):
    self.ledger["articles"][OTHER] = self.confirmed(SECOND)
    self.write_ledger()
    item = self.check(PreflightStatus.ERROR, plan=self.changed_plan(), requested=[]).articles[0]
    self.assertIn(OTHER, item.detail)

  def test_old_slug_collision_with_other_confirmed(self):
    self.ledger["articles"][OTHER] = self.confirmed(SLUG)
    self.write_ledger()
    self.check(PreflightStatus.ERROR, plan=self.changed_plan(), requested=[])

  def test_other_confirmed_malformed(self):
    for value in (None, [], "invalid", 1, {}, *({"slug": slug} for slug in (
        None, True, 123, [], "", "a" * 11, "a" * 51, "UPPERCASE-SLUG", SECOND + "\n"))):
      with self.subTest(value=value):
        self.ledger["articles"][OTHER] = value
        self.write_ledger()
        self.check(PreflightStatus.ERROR, requested=[])

  def multi_plan(self, changed=False):
    self.ledger["articles"][OTHER] = self.confirmed(THIRD)
    self.write_ledger()
    return PublishPlan((replace(self.plan.articles[0], slug=SECOND if changed else SLUG),
                        RecreateArticlePlan(OTHER, "RECREATE", FOURTH if changed else THIRD, "active", 4)))

  def test_multiple_all_ok(self):
    result = self.check(PreflightStatus.OK, [ArticleResponse(404), ArticleResponse(404)],
                        plan=self.multi_plan(), requested=[SLUG, THIRD])
    self.assertEqual(len(result.articles), 2)

  def test_multiple_changed_slugs_in_order(self):
    self.check(PreflightStatus.OK, [response(), ArticleResponse(404),
                                  response(slug=THIRD), ArticleResponse(404)],
               plan=self.multi_plan(changed=True), requested=[SLUG, SECOND, THIRD, FOURTH])

  def test_multiple_partial_collision(self):
    result = self.check(PreflightStatus.COLLISION, [ArticleResponse(404), response(slug=THIRD)],
                        plan=self.multi_plan(), requested=[SLUG, THIRD])
    self.assertEqual([item.result for item in result.articles],
                     [PreflightStatus.OK, PreflightStatus.COLLISION])

  def test_error_priority_over_collision_and_continue(self):
    plan = self.multi_plan()
    for responses in ([OSError("network"), response(slug=THIRD)],
                      [response(), OSError("network")]):
      with self.subTest(responses=responses):
        result = self.check(PreflightStatus.ERROR, responses, plan=plan, requested=[SLUG, THIRD])
        self.assertEqual({item.result for item in result.articles},
                         {PreflightStatus.ERROR, PreflightStatus.COLLISION})

  def test_old_short_circuit_continues_other_article(self):
    result = self.check(PreflightStatus.ERROR, [ArticleResponse(404), response(slug=THIRD), ArticleResponse(404)],
                        plan=self.multi_plan(changed=True), requested=[SLUG, THIRD, FOURTH])
    self.assertEqual([item.result for item in result.articles], [PreflightStatus.ERROR, PreflightStatus.OK])

  def test_all_local_checks_before_any_http(self):
    plan = self.multi_plan()
    self.check(PreflightStatus.ERROR, plan=PublishPlan((
      plan.articles[0], replace(plan.articles[1], release=5))), requested=[])
    result = self.check(PreflightStatus.ERROR, plan=PublishPlan(tuple(
      replace(entry, state="hold") for entry in plan.articles)), requested=[])
    self.assertEqual(len(result.articles), 2)

  def test_unsupported_mixed_operations(self):
    for operation in ("CREATE", "UPDATE", "DELETE", "", None):
      with self.subTest(operation=operation):
        self.check(PreflightStatus.ERROR, plan=PublishPlan((
          *self.plan.articles, ArticlePlan(OTHER, operation, SECOND))), requested=[])

  def test_empty_skips_config_and_http(self):
    self.config.unlink()
    result = self.check(PreflightStatus.OK, plan=PublishPlan(), requested=[])
    self.assertEqual(result.articles, ())

  def test_tuple_required(self):
    self.check(PreflightStatus.ERROR, plan=SimpleNamespace(articles=list(self.plan.articles)), requested=[])

  def test_invalid_or_missing_ledger(self):
    path = self.meta / "ledger.json"
    for content in ('[]', '{}', 'invalid', '{"articles":{},"pending_next_state":[]}'):
      path.write_text(content)
      self.check(PreflightStatus.ERROR, requested=[])
    path.unlink()
    self.check(PreflightStatus.ERROR, requested=[])

  def test_invalid_or_missing_config(self):
    for content in ('{}', '{"zenn_user":{"id":true}}', '{"zenn_user":{"id":"2"}}', 'invalid'):
      self.config.write_text(content)
      self.check(PreflightStatus.ERROR, requested=[])
    self.config.unlink()
    self.check(PreflightStatus.ERROR, requested=[])

  def test_symlink_local_paths(self):
    for path in (self.config, self.meta / "ledger.json"):
      content = path.read_bytes()
      target = self.root / "target.json"
      target.write_bytes(content)
      path.unlink()
      path.symlink_to(target)
      self.check(PreflightStatus.ERROR, requested=[])
      path.unlink()
      path.write_bytes(content)

  def test_frozen_models(self):
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles = ()
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles[0].slug = SECOND
    item = self.check(PreflightStatus.OK).articles[0]
    with self.assertRaises(FrozenInstanceError):
      item.result = PreflightStatus.ERROR

  def test_structural_converter_plan(self):
    class ConverterPlan:
      candidate_content = "converter-owned"
      @property
      def articles(inner):
        return self.plan.articles
    self.check(PreflightStatus.OK, plan=ConverterPlan())

  def test_snapshot_artifact_not_required_or_compared(self):
    (self.meta / "released" / f"{ID}.md").unlink()
    (self.root / "articles" / f"{SLUG}.md").unlink()
    self.check(PreflightStatus.OK)

  def test_default_adapter_mocked(self):
    before = self.tree()
    with patch("checker.zenn_http.urlopen", side_effect=OSError("mock")) as http:
      result = preflight_recreate(self.root, self.changed_plan())
    self.assertEqual(result.result, PreflightStatus.ERROR)
    http.assert_called_once()
    self.assertEqual(self.tree(), before)

  def test_create_update_delete_regression(self):
    self.ledger["articles"][ID]["state"] = "active"
    self.write_ledger()
    before = self.tree()
    created = preflight_create(self.root, PublishPlan((ArticlePlan(OTHER, "CREATE", SECOND),)),
                               Mock(return_value=ArticleResponse(404)))
    updated = preflight_update(self.root, PublishPlan((
      UpdateArticlePlan(ID, "UPDATE", SLUG, "active", 4),)), Mock(return_value=own_response()))
    deleted = preflight_delete(self.root, PublishPlan((
      DeleteArticlePlan(ID, "DELETE", SLUG, "erased", 3),)), Mock(return_value=ArticleResponse(404)))
    self.assertEqual([created.result, updated.result, deleted.result], [PreflightStatus.OK] * 3)
    self.assertEqual(updated.articles[0].baseline_observation, BASE)
    self.assertTrue(deleted.articles[0].warnings)
    self.assertEqual(self.tree(), before)


if __name__ == "__main__":
  unittest.main()
