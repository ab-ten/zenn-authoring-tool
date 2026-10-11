"""隔離 Git repository で認可、pending 構築、対象検証と非変更を検証します。"""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from checker.create_observation import ArticleResponse
from checker.create_preflight import ArticlePreflight, PreflightResult, PreflightStatus
from checker.delete_preflight import DeleteArticlePreflight
from checker.errors import LocalApplyError
from checker.update_preflight import UpdateArticlePreflight
from converter import apply_preparation as preparation
from converter.models import ArticlePlanEntry, ImagePlanEntry, PublishPlan
from converter.preflight_integration import PreflightOutcome, preflight_publish
import test_article_integration as fixtures
from test_article_integration import ID, OTHER, SLUG, NOW, OBSERVATION, source
from test_entry_guards import snapshot
from test_unified_preflight import DELETE_ID, RECREATE_ID, DELETE_SLUG, OLD_SLUG, NEW_SLUG


class ApplyPreparationTests(unittest.TestCase):
  setUp = fixtures.ArticleIntegrationTests.setUp
  commit = fixtures.ArticleIntegrationTests.commit
  save_ledger = fixtures.ArticleIntegrationTests.save_ledger
  write_source = fixtures.ArticleIntegrationTests.write_source
  confirm = fixtures.ArticleIntegrationTests.confirm

  def tree(self):
    objects = {}
    def walk(directory):
      for path in directory.iterdir():
        info = path.lstat()
        mode = info.st_mode
        value = os.readlink(path) if stat.S_ISLNK(mode) else None
        objects[str(path.relative_to(self.root))] = (mode, info.st_mtime_ns, value)
        if stat.S_ISDIR(mode):
          walk(path)
    walk(self.root)
    return snapshot(self.root), objects

  def prepare(self, outcome):
    before = self.tree()
    original = repr(outcome)
    overrides = tuple(entry.observation_override for entry in outcome.plan.articles) if (
      isinstance(getattr(outcome, "plan", None), PublishPlan)) else ()
    with ExitStack() as stack:
      for target in ("converter.source_parser.parse_source", "converter.planning._classify",
                     "converter.image_planning._mirror_diff", "checker.unified_preflight.preflight_plan",
                     "checker.atomic_write.atomic_replace_bytes"):
        stack.enter_context(patch(target, side_effect=AssertionError("Unexpected work")))
      try:
        return preparation.prepare_apply(outcome)
      finally:
        self.assertEqual(self.tree(), before)
        self.assertEqual(repr(outcome), original)
        if overrides:
          for entry, override in zip(outcome.plan.articles, overrides):
            self.assertIs(entry.observation_override, override)
        self.http_mock.assert_not_called()
        self.zenn_http_mock.assert_not_called()

  def outcome(self, articles=(), images=(), results=None):
    if results is None:
      results = tuple(
        UpdateArticlePreflight(entry.article_id, entry.slug, PreflightStatus.OK,
                               baseline_observation=dict(OBSERVATION)) if entry.operation == "UPDATE"
        else DeleteArticlePreflight(entry.article_id, entry.slug, PreflightStatus.OK)
        if entry.operation == "DELETE"
        else ArticlePreflight(entry.article_id, entry.slug, PreflightStatus.OK)
        for entry in articles)
    return PreflightOutcome(self.deploy, PublishPlan(articles, images),
                            PreflightResult(PreflightStatus.OK, results))

  def create(self, **changes):
    return replace(ArticlePlanEntry(ID, "CREATE", SLUG, "active", 1, "candidate"), **changes)

  def mixed(self):
    self.confirm(article_id=OTHER, slug="update-article-slug")
    self.confirm(article_id=DELETE_ID, slug=DELETE_SLUG)
    self.confirm(article_id=RECREATE_ID, slug=OLD_SLUG, state="erased")
    return self.outcome((
      self.create(),
      ArticlePlanEntry(OTHER, "UPDATE", "update-article-slug", "active", 4, "updated",
                       observation_override=dict(OBSERVATION)),
      ArticlePlanEntry(DELETE_ID, "DELETE", DELETE_SLUG, "erased", 3),
      ArticlePlanEntry(RECREATE_ID, "RECREATE", NEW_SLUG, "active", 3, "recreated"),
    ))

  def reject(self, outcome):
    with self.assertRaises(LocalApplyError):
      self.prepare(outcome)

  def test_mixed_pending_and_identity(self):
    outcome = self.mixed()
    self.ledger["unrelated"] = {"retained": ["日本語", 7]}
    self.save_ledger()
    prepared = self.prepare(outcome)
    self.assertIs(prepared.outcome, outcome)
    self.assertIs(prepared.outcome.plan, outcome.plan)
    self.assertIs(prepared.outcome.preflight, outcome.preflight)
    ledger = json.loads(prepared.pending_ledger_bytes)
    self.assertEqual(ledger["articles"], self.ledger["articles"])
    self.assertEqual(ledger["unrelated"], self.ledger["unrelated"])
    self.assertEqual(len(ledger["pending_next_state"]), 4)
    for entry, target in zip(outcome.plan.articles, prepared.article_targets):
      self.assertIs(target.entry, entry)
      self.assertEqual(target.path, self.deploy / "articles" / f"{entry.slug}.md")
      expected = {"operation": entry.operation, "slug": entry.slug,
                  "state": entry.state, "release": entry.release}
      if entry.operation == "UPDATE":
        expected["baseline_observation"] = dict(OBSERVATION)
      self.assertEqual(ledger["pending_next_state"][entry.article_id], expected)
    self.assertNotIn(b"candidate", prepared.pending_ledger_bytes)
    self.assertNotIn(b"observation_override", prepared.pending_ledger_bytes)
    with self.assertRaises(FrozenInstanceError):
      prepared.pending_ledger_bytes = b"changed"

  def test_baseline_is_copied(self):
    outcome = self.mixed()
    baseline = outcome.preflight.articles[1].baseline_observation
    prepared = self.prepare(outcome)
    baseline["published_at"] = "changed"
    self.assertEqual(json.loads(prepared.pending_ledger_bytes)["pending_next_state"][OTHER]
                     ["baseline_observation"], OBSERVATION)

  def test_baseline_uses_existing_string_validation_and_normalization(self):
    outcome = self.mixed()
    baseline = {key: "non-date-string" for key in OBSERVATION}
    results = list(outcome.preflight.articles)
    results[1] = replace(results[1], baseline_observation={**baseline, "extra": "discard"})
    outcome = replace(outcome, preflight=replace(outcome.preflight, articles=tuple(results)))
    prepared = self.prepare(outcome)
    self.assertEqual(json.loads(prepared.pending_ledger_bytes)["pending_next_state"][OTHER]
                     ["baseline_observation"], baseline)

  def test_overall_status_and_types_fail_closed(self):
    outcome = self.mixed()
    for status in (PreflightStatus.COLLISION, PreflightStatus.ERROR, "OK", None, object()):
      with self.subTest(status=status):
        self.reject(replace(outcome, preflight=replace(outcome.preflight, result=status)))
    for result in (None, SimpleNamespace(result=PreflightStatus.OK), "OK"):
      with self.subTest(result=result):
        self.reject(replace(outcome, preflight=result))
    self.reject(SimpleNamespace(**outcome.__dict__))
    self.reject(replace(outcome, plan=SimpleNamespace(articles=(), images=())))

  def test_missing_reordered_duplicate_mismatched_results(self):
    outcome = self.mixed()
    results = outcome.preflight.articles
    invalid = (results[:-1], tuple(reversed(results)), (results[0], results[0], *results[2:]),
               (*results, results[0]), list(results),
               (replace(results[0], article_id=OTHER), *results[1:]),
               (replace(results[0], slug="another-valid-slug"), *results[1:]),
               (replace(results[0], result=PreflightStatus.ERROR), *results[1:]),
               (SimpleNamespace(**results[0].__dict__), *results[1:]),
               (replace(results[0], result="OK"), *results[1:]))
    for items in invalid:
      with self.subTest(items=items):
        self.reject(replace(outcome, preflight=replace(outcome.preflight, articles=items)))

  def test_update_requires_valid_baseline_and_result_type(self):
    outcome = self.mixed()
    results = outcome.preflight.articles
    for baseline in (None, {}, [], {**OBSERVATION, "body_updated_at": ""},
                     {**OBSERVATION, "published_at": None}):
      with self.subTest(baseline=baseline):
        items = (results[0], replace(results[1], baseline_observation=baseline), *results[2:])
        self.reject(replace(outcome, preflight=replace(outcome.preflight, articles=items)))
    items = (results[0], ArticlePreflight(OTHER, results[1].slug, PreflightStatus.OK), *results[2:])
    self.reject(replace(outcome, preflight=replace(outcome.preflight, articles=items)))

  def test_non_update_cannot_acquire_baseline(self):
    entry = self.create()
    result = UpdateArticlePreflight(ID, SLUG, PreflightStatus.OK,
                                    baseline_observation=dict(OBSERVATION))
    self.reject(self.outcome((entry,), results=(result,)))

  def test_duplicate_plan_ids_and_slugs(self):
    entry = self.create()
    for second in (entry, replace(entry, article_id=OTHER), replace(entry, slug="another-valid-slug")):
      with self.subTest(second=second):
        self.reject(self.outcome((entry, second)))

  def test_operation_contracts(self):
    for changes in ({"state": "hold"}, {"release": True}, {"release": 0},
                    {"slug": "../outside"}, {"article_id": "invalid"}):
      with self.subTest(changes=changes):
        self.reject(self.outcome((self.create(**changes),)))
    outcome = self.mixed()
    for index, changes in ((1, {"release": 3}), (2, {"state": "active"}),
                           (2, {"slug": "wrong-confirmed-slug"}), (3, {"release": 7})):
      entries = list(outcome.plan.articles)
      entries[index] = replace(entries[index], **changes)
      self.reject(self.outcome(tuple(entries)))

  def test_noop_and_image_only_have_no_pending_serialization(self):
    for images in ((), (ImagePlanEntry("UPDATE", "images/asset.png", b"fixed"),)):
      with self.subTest(images=images), patch.object(preparation, "ledger_bytes") as serialize:
        outcome = self.outcome(images=images)
        prepared = self.prepare(outcome)
        self.assertIsNone(prepared.pending_ledger_bytes)
        serialize.assert_not_called()
        if images:
          self.assertIs(prepared.image_writes[0].entry, images[0])
          self.assertIs(prepared.image_writes[0].entry.content, images[0].content)

  def test_pending_blocks_even_noop_or_image_only(self):
    self.ledger["pending_next_state"][ID] = {}
    self.save_ledger()
    for images in ((), (ImagePlanEntry("UPDATE", "images/asset.png", b"new"),)):
      self.reject(self.outcome(images=images))

  def test_invalid_ledger_and_serialization(self):
    for content in (b"invalid", b"[]", b'{"articles": {}, "pending_next_state": []}'):
      self.ledger_path.write_bytes(content)
      self.reject(self.outcome())
    self.save_ledger()
    for error in (ValueError("serialize"), UnicodeEncodeError("utf-8", "x", 0, 1, "bad")):
      with patch.object(preparation, "ledger_bytes", side_effect=error):
        self.reject(self.outcome((self.create(),)))
    with patch.object(preparation, "ledger_bytes", return_value=None):
      self.reject(self.outcome((self.create(),)))
    self.ledger["unrelated"] = "\ud800"
    self.save_ledger()
    self.reject(self.outcome((self.create(),)))

  def test_article_existing_targets_and_parent_safety(self):
    path = self.deploy / "articles" / f"{SLUG}.md"
    path.parent.mkdir()
    for kind in ("file", "directory", "symlink", "fifo"):
      with self.subTest(kind=kind):
        if kind == "file":
          path.write_bytes(b"unrelated")
        elif kind == "directory":
          path.mkdir()
        elif kind == "symlink":
          path.symlink_to(self.root / "missing")
        else:
          os.mkfifo(path)
        self.reject(self.outcome((self.create(),)))
        if kind == "directory":
          path.rmdir()
        else:
          path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(self.root / "articles", target_is_directory=True)
    self.reject(self.outcome((self.create(),)))

  def test_missing_update_delete_and_existing_recreate_artifact(self):
    outcome = self.mixed()
    for index in (1, 2):
      path = self.deploy / "articles" / f"{outcome.plan.articles[index].slug}.md"
      content = path.read_bytes()
      path.unlink()
      self.reject(outcome)
      path.write_bytes(content)
    path = self.deploy / "articles" / f"{NEW_SLUG}.md"
    path.write_bytes(b"unrelated")
    self.reject(outcome)

  def test_nested_unusual_image_paths_and_fixed_bytes(self):
    entries = (ImagePlanEntry("ADD", "images/nested/a b\nc\t日本語:#?.png", b"fixed"),)
    outcome = self.outcome(images=entries)
    prepared = self.prepare(outcome)
    self.assertIs(prepared.image_writes[0].entry, entries[0])
    self.assertEqual(prepared.image_writes[0].path,
                     self.deploy / entries[0].path)
    self.assertEqual(prepared.image_directories_to_remove, ())

  def test_file_to_directory_transition(self):
    entries = (ImagePlanEntry("ADD", "images/asset.png/deep/new.png", b"fixed"),
               ImagePlanEntry("DELETE", "images/asset.png"))
    prepared = self.prepare(self.outcome(images=entries))
    self.assertEqual(prepared.image_deletes[0].path, self.deploy / "images/asset.png")
    self.assertEqual(prepared.image_writes[0].path, self.deploy / entries[0].path)
    self.assertEqual(prepared.image_directories_to_remove, ())

  def test_directory_to_file_transition_deepest_first(self):
    base = self.deploy / "images/replacement.png"
    (base / "nested/empty").mkdir(parents=True)
    (base / "nested/old.png").write_bytes(b"old")
    entries = (ImagePlanEntry("ADD", "images/replacement.png", b"fixed"),
               ImagePlanEntry("DELETE", "images/replacement.png/nested/old.png"))
    prepared = self.prepare(self.outcome(images=entries))
    self.assertEqual(prepared.image_directories_to_remove,
                     (base / "nested/empty", base / "nested", base))
    self.reject(self.outcome(images=entries[:1]))

  def test_empty_directory_to_file_transition(self):
    base = self.deploy / "images/empty.png"
    base.mkdir()
    prepared = self.prepare(self.outcome(images=(ImagePlanEntry("ADD", "images/empty.png", b"new"),)))
    self.assertEqual(prepared.image_directories_to_remove, (base,))

  def test_image_contracts_and_conflicting_targets(self):
    for entries in ((ImagePlanEntry("ADD", "images/asset.png", b"new"),),
                    (ImagePlanEntry("UPDATE", "images/missing.png", b"new"),),
                    (ImagePlanEntry("DELETE", "images/missing.png"),),
                    (ImagePlanEntry("ADD", "images/asset.png/nested.png", b"new"),),
                    (ImagePlanEntry("ADD", "images/new.png", b"new"),
                     ImagePlanEntry("ADD", "images/new.png/child.png", b"new")),
                    (ImagePlanEntry("DELETE", "images/asset.png"),
                     ImagePlanEntry("UPDATE", "images/asset.png", b"new"))):
      with self.subTest(entries=entries):
        self.reject(self.outcome(images=entries))

  def test_image_directory_cannot_be_update_or_delete(self):
    (self.deploy / "images/directory.png").mkdir()
    for operation in ("UPDATE", "DELETE"):
      entry = ImagePlanEntry(operation, "images/directory.png",
                             b"fixed" if operation == "UPDATE" else None)
      self.reject(self.outcome(images=(entry,)))

  def test_forged_mutable_or_invalid_plan_entries(self):
    for field, value in (("articles", []), ("images", []), ("images", (object(),))):
      outcome = self.outcome()
      object.__setattr__(outcome.plan, field, value)
      self.reject(outcome)
    for field, value in (("content", bytearray(b"mutable")), ("operation", "CREATE")):
      entry = ImagePlanEntry("ADD", "images/new.png", b"fixed")
      object.__setattr__(entry, field, value)
      self.reject(self.outcome(images=(entry,)))

  def test_forged_unsafe_image_paths_fail_closed(self):
    for path in ("images/../../outside.png", "/outside.png", "images/a\\b.png",
                 "images/./a.png", "images//a.png"):
      entry = ImagePlanEntry("ADD", "images/valid.png", b"new")
      object.__setattr__(entry, "path", path)
      self.reject(self.outcome(images=(entry,)))

  def test_image_symlinks_and_special_objects(self):
    base = self.deploy / "images"
    target = base / "unsafe.png"
    for kind in ("symlink", "fifo"):
      if kind == "symlink":
        target.symlink_to(self.root / "images/asset.png")
      else:
        os.mkfifo(target)
      self.reject(self.outcome(images=(ImagePlanEntry("ADD", "images/unsafe.png", b"new"),)))
      target.unlink()
    (base / "parent").symlink_to(self.root / "images", target_is_directory=True)
    self.reject(self.outcome(images=(ImagePlanEntry("ADD", "images/parent/new.png", b"new"),)))
    (base / "parent").unlink()
    target.mkdir()
    (target / "link.png").symlink_to(self.root / "images/asset.png")
    self.reject(self.outcome(images=(ImagePlanEntry("ADD", "images/unsafe.png", b"new"),)))

  def test_image_root_and_management_symlink_rejection(self):
    (self.deploy / "images/asset.png").unlink()
    (self.deploy / "images").rmdir()
    (self.deploy / "images").symlink_to(self.root / "images", target_is_directory=True)
    self.reject(self.outcome())
    (self.deploy / "images").unlink()
    (self.deploy / "images").mkdir()
    self.ledger_path.unlink()
    self.ledger_path.symlink_to(self.root / "missing")
    self.reject(self.outcome())

  def test_preflight_publish_direct_reuse(self):
    self.write_source()
    (self.management / "config.json").write_text(json.dumps({"zenn_user": {"id": 12345}}))
    self.commit(self.deploy)
    outcome = preflight_publish(self.root, now=NOW, fetch_article=lambda slug: ArticleResponse(404))
    prepared = self.prepare(outcome)
    self.assertIs(prepared.outcome, outcome)
    self.assertEqual(prepared.article_targets[0].path, self.deploy / "articles" / f"{SLUG}.md")
    self.assertFalse(prepared.article_targets[0].path.exists())


if __name__ == "__main__":
  unittest.main()
