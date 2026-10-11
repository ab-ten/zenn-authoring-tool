"""最終計画の不変性、検証順序と既存 Checker 契約への適合を検証します。"""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from checker.create_preflight import PublishPlanInput, PreflightStatus
from checker.unified_preflight import preflight_plan
from converter import article_integration, image_planning, planning, repository_planning
from converter.entry_guards import CheckedInputs
from converter.models import (
  ArticlePlanEntry, ArticlePlanningResult, ImagePlanEntry, ImagePlanningResult, PublishPlan,
)
from converter.repository_planning import RepositoryPlanningResult, finalize_publish_plan, plan_publish
import test_article_integration as fixtures
from test_article_integration import ID, SLUG, NOW, OBSERVATION, source
from test_entry_guards import snapshot
import test_unified_preflight as checker_fixtures


class PublishPlanModelTests(unittest.TestCase):
  def setUp(self):
    self.article = ArticlePlanEntry(ID, "CREATE", SLUG, "active", 1, "content")
    self.image = ImagePlanEntry("ADD", "images/asset.png", b"image")

  def test_empty_article_image_and_mixed(self):
    for articles, images in (((), ()), ((self.article,), ()),
                             ((), (self.image,)), ((self.article,), (self.image,))):
      with self.subTest(articles=articles, images=images):
        plan = PublishPlan(articles, images)
        self.assertIs(plan.articles, articles)
        self.assertIs(plan.images, images)
        self.assertEqual(plan.has_image_changes, bool(images))
        if articles:
          self.assertIs(plan.articles[0], self.article)
        if images:
          self.assertIs(plan.images[0], self.image)

  def test_frozen_fields_and_entries(self):
    plan = PublishPlan((self.article,), (self.image,))
    for name in ("articles", "images", "has_image_changes"):
      with self.subTest(name=name), self.assertRaises(FrozenInstanceError):
        setattr(plan, name, ())
    for entry, name in ((self.article, "release"), (self.image, "content")):
      with self.assertRaises(FrozenInstanceError):
        setattr(entry, name, None)
    self.assertEqual([field.name for field in fields(plan)], ["articles", "images"])

  def test_tuple_and_entry_types(self):
    for name, entry in (("articles", self.article), ("images", self.image)):
      for invalid in ([], [entry], {}, None, iter((entry,)), ({},), ([],),
                      (object(),), (self.image if name == "articles" else self.article,)):
        with self.subTest(name=name, invalid=invalid), self.assertRaises(TypeError):
          PublishPlan(**{name: invalid})

  def test_order_and_image_bytes_are_preserved(self):
    content = bytes(bytearray(b"validated"))
    images = (ImagePlanEntry("UPDATE", "images/z.png", content),
              ImagePlanEntry("DELETE", "images/a.png"))
    plan = PublishPlan((self.article,), images)
    self.assertIs(plan.images, images)
    self.assertIs(plan.images[0].content, content)
    self.assertIsNone(plan.images[1].content)
    with self.assertRaises(TypeError):
      plan.images[0].content[0] = 0

  def test_override_is_an_isolated_immutable_snapshot(self):
    override = dict(OBSERVATION)
    article = ArticlePlanEntry(ID, "UPDATE", SLUG, "active", 4, "updated", override)
    plan = PublishPlan((article,))
    override.clear()
    self.assertIs(plan.articles[0], article)
    self.assertEqual(dict(article.observation_override), OBSERVATION)
    with self.assertRaises(TypeError):
      article.observation_override["published_at"] = "changed"

  def test_delete_and_recreate_fields_are_preserved(self):
    articles = (ArticlePlanEntry(ID, "DELETE", SLUG, "erased", 3),
                ArticlePlanEntry(ID, "RECREATE", "selected-new-slug", "active", 4, "candidate"))
    plan = PublishPlan(articles)
    self.assertIs(plan.articles, articles)
    self.assertIsNone(plan.articles[0].candidate_content)
    self.assertEqual((plan.articles[1].slug, plan.articles[1].release),
                     ("selected-new-slug", 4))


class FinalizationTests(unittest.TestCase):
  def test_finalization_only_retains_original_tuples_without_io_or_replanning(self):
    inputs = CheckedInputs(Path("/explicit"), Path("/explicit/_deploy"), (), ())
    article = ArticlePlanEntry(ID, "CREATE", SLUG, "active", 1, "candidate")
    images = ImagePlanningResult((ImagePlanEntry("ADD", "images/a.png", b"validated"),))
    partial = RepositoryPlanningResult(inputs, ArticlePlanningResult((article,)), images)
    before = repr(partial)
    forbidden = (
      "converter.planning._classify", "converter.planning.plan_articles",
      "converter.source_parser.parse_source", "converter.image_planning._validate_images",
      "converter.image_planning._mirror_diff", "converter.repository_planning.plan_images",
      "converter.repository_planning.plan_repository_articles", "builtins.open",
      "pathlib.Path.open", "pathlib.Path.stat", "pathlib.Path.lstat", "os.open",
      "subprocess.run", "urllib.request.urlopen", "checker.zenn_http.urlopen",
    )
    with ExitStack() as stack:
      mocks = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
               for target in forbidden]
      plan = finalize_publish_plan(partial)
    for mock in mocks:
      mock.assert_not_called()
    self.assertIs(plan.articles, partial.article_plan.articles)
    self.assertIs(plan.images, partial.image_plan.images)
    self.assertIs(plan.articles[0], article)
    self.assertIs(plan.images[0].content, images.images[0].content)
    self.assertEqual(repr(partial), before)
    self.assertEqual(vars(plan), {"articles": partial.article_plan.articles, "images": images.images})


class PublishPlanningIntegrationTests(unittest.TestCase):
  setUp = fixtures.ArticleIntegrationTests.setUp
  commit = fixtures.ArticleIntegrationTests.commit
  save_ledger = fixtures.ArticleIntegrationTests.save_ledger
  write_source = fixtures.ArticleIntegrationTests.write_source
  confirm = fixtures.ArticleIntegrationTests.confirm

  def plan(self):
    before = snapshot(self.root)
    try:
      return plan_publish(self.root, now=NOW)
    finally:
      self.assertEqual(snapshot(self.root), before)
      self.http_mock.assert_not_called()
      self.zenn_http_mock.assert_not_called()

  def change_image(self):
    (self.root / "images/asset.png").write_bytes(b"new validated bytes")
    self.commit(self.root)

  def assert_no_final_plan(self, pattern):
    with patch.object(repository_planning, "PublishPlan", wraps=PublishPlan) as model, patch.object(
        repository_planning, "finalize_publish_plan", wraps=finalize_publish_plan) as finalize:
      with self.assertRaisesRegex(ValueError, pattern):
        self.plan()
      model.assert_not_called()
      finalize.assert_not_called()

  def test_article_only(self):
    self.write_source()
    plan = self.plan()
    self.assertEqual([entry.operation for entry in plan.articles], ["CREATE"])
    self.assertEqual(plan.images, ())

  def test_image_only(self):
    self.change_image()
    plan = self.plan()
    self.assertEqual(plan.articles, ())
    self.assertEqual([(entry.operation, entry.content) for entry in plan.images],
                     [("UPDATE", b"new validated bytes")])
    self.assertTrue(plan.has_image_changes)

  def test_mixed(self):
    self.write_source()
    self.change_image()
    plan = self.plan()
    self.assertEqual(len(plan.articles), 1)
    self.assertEqual(len(plan.images), 1)

  def test_noop(self):
    self.confirm()
    self.write_source(source(release=3))
    self.assertEqual(self.plan(), PublishPlan())

  def test_hold_only(self):
    self.write_source(source(state="hold", slug=None, release=None), commit=False)
    self.assertEqual(self.plan(), PublishPlan())

  def test_article_validation_failure(self):
    self.write_source(source(release=2))
    self.assert_no_final_plan("release")

  def test_publication_date_failure(self):
    self.write_source(source(date="2099-01-01"))
    self.assert_no_final_plan("Future published_at")

  def test_global_article_validation_failure(self):
    self.write_source()
    self.write_source(name="duplicate.md")
    self.assert_no_final_plan("Duplicate")

  def test_image_validation_failure(self):
    self.write_source()
    (self.root / "images/bad.svg").write_bytes(b"invalid")
    self.commit(self.root)
    self.assert_no_final_plan("extension")

  def test_dirty_deploy(self):
    (self.deploy / "dirty").write_text("dirty")
    self.assert_no_final_plan("Dirty deploy")

  def test_pending(self):
    self.ledger["pending_next_state"][ID] = {"operation": "CREATE"}
    self.save_ledger()
    self.commit(self.deploy)
    self.assert_no_final_plan("pending")

  def test_each_stage_executes_once_and_finalizes_last(self):
    self.write_source()
    self.change_image()
    targets = (
      (repository_planning, "plan_repository"),
      (repository_planning, "plan_repository_articles"),
      (article_integration, "inspect_inputs"),
      (article_integration, "parse_source"),
      (article_integration, "plan_articles"),
      (planning, "_classify"),
      (article_integration, "validate_publication_dates"),
      (repository_planning, "plan_images"),
      (image_planning, "_validate_images"),
      (image_planning, "_mirror_diff"),
      (repository_planning, "finalize_publish_plan"),
    )
    events, results = [], {}
    with ExitStack() as stack:
      mocks = []
      for module, name in targets:
        original = getattr(module, name)
        def invoke(*args, original=original, name=name, **kwargs):
          result = original(*args, **kwargs)
          events.append(name)
          results[name] = result
          return result
        mocks.append(stack.enter_context(patch.object(module, name, side_effect=invoke)))
      model = stack.enter_context(patch.object(repository_planning, "PublishPlan", wraps=PublishPlan))
      plan = self.plan()
    for mock in mocks:
      mock.assert_called_once()
    model.assert_called_once()
    mocks[0].assert_called_once_with(self.root, now=NOW)
    self.assertEqual(events[-1], "finalize_publish_plan")
    self.assertLess(events.index("validate_publication_dates"), events.index("_validate_images"))
    self.assertLess(events.index("_validate_images"), events.index("_mirror_diff"))
    self.assertIs(plan.articles, results["plan_articles"].articles)
    self.assertIs(plan.images, results["_mirror_diff"].images)


class CheckerCompatibilityTests(unittest.TestCase):
  setUp = checker_fixtures.UnifiedPreflightTests.setUp
  confirmed = checker_fixtures.UnifiedPreflightTests.confirmed
  tree = checker_fixtures.UnifiedPreflightTests.tree
  write_ledger = checker_fixtures.UnifiedPreflightTests.write_ledger
  ok_responses = checker_fixtures.UnifiedPreflightTests.ok_responses

  def test_original_converter_entries_pass_all_operation_dispatches(self):
    articles = tuple(ArticlePlanEntry(
      entry.article_id, entry.operation, entry.slug,
      "erased" if entry.operation == "DELETE" else "active",
      getattr(entry, "release", 1), None if entry.operation == "DELETE" else "candidate",
    ) for entry in self.plan.articles)
    plan: PublishPlanInput = PublishPlan(articles, (ImagePlanEntry("ADD", "images/a.png", b"image"),))
    before = self.tree()
    from checker import create_preflight, update_preflight, delete_preflight, recreate_preflight
    modules = (create_preflight, update_preflight, delete_preflight, recreate_preflight)
    with ExitStack() as stack:
      mocks = []
      for module in modules:
        name = "preflight_" + module.__name__.split(".")[-1].split("_")[0]
        original = getattr(module, name)
        def dispatch(root, view, fetch, original=original):
          self.assertTrue(any(view.articles[0] is entry for entry in articles))
          return original(root, view, fetch)
        mocks.append(stack.enter_context(patch.object(module, name, side_effect=dispatch)))
      classify = stack.enter_context(patch("converter.planning._classify",
                                           side_effect=AssertionError("Unexpected classification")))
      fetch = Mock(side_effect=self.ok_responses())
      result = preflight_plan(self.root, plan, fetch)
    self.assertEqual(result.result, PreflightStatus.OK)
    self.assertIs(plan.articles, articles)
    self.assertEqual(self.tree(), before)
    classify.assert_not_called()
    for mock in mocks:
      mock.assert_called_once()
    self.assertEqual(fetch.call_count, 4)


if __name__ == "__main__":
  unittest.main()
