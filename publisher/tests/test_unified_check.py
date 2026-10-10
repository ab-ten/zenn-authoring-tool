"""4 operation の混在、部分確定、復旧と安全性を fixture で検証します。"""

from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import check_pending as checker
from checker import terminal_create, terminal_update, terminal_delete, terminal_recreate
from checker import terminal_collision
from checker.commit_readiness import CommitReadiness
from checker.create_observation import ArticleResponse, CreateResult


IDS = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 6)]
SLUGS = [f"unified-article-{index}" for index in range(1, 6)]
OLD_SLUG = "unified-erased-old"
BASELINE = {key: "previous" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}
CURRENT = {**BASELINE, "body_updated_at": "current"}
OPERATIONS = ("CREATE", "UPDATE", "DELETE", "RECREATE")
MODULES = (terminal_create, terminal_update, terminal_delete, terminal_recreate)


def response(slug, owner=2, observation=CURRENT):
  """API の所有者と observation を fixture として返します。"""
  return ArticleResponse(200, {"article": {
    "slug": slug, "user": {"id": owner}, **observation}})


class UnifiedCheckTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    self.meta = self.root / ".zenn-authoring"
    (self.meta / "released").mkdir(parents=True)
    (self.root / "articles").mkdir()
    (self.meta / "config.json").write_text('{"zenn_user":{"id":2}}', encoding="utf-8")
    self.ledger_path = self.meta / "ledger.json"
    self.ledger = {"articles": {}, "pending_next_state": {}, "extra": "preserved"}
    for index, operation in enumerate(OPERATIONS):
      pending = {"operation": operation, "state": "erased" if operation == "DELETE" else "active",
                 "slug": SLUGS[index], "release": 2 if operation in ("UPDATE", "RECREATE") else 1}
      if operation == "UPDATE":
        pending["baseline_observation"] = BASELINE
      self.ledger["pending_next_state"][IDS[index]] = pending
      if operation != "CREATE":
        self.ledger["articles"][IDS[index]] = {
          "slug": OLD_SLUG if operation == "RECREATE" else SLUGS[index],
          "state": "erased" if operation == "RECREATE" else "active",
          "successful_release": 1, "successful_zenn_observation": BASELINE}
        self.snapshot(index).write_bytes(f"previous {index}\n".encode())
      if operation != "DELETE":
        self.artifact(index).write_bytes(f"current {index}\n".encode())
    self.save()

  def artifact(self, index):
    return self.root / "articles" / f"{SLUGS[index]}.md"

  def snapshot(self, index):
    return self.meta / "released" / f"{IDS[index]}.md"

  def save(self):
    self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")

  def read(self):
    return json.loads(self.ledger_path.read_bytes())

  def fetch(self, slug):
    return ArticleResponse(404) if slug == SLUGS[2] else response(slug)

  def check(self, fixtures=None):
    def fetch(slug):
      value = fixtures[slug] if fixtures and slug in fixtures else self.fetch(slug)
      if isinstance(value, Exception):
        raise value
      return value
    self.adapter = Mock(side_effect=fetch)
    return checker.check_pending(self.root, self.adapter)

  def assert_pending(self, indexes):
    self.assertEqual(set(self.read()["pending_next_state"]), {IDS[i] for i in indexes})

  def assert_ready(self, result, ready=True):
    self.assertEqual(result.readiness.result,
                     CommitReadiness.COMMIT_READY if ready else CommitReadiness.NOT_READY,
                     result.readiness.diagnostics)

  def test_all_four_observations_and_classifications_before_any_write(self):
    before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
    observed, classified, writes = [], [], []

    def fetch(slug):
      for path, content in before.items():
        self.assertEqual(path.read_bytes(), content)
      self.assertFalse(self.snapshot(0).exists())
      observed.append(slug)
      return self.fetch(slug)

    def classify(real, operation):
      def call(*args):
        value = real(*args)
        classified.append(operation)
        return value
      return call

    real_replace = terminal_create.atomic_replace_bytes

    def replace(path, content):
      self.assertEqual(observed, SLUGS[:4])
      self.assertEqual(classified, list(OPERATIONS))
      writes.append(path)
      return real_replace(path, content)

    with ExitStack() as stack:
      for operation in OPERATIONS:
        name = f"classify_{operation.lower()}_response"
        stack.enter_context(patch.object(checker, name,
                                         side_effect=classify(getattr(checker, name), operation)))
      for module in MODULES:
        stack.enter_context(patch.object(module, "atomic_replace_bytes", side_effect=replace))
      result = checker.check_pending(self.root, fetch)
    self.assertTrue(writes)
    self.assert_ready(result)

  def test_all_four_success_preserves_prior_updates_and_delete_history(self):
    previous_delete = copy.deepcopy(self.ledger["articles"][IDS[2]])
    previous_snapshot = self.snapshot(2).read_bytes()
    result = self.check()
    self.assertEqual([r.result for r in result.articles.values()], [CreateResult.SUCCESS] * 4)
    self.assert_pending([])
    ledger = self.read()
    self.assertEqual(ledger["extra"], "preserved")
    for index in (0, 1, 3):
      self.assertEqual(ledger["articles"][IDS[index]]["slug"], SLUGS[index])
      self.assertEqual(ledger["articles"][IDS[index]]["successful_zenn_observation"], CURRENT)
      self.assertEqual(self.snapshot(index).read_bytes(), self.artifact(index).read_bytes())
    self.assertEqual(ledger["articles"][IDS[1]]["successful_release"], 2)
    self.assertEqual(ledger["articles"][IDS[3]]["successful_release"], 2)
    self.assertEqual(ledger["articles"][IDS[2]], {**previous_delete, "state": "erased"})
    self.assertEqual(self.snapshot(2).read_bytes(), previous_snapshot)
    self.assert_ready(result)

  def test_recreate_same_slug_same_release_allowed(self):
    self.ledger["articles"][IDS[3]]["slug"] = SLUGS[3]
    self.ledger["pending_next_state"][IDS[3]]["release"] = 1
    self.save()
    self.assert_ready(self.check())
    self.assertEqual(self.read()["articles"][IDS[3]]["successful_release"], 1)

  def test_partial_completion_and_second_check_only_observes_unresolved(self):
    previous = copy.deepcopy(self.ledger["articles"][IDS[3]])
    snapshot = self.snapshot(3).read_bytes()
    result = self.check({SLUGS[1]: response(SLUGS[1], observation=BASELINE),
                         SLUGS[3]: response(SLUGS[3], owner=99)})
    self.assertEqual([r.result for r in result.articles.values()], [
      CreateResult.SUCCESS, CreateResult.PENDING, CreateResult.SUCCESS, CreateResult.COLLISION])
    self.assert_pending([1])
    self.assertEqual(self.read()["articles"][IDS[3]], previous)
    self.assertEqual(self.snapshot(3).read_bytes(), snapshot)
    self.assertFalse(self.artifact(3).exists())
    self.assert_ready(result, False)
    result = self.check()
    self.adapter.assert_called_once_with(SLUGS[1])
    self.assertEqual(set(result.articles), {IDS[1]})
    self.assert_ready(result)

  def test_create_and_recreate_collision_cleanup_after_all_classifications(self):
    previous = copy.deepcopy(self.ledger["articles"][IDS[3]])
    snapshot = self.snapshot(3).read_bytes()
    classified = []
    real_create = checker.classify_create_response
    real_recreate = checker.classify_recreate_response
    real_unlink = Path.unlink

    def create(*args):
      classified.append("CREATE")
      return real_create(*args)

    def recreate(*args):
      classified.append("RECREATE")
      return real_recreate(*args)

    def unlink(path, *args, **kwargs):
      if path in (self.artifact(0), self.artifact(3)):
        self.assertEqual(classified, ["CREATE", "RECREATE"])
        self.assertEqual(self.adapter.call_count, 4)
      return real_unlink(path, *args, **kwargs)

    with patch.object(checker, "classify_create_response", side_effect=create), \
         patch.object(checker, "classify_recreate_response", side_effect=recreate), \
         patch.object(Path, "unlink", autospec=True, side_effect=unlink):
      result = self.check({SLUGS[0]: response(SLUGS[0], 99), SLUGS[3]: response(SLUGS[3], 99)})
    self.assertFalse(self.artifact(0).exists())
    self.assertFalse(self.artifact(3).exists())
    self.assertNotIn(IDS[0], self.read()["articles"])
    self.assertEqual(self.read()["articles"][IDS[3]], previous)
    self.assertEqual(self.snapshot(3).read_bytes(), snapshot)
    self.assert_ready(result)

  def test_delete_own_user_200_is_pending(self):
    result = self.check({SLUGS[2]: response(SLUGS[2])})
    self.assertEqual(result.articles[IDS[2]].result, CreateResult.PENDING)
    self.assert_pending([2])
    self.assertEqual(self.read()["articles"][IDS[2]]["state"], "active")
    self.assert_ready(result, False)

  def test_update_other_owner_and_404_are_unknown_never_collision(self):
    for remote in (response(SLUGS[1], 99), ArticleResponse(404)):
      with self.subTest(remote=remote):
        self.save()
        result = self.check({SLUGS[1]: remote})
        self.assertEqual(result.articles[IDS[1]].result, CreateResult.UNKNOWN)
        self.assertTrue(self.artifact(1).exists())
        self.assert_pending([1])

  def test_delete_other_owner_is_success_never_collision(self):
    result = self.check({SLUGS[2]: response(SLUGS[2], 99)})
    self.assertEqual(result.articles[IDS[2]].result, CreateResult.SUCCESS)
    self.assert_ready(result)

  def test_network_error_and_unknown_do_not_block_other_terminal_results(self):
    result = self.check({SLUGS[0]: OSError("network unavailable"), SLUGS[1]: ArticleResponse(302)})
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(result.articles[IDS[1]].result, CreateResult.UNKNOWN)
    self.assertEqual(self.adapter.call_count, 4)
    self.assert_pending([0, 1])
    self.assertEqual(self.read()["articles"][IDS[2]]["state"], "erased")
    self.assertEqual(self.read()["articles"][IDS[3]]["state"], "active")
    self.assert_ready(result, False)

  def test_cross_article_slug_and_artifact_collisions_block_all_writes(self):
    original = copy.deepcopy(self.ledger)
    cases = [("pending_next_state", IDS[0], SLUGS[1]),
             ("pending_next_state", IDS[0], SLUGS[3]),
             ("pending_next_state", IDS[0], OLD_SLUG),
             ("articles", IDS[2], SLUGS[1]),
             ("pending_next_state", IDS[3], SLUGS[2])]
    for entries, article_id, slug in cases:
      with self.subTest(entries=entries, article_id=article_id, slug=slug):
        self.ledger = copy.deepcopy(original)
        self.ledger[entries][article_id]["slug"] = slug
        self.save()
        before = self.ledger_path.read_bytes()
        result = self.check()
        self.adapter.assert_not_called()
        self.assertTrue(result.diagnostics)
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertFalse(self.snapshot(0).exists())
        self.assertTrue(self.artifact(0).exists())
        self.assert_ready(result, False)

  def test_invalid_pending_is_error_other_valid_articles_progress(self):
    original = copy.deepcopy(self.ledger)
    for entry in (None, [], {"operation": "BOGUS"}, {"operation": []},
                  {**original["pending_next_state"][IDS[0]], "slug": []},
                  {**original["pending_next_state"][IDS[0]], "state": "erased"},
                  {**original["pending_next_state"][IDS[0]], "release": True}):
      with self.subTest(entry=entry):
        self.ledger = copy.deepcopy(original)
        self.ledger["pending_next_state"][IDS[0]] = entry
        self.save()
        result = self.check()
        self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
        self.assertEqual(result.articles[IDS[1]].result, CreateResult.SUCCESS)
        self.assertEqual(result.articles[IDS[2]].result, CreateResult.SUCCESS)
        self.assertEqual(result.articles[IDS[3]].result, CreateResult.SUCCESS)
        self.assert_pending([0])
        self.assertIn(IDS[0], self.read()["pending_next_state"])
        self.assert_ready(result, False)

  def test_malformed_other_pending_does_not_block_collision_cleanup(self):
    self.ledger["pending_next_state"][IDS[1]] = None
    self.save()
    result = self.check({SLUGS[0]: response(SLUGS[0], 99),
                         SLUGS[3]: response(SLUGS[3], 99)})
    self.assertEqual(result.articles[IDS[1]].result, CreateResult.ERROR)
    for index in (0, 3):
      self.assertEqual(result.articles[IDS[index]].result, CreateResult.COLLISION)
      self.assertFalse(self.artifact(index).exists())
    self.assert_pending([1])

  def test_operation_specific_invalid_confirmed_state_retains_pending(self):
    original = copy.deepcopy(self.ledger)
    for index, change in ((0, {"state": "active"}), (1, {"state": "erased"}),
                          (2, {"slug": "different-valid-slug"}), (3, {"state": "active"})):
      with self.subTest(index=index):
        self.ledger = copy.deepcopy(original)
        confirmed = self.ledger["articles"].setdefault(IDS[index], {})
        confirmed.update(change)
        self.save()
        result = self.check()
        self.assertEqual(result.articles[IDS[index]].result, CreateResult.ERROR)
        self.assertIn(IDS[index], self.read()["pending_next_state"])

  def test_snapshot_replacement_interruption_then_reobserve_retry(self):
    for index in (0, 1, 3):
      with self.subTest(operation=OPERATIONS[index]):
        self.setUp()
        real = MODULES[index].atomic_replace_bytes

        def fail_ledger(path, content):
          if path == self.ledger_path:
            raise OSError("interrupted after snapshot")
          return real(path, content)

        with patch.object(MODULES[index], "atomic_replace_bytes", side_effect=fail_ledger):
          result = self.check()
        self.assertEqual(result.articles[IDS[index]].result, CreateResult.ERROR)
        self.assertEqual(self.snapshot(index).read_bytes(), self.artifact(index).read_bytes())
        self.assert_pending([index])
        snapshot_stat = self.snapshot(index).stat()
        self.assert_ready(self.check())
        self.adapter.assert_called_once_with(SLUGS[index])
        self.assertEqual(self.snapshot(index).stat().st_ino, snapshot_stat.st_ino)
        self.assertEqual(self.snapshot(index).stat().st_mtime_ns, snapshot_stat.st_mtime_ns)

  def test_collision_cleanup_interruption_then_reobserve_retry(self):
    for index in (0, 3):
      with self.subTest(operation=OPERATIONS[index]):
        self.setUp()
        previous = copy.deepcopy(self.ledger["articles"])
        fixtures = {SLUGS[index]: response(SLUGS[index], 99)}
        with patch.object(terminal_collision, "atomic_replace_bytes", side_effect=OSError("interrupted")):
          result = self.check(fixtures)
        self.assertEqual(result.articles[IDS[index]].result, CreateResult.ERROR)
        self.assertFalse(self.artifact(index).exists())
        self.assert_pending([index])
        if index == 3:
          self.assertEqual(self.read()["articles"][IDS[index]], previous[IDS[index]])
        self.assert_ready(self.check(fixtures))
        self.adapter.assert_called_once_with(SLUGS[index])

  def test_actual_ledger_atomic_replacement_failure_keeps_pending(self):
    real_replace = os.replace

    def fail_ledger(source, target):
      if Path(target) == self.ledger_path:
        raise OSError("ledger replacement failed")
      return real_replace(source, target)

    with patch("checker.atomic_write.os.replace", side_effect=fail_ledger):
      result = self.check()
    self.assertEqual([r.result for r in result.articles.values()], [CreateResult.ERROR] * 4)
    self.assertEqual(self.read(), self.ledger)
    self.assertEqual(list(self.meta.glob(".ledger.json.*.tmp")), [])
    self.assert_ready(result, False)
    self.assert_ready(self.check())

  def test_inconsistent_local_files_after_all_pending_resolve_not_ready(self):
    (self.root / "articles" / "unmanaged-article.md").write_bytes(b"unmanaged")
    result = self.check()
    self.assert_pending([])
    self.assert_ready(result, False)
    self.assertTrue(any("unmanaged" in d for d in result.readiness.diagnostics))

  def test_existing_active_snapshot_mismatch_after_completion_not_ready(self):
    self.ledger["articles"][IDS[4]] = {
      "slug": SLUGS[4], "state": "active", "successful_release": 1,
      "successful_zenn_observation": BASELINE}
    self.artifact(4).write_bytes(b"different")
    self.snapshot(4).write_bytes(b"confirmed")
    self.save()
    result = self.check()
    self.assert_pending([])
    self.assert_ready(result, False)
    self.assertTrue(any("differs" in d for d in result.readiness.diagnostics))

  def test_empty_pending_only_evaluates_readiness_without_config_or_remote(self):
    self.assert_ready(self.check())
    (self.meta / "config.json").unlink()
    result = self.check()
    self.adapter.assert_not_called()
    self.assertEqual(result.articles, {})
    self.assert_ready(result)
    self.artifact(1).write_bytes(b"inconsistent")
    result = self.check()
    self.adapter.assert_not_called()
    self.assert_ready(result, False)

  def test_ledger_changed_during_observation_refuses_every_apply(self):
    before_snapshots = {index: self.snapshot(index).read_bytes() for index in (1, 2, 3)}

    def fetch(slug):
      self.ledger["extra"] = "concurrent edit"
      self.save()
      return self.fetch(slug)

    adapter = Mock(side_effect=fetch)
    result = checker.check_pending(self.root, adapter)
    self.assertEqual(adapter.call_count, 4)
    self.assertTrue(result.diagnostics)
    self.assertEqual(self.read(), self.ledger)
    self.assertFalse(self.snapshot(0).exists())
    for index, content in before_snapshots.items():
      self.assertEqual(self.snapshot(index).read_bytes(), content)

  def test_ledger_replaced_with_symlink_during_observation_refuses_apply(self):
    external = self.root / "external-ledger.json"
    external.write_bytes(self.ledger_path.read_bytes())

    def fetch(slug):
      if not self.ledger_path.is_symlink():
        self.ledger_path.unlink()
        self.ledger_path.symlink_to(external)
      return self.fetch(slug)

    result = checker.check_pending(self.root, fetch)
    self.assertTrue(result.diagnostics)
    self.assertFalse(self.snapshot(0).exists())
    self.assertEqual(json.loads(external.read_bytes()), self.ledger)

  def test_artifact_and_snapshot_unsafe_targets_refused_for_every_operation(self):
    for index in range(4):
      for target in ("artifact", "snapshot"):
        for kind in ("symlink", "dangling", "fifo", "directory"):
          with self.subTest(operation=OPERATIONS[index], target=target, kind=kind):
            self.setUp()
            path = self.artifact(index) if target == "artifact" else self.snapshot(index)
            if path.exists():
              path.unlink()
            external = self.root / "external"
            if kind == "symlink":
              external.write_bytes(b"must remain unchanged")
              path.symlink_to(external)
            elif kind == "dangling":
              path.symlink_to(external)
            elif kind == "fifo":
              os.mkfifo(path)
            else:
              path.mkdir()
            result = self.check()
            self.assertEqual(result.articles[IDS[index]].result, CreateResult.ERROR)
            self.assertIn(IDS[index], self.read()["pending_next_state"])
            if kind == "symlink":
              self.assertEqual(external.read_bytes(), b"must remain unchanged")
            elif kind == "dangling":
              self.assertFalse(external.exists())

  def test_collision_unsafe_artifacts_refused_and_other_articles_complete(self):
    for index in (0, 3):
      for kind in ("symlink", "dangling", "fifo", "directory"):
        with self.subTest(index=index, kind=kind):
          self.setUp()
          path = self.artifact(index)
          path.unlink()
          external = self.root / "external"
          if kind == "symlink":
            external.write_bytes(b"preserved")
            path.symlink_to(external)
          elif kind == "dangling":
            path.symlink_to(external)
          elif kind == "fifo":
            os.mkfifo(path)
          else:
            path.mkdir()
          result = self.check({SLUGS[index]: response(SLUGS[index], 99)})
          self.assertEqual(result.articles[IDS[index]].result, CreateResult.ERROR)
          self.assert_pending([index])
          if kind == "symlink":
            self.assertEqual(external.read_bytes(), b"preserved")

  def test_unsafe_ledger_and_parent_paths_refuse_writes(self):
    for target in ("ledger", "articles", "released"):
      for kind in ("symlink", "dangling", "fifo", "file"):
        with self.subTest(target=target, kind=kind):
          self.setUp()
          path = {"ledger": self.ledger_path, "articles": self.root / "articles",
                  "released": self.meta / "released"}[target]
          backup = self.root / "backup"
          path.rename(backup)
          if kind in ("symlink", "dangling"):
            path.symlink_to(backup if kind == "symlink" else self.root / "absent",
                            target_is_directory=target != "ledger")
          elif kind == "fifo":
            os.mkfifo(path)
          else:
            path.write_bytes(b"invalid")
          result = self.check()
          self.assert_ready(result, False)
          if target == "ledger":
            self.adapter.assert_not_called()
            self.assertEqual(json.loads(backup.read_bytes()), self.ledger)
          else:
            self.assertEqual(self.read(), self.ledger)


if __name__ == "__main__":
  unittest.main()
