"""隔離 repository で Converter と Unified Preflight の接続と非変更を検証します。"""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import json
import unittest
from unittest.mock import Mock, call, patch

from checker import unified_preflight
from checker.create_observation import ArticleResponse
from checker.create_preflight import PreflightResult, PreflightStatus
from converter import article_integration, entry_guards, image_planning, planning
from converter import preflight_integration as integration
from converter.entry_guards import CheckedInputs
from converter.models import ArticlePlanEntry, ArticlePlanningResult, ImagePlanEntry, ImagePlanningResult
from converter.repository_planning import RepositoryPlanningResult
import test_article_integration as fixtures
from test_article_integration import ID, OTHER, SLUG, NOW, OBSERVATION, source
from test_entry_guards import snapshot
from test_update_preflight import response
from test_create_preflight import response as other_response
from test_unified_preflight import DELETE_ID, RECREATE_ID, DELETE_SLUG, OLD_SLUG, NEW_SLUG

CURRENT = {**OBSERVATION, "body_updated_at": "changed"}
UPDATE_SLUG = "integration-update-slug"


class PreflightOrchestrationTests(unittest.TestCase):
  def setUp(self):
    self.root = Path("/explicit/article-root")
    self.deploy = Path("/verified/deploy-location")
    self.partial = RepositoryPlanningResult(
      CheckedInputs(self.root, self.deploy, (), ()),
      ArticlePlanningResult((ArticlePlanEntry(ID, "CREATE", SLUG, "active", 1, "candidate"),)),
      ImagePlanningResult((ImagePlanEntry("ADD", "images/a.png", b"validated"),)),
    )

  def test_calls_once_in_order_and_preserves_all_objects_for_every_status(self):
    for status in PreflightStatus:
      with self.subTest(status=status):
        events, captured = [], {}
        fetch = Mock()
        result = PreflightResult(status, diagnostics=("original diagnostic",))
        def local(root, *, now):
          self.assertIs(root, self.root)
          self.assertIs(now, NOW)
          events.append("local")
          return self.partial
        original = integration.finalize_publish_plan
        def finalize(partial):
          self.assertIs(partial, self.partial)
          events.append("finalize")
          captured["plan"] = original(partial)
          return captured["plan"]
        def remote(root, plan, injected):
          events.append("preflight")
          self.assertIs(root, self.deploy)
          self.assertIs(plan, captured["plan"])
          self.assertIs(injected, fetch)
          return result
        with patch.object(integration, "plan_repository", side_effect=local) as local_mock, patch.object(
            integration, "finalize_publish_plan", side_effect=finalize) as final_mock, patch.object(
            unified_preflight, "preflight_plan", side_effect=remote) as remote_mock:
          outcome = integration.preflight_publish(self.root, now=NOW, fetch_article=fetch)
        self.assertEqual(events, ["local", "finalize", "preflight"])
        for mock in (local_mock, final_mock, remote_mock):
          mock.assert_called_once()
        self.assertIs(outcome.deploy_root, self.deploy)
        self.assertIs(outcome.plan, captured["plan"])
        self.assertIs(outcome.preflight, result)
        self.assertIs(outcome.plan.articles, self.partial.article_plan.articles)
        self.assertIs(outcome.plan.images, self.partial.image_plan.images)
        self.assertEqual(outcome.preflight.result, status)
        for name in ("deploy_root", "plan", "preflight"):
          with self.assertRaises(FrozenInstanceError):
            setattr(outcome, name, None)
        self.assertEqual([field.name for field in fields(outcome)], ["deploy_root", "plan", "preflight"])

  def test_unexpected_checker_exception_propagates_without_outcome(self):
    error = RuntimeError("unexpected execution failure")
    with patch.object(integration, "plan_repository", return_value=self.partial), patch.object(
        unified_preflight, "preflight_plan", side_effect=error), patch.object(
        integration, "PreflightOutcome") as outcome:
      with self.assertRaises(RuntimeError) as caught:
        integration.preflight_publish(self.root, fetch_article=Mock())
    self.assertIs(caught.exception, error)
    outcome.assert_not_called()

  def test_local_exception_propagates_without_finalization_or_preflight(self):
    error = RuntimeError("local planning failure")
    fetch = Mock()
    with patch.object(integration, "plan_repository", side_effect=error), patch.object(
        integration, "finalize_publish_plan") as finalize, patch.object(
        unified_preflight, "preflight_plan") as checker, patch.object(integration, "PreflightOutcome") as outcome:
      with self.assertRaises(RuntimeError) as caught:
        integration.preflight_publish(self.root, fetch_article=fetch)
    self.assertIs(caught.exception, error)
    for mock in (finalize, checker, outcome, fetch):
      mock.assert_not_called()


class PreflightIntegrationTests(unittest.TestCase):
  commit = fixtures.ArticleIntegrationTests.commit
  save_ledger = fixtures.ArticleIntegrationTests.save_ledger
  write_source = fixtures.ArticleIntegrationTests.write_source
  confirm = fixtures.ArticleIntegrationTests.confirm

  def setUp(self):
    fixtures.ArticleIntegrationTests.setUp(self)
    self.config = self.management / "config.json"
    self.config.write_text(json.dumps({"zenn_user": {"id": 2, "username": "fixture"}}))
    self.commit(self.deploy)

  def check(self, status, *, responses=(), failure=None, requested=None):
    before = snapshot(self.root)
    captured = {}
    fetch = Mock(side_effect=failure if failure is not None else list(responses))
    original = unified_preflight.preflight_plan
    def checker(root, plan, injected):
      self.assertEqual(root, self.deploy)
      self.assertIs(injected, fetch)
      captured["plan"] = plan
      captured["representation"] = repr(plan)
      captured["result"] = original(root, plan, injected)
      return captured["result"]
    try:
      with patch.object(unified_preflight, "preflight_plan", side_effect=checker) as remote:
        outcome = integration.preflight_publish(self.root, now=NOW, fetch_article=fetch)
      remote.assert_called_once_with(outcome.deploy_root, outcome.plan, fetch)
      self.assertIs(outcome.plan, captured["plan"])
      self.assertEqual(repr(outcome.plan), captured["representation"])
      self.assertIs(outcome.preflight, captured["result"])
      self.assertEqual(outcome.preflight.result, status)
      for actual, original_item in zip(outcome.preflight.articles, captured["result"].articles, strict=True):
        self.assertIs(actual, original_item)
      if requested is not None:
        self.assertEqual(fetch.call_args_list, [call(slug) for slug in requested])
      else:
        self.assertEqual(fetch.call_args_list, [call(entry.slug) for entry in outcome.plan.articles])
      return outcome
    finally:
      self.assertEqual(snapshot(self.root), before)
      self.http_mock.assert_not_called()
      self.zenn_http_mock.assert_not_called()

  def fail_locally(self, pattern):
    before = snapshot(self.root)
    fetch = Mock()
    try:
      with patch.object(unified_preflight, "preflight_plan") as checker, patch.object(
          integration, "PreflightOutcome") as outcome, patch.object(
          integration, "finalize_publish_plan") as finalize:
        with self.assertRaisesRegex(ValueError, pattern):
          integration.preflight_publish(self.root, now=NOW, fetch_article=fetch)
        for mock in (checker, outcome, finalize, fetch):
          mock.assert_not_called()
    finally:
      self.assertEqual(snapshot(self.root), before)
      self.http_mock.assert_not_called()
      self.zenn_http_mock.assert_not_called()

  def prepare_update(self, override=None):
    self.confirm()
    markdown = source(release=4, body="Updated\n")
    if override is not None:
      markdown = markdown.replace("<!-- #release:4 -->\n",
        "<!-- #release:4 -->\n<!-- #observation-override:" + json.dumps(override) + " -->\n")
    self.write_source(markdown)

  def test_create_success(self):
    self.write_source()
    outcome = self.check(PreflightStatus.OK, responses=[ArticleResponse(404)])
    self.assertEqual(outcome.plan.articles[0].operation, "CREATE")

  def test_update_success_preserves_immutable_baseline(self):
    self.prepare_update()
    observed = response(OBSERVATION, slug=SLUG)
    outcome = self.check(PreflightStatus.OK, responses=[observed])
    item = outcome.preflight.articles[0]
    self.assertEqual(item.baseline_observation, OBSERVATION)
    self.assertIs(item.baseline_observation, item.current_observation)
    observed.payload["article"]["body_updated_at"] = "later mutation"
    self.assertEqual(item.baseline_observation, OBSERVATION)
    with self.assertRaises(TypeError):
      item.baseline_observation["published_at"] = "changed"
    self.assertFalse(hasattr(outcome.plan.articles[0], "baseline_observation"))

  def test_update_drift_retains_observation_diagnostics(self):
    self.prepare_update()
    item = self.check(PreflightStatus.ERROR, responses=[response(CURRENT, slug=SLUG)]).preflight.articles[0]
    self.assertIsNone(item.baseline_observation)
    self.assertEqual(item.ledger_observation, OBSERVATION)
    self.assertEqual(item.current_observation, CURRENT)
    self.assertIsNone(item.override_observation)
    self.assertEqual(json.loads(item.observation_override_json), CURRENT)
    self.assertIn("Remote drift", item.detail)

  def test_valid_override_keeps_plan_and_baseline(self):
    self.prepare_update(CURRENT)
    outcome = self.check(PreflightStatus.OK, responses=[response(CURRENT, slug=SLUG)])
    item = outcome.preflight.articles[0]
    self.assertEqual(item.baseline_observation, CURRENT)
    self.assertEqual(item.override_observation, CURRENT)
    self.assertEqual(item.ledger_observation, OBSERVATION)
    self.assertIs(item.baseline_observation, item.current_observation)
    self.assertEqual(outcome.plan.articles[0].observation_override, CURRENT)
    self.assertIsNone(item.observation_override_json)

  def test_mismatched_override_returns_error(self):
    self.prepare_update(CURRENT)
    item = self.check(PreflightStatus.ERROR, responses=[response(OBSERVATION, slug=SLUG)]).preflight.articles[0]
    self.assertIsNone(item.baseline_observation)
    self.assertEqual(item.override_observation, CURRENT)
    self.assertEqual(item.current_observation, OBSERVATION)
    self.assertIn("changed again", item.detail)

  def test_stale_override_returns_error(self):
    self.prepare_update(OBSERVATION)
    item = self.check(PreflightStatus.ERROR, responses=[response(OBSERVATION, slug=SLUG)]).preflight.articles[0]
    self.assertIsNone(item.baseline_observation)
    self.assertEqual(item.override_observation, OBSERVATION)
    self.assertIn("stale", item.detail)

  def test_invalid_override_fails_locally(self):
    self.prepare_update({"published_at": "invalid"})
    self.fail_locally("observation")

  def test_delete_already_absent_warning(self):
    self.confirm()
    self.write_source(source(state="erased", release=3))
    outcome = self.check(PreflightStatus.OK, responses=[ArticleResponse(404)])
    self.assertEqual(outcome.plan.articles[0].operation, "DELETE")
    self.assertTrue(outcome.preflight.articles[0].warnings)

  def test_recreate_changed_slug_result(self):
    self.confirm(state="erased")
    self.write_source(source(slug=NEW_SLUG, release=3))
    outcome = self.check(PreflightStatus.OK,
      responses=[other_response(slug=SLUG), ArticleResponse(404)], requested=[SLUG, NEW_SLUG])
    self.assertEqual((outcome.plan.articles[0].operation, outcome.plan.articles[0].slug,
                      outcome.plan.articles[0].release), ("RECREATE", NEW_SLUG, 3))

  def test_create_collision(self):
    self.write_source()
    item = self.check(PreflightStatus.COLLISION, responses=[other_response(slug=SLUG)]).preflight.articles[0]
    self.assertEqual(item.result, PreflightStatus.COLLISION)
    self.assertTrue(item.detail)

  def test_network_failure_returns_error(self):
    self.write_source()
    item = self.check(PreflightStatus.ERROR, failure=OSError("mocked transport failure")).preflight.articles[0]
    self.assertIn("mocked transport failure", item.detail)

  def test_checker_local_error_without_remote_observation(self):
    self.write_source()
    self.config.write_text("invalid JSON")
    self.commit(self.deploy)
    outcome = self.check(PreflightStatus.ERROR, requested=[])
    self.assertTrue(outcome.preflight.diagnostics)

  def test_dirty_deploy_prevents_preflight(self):
    (self.deploy / "dirty").write_text("dirty")
    self.fail_locally("Dirty deploy")

  def test_pending_prevents_preflight_including_empty_and_image_only(self):
    self.ledger["pending_next_state"][ID] = {"operation": "CREATE"}
    self.save_ledger()
    self.commit(self.deploy)
    self.fail_locally("pending")
    (self.root / "images/asset.png").write_bytes(b"new image")
    self.commit(self.root)
    self.fail_locally("pending")

  def test_article_validation_prevents_preflight(self):
    self.write_source(source(release=2))
    self.fail_locally("release")

  def test_article_global_validation_prevents_preflight(self):
    self.write_source()
    self.write_source(name="duplicate.md")
    self.fail_locally("Duplicate")

  def test_publication_date_validation_prevents_preflight(self):
    self.write_source(source(date="2099-01-01"))
    self.fail_locally("Future published_at")

  def test_image_validation_prevents_preflight(self):
    self.write_source()
    (self.root / "images/bad.svg").write_bytes(b"invalid")
    self.commit(self.root)
    self.fail_locally("extension")

  def check_empty(self):
    self.config.unlink()
    self.commit(self.deploy)
    with patch.object(unified_preflight, "load_configured_user_id",
                      side_effect=AssertionError("Unexpected owner lookup")) as owner:
      outcome = self.check(PreflightStatus.OK, requested=[])
    owner.assert_not_called()
    self.assertEqual(outcome.plan.articles, ())
    return outcome

  def test_image_only_invokes_checker_without_remote_or_owner_lookup(self):
    self.confirm()
    self.write_source(source(release=3))
    (self.root / "images/asset.png").write_bytes(b"new image")
    self.commit(self.root)
    outcome = self.check_empty()
    self.assertTrue(outcome.plan.has_image_changes)
    self.assertEqual(outcome.plan.images[0].content, b"new image")

  def test_noop_without_remote_or_owner_lookup(self):
    self.confirm()
    self.write_source(source(release=3))
    self.assertEqual(self.check_empty().plan.images, ())

  def test_hold_only_without_remote_or_owner_lookup(self):
    self.write_source(source(state="hold", slug=None, release=None), commit=False)
    self.assertEqual(self.check_empty().plan.images, ())

  def test_actual_local_stages_once_before_checker_and_entry_identity(self):
    self.write_source()
    (self.root / "images/asset.png").write_bytes(b"new image")
    self.commit(self.root)
    events, results = [], {}
    targets = (
      (integration, "plan_repository"),
      (article_integration, "inspect_inputs"),
      (entry_guards, "identify_worktrees"),
      (article_integration, "plan_articles"),
      (planning, "_classify"),
      (article_integration, "validate_publication_dates"),
      (image_planning, "_validate_images"),
      (image_planning, "_mirror_diff"),
      (integration, "finalize_publish_plan"),
      (unified_preflight, "preflight_plan"),
    )
    with ExitStack() as stack:
      mocks = []
      for module, name in targets:
        original = getattr(module, name)
        def invoke(*args, original=original, name=name, **kwargs):
          value = original(*args, **kwargs)
          results[name] = value
          events.append(name)
          return value
        mocks.append(stack.enter_context(patch.object(module, name, side_effect=invoke)))
      with patch("converter.repository_planning.plan_publish",
                 side_effect=AssertionError("Unexpected alternate planning")) as alternate:
        outcome = self.check(PreflightStatus.OK, responses=[ArticleResponse(404)])
    for mock in mocks:
      mock.assert_called_once()
    alternate.assert_not_called()
    self.assertEqual(events[-3:], ["plan_repository", "finalize_publish_plan", "preflight_plan"])
    self.assertLess(events.index("validate_publication_dates"), events.index("_validate_images"))
    self.assertIs(outcome.deploy_root, results["inspect_inputs"].deploy_root)
    self.assertIs(outcome.plan.articles, results["plan_articles"].articles)
    self.assertIs(outcome.plan.images, results["_mirror_diff"].images)
    self.assertIs(outcome.plan, results["finalize_publish_plan"])
    self.assertIs(outcome.preflight, results["preflight_plan"])

  def prepare_mixed(self):
    self.write_source(name="01-create.md")
    self.confirm(article_id=OTHER, slug=UPDATE_SLUG)
    self.write_source(source(OTHER, slug=UPDATE_SLUG, release=4, body="Updated\n"), name="02-update.md")
    self.confirm(article_id=DELETE_ID, slug=DELETE_SLUG)
    self.write_source(source(DELETE_ID, state="erased", slug=DELETE_SLUG, release=3), name="03-delete.md")
    self.confirm(article_id=RECREATE_ID, slug=OLD_SLUG, state="erased")
    self.write_source(source(RECREATE_ID, slug=OLD_SLUG, release=3), name="04-recreate.md")

  def check_mixed(self, first, status):
    self.prepare_mixed()
    outcome = self.check(status, responses=[first, response(OBSERVATION, slug=UPDATE_SLUG),
                                            ArticleResponse(404), ArticleResponse(404)])
    self.assertEqual([entry.operation for entry in outcome.plan.articles],
                     ["CREATE", "UPDATE", "DELETE", "RECREATE"])
    self.assertEqual([item.article_id for item in outcome.preflight.articles],
                     [entry.article_id for entry in outcome.plan.articles])
    self.assertEqual(outcome.preflight.articles[1].result, PreflightStatus.OK)
    self.assertEqual(outcome.preflight.articles[1].baseline_observation, OBSERVATION)
    self.assertEqual(outcome.preflight.result, status)
    return outcome

  def test_mixed_all_ok_keeps_update_baseline(self):
    self.check_mixed(ArticleResponse(404), PreflightStatus.OK)

  def test_mixed_collision_keeps_update_baseline_without_authorizing_plan(self):
    outcome = self.check_mixed(other_response(slug=SLUG), PreflightStatus.COLLISION)
    self.assertNotEqual(outcome.preflight.result, PreflightStatus.OK)
    self.assertEqual(outcome.preflight.articles[0].result, PreflightStatus.COLLISION)

  def test_mixed_error_keeps_successful_update_baseline(self):
    self.check_mixed(ArticleResponse(500), PreflightStatus.ERROR)


if __name__ == "__main__":
  unittest.main()
