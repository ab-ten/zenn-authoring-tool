"""複数 CREATE、cleanup、readiness、中断復旧を fixture で検証します。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import check_pending, terminal_collision, terminal_create
from checker.check_pending import check_pending_creates
from checker.commit_readiness import CommitReadiness, check_commit_readiness
from checker.create_observation import ArticleResponse, CreateResult


IDS = ["6a5c05e8-401a-4e2b-bc92-2c9aa61be305",
       "95660b64-4620-4292-b509-02f8b6f49599",
       "ad897baa-c205-4a14-aafc-dc253f29a8aa"]
SLUGS = ["multi-create-article-a", "multi-create-article-b", "multi-create-article-c"]
OBSERVATION = {key: "2026-10-10T00:00:00+09:00" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}


def response(slug, owner=2):
  """所有者と slug を指定して API fixture を生成します。"""
  return ArticleResponse(200, {"article": {
    "slug": slug, "user": {"id": owner}, **OBSERVATION}})


class PendingCheckTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    self.meta = self.root / ".zenn-authoring"
    (self.meta / "released").mkdir(parents=True)
    (self.root / "articles").mkdir()
    self.ledger_path = self.meta / "ledger.json"
    (self.meta / "config.json").write_text('{"zenn_user":{"id":2}}', encoding="utf-8")
    self.ledger = {"articles": {}, "pending_next_state": {}}
    self.write_ledger()

  def write_ledger(self):
    self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")

  def read_ledger(self):
    return json.loads(self.ledger_path.read_bytes())

  def artifact(self, index):
    return self.root / "articles" / f"{SLUGS[index]}.md"

  def snapshot(self, index):
    return self.meta / "released" / f"{IDS[index]}.md"

  def add_pending(self, count):
    for index in range(count):
      self.ledger["pending_next_state"][IDS[index]] = {
        "operation": "CREATE", "slug": SLUGS[index], "state": "active", "release": 1}
      self.artifact(index).write_bytes(f"article {index}\n".encode())
    self.write_ledger()

  def check(self, statuses):
    fixtures = {SLUGS[index]: (
      response(SLUGS[index]) if status == "SUCCESS" else
      response(SLUGS[index], 99) if status == "COLLISION" else
      ArticleResponse(status)) for index, status in enumerate(statuses)}
    self.fetch = Mock(side_effect=lambda slug: fixtures[slug])
    return check_pending_creates(self.root, self.fetch)

  def assert_ready(self, result, ready):
    expected = CommitReadiness.COMMIT_READY if ready else CommitReadiness.NOT_READY
    self.assertEqual(result.readiness.result, expected, result.readiness.diagnostics)

  def confirm(self, index=0, state="active"):
    self.ledger["articles"][IDS[index]] = {
      "slug": SLUGS[index], "state": state, "successful_release": 1,
      "successful_zenn_observation": OBSERVATION}
    self.snapshot(index).write_bytes(b"confirmed content")
    if state == "active":
      self.artifact(index).write_bytes(b"confirmed content")
    self.write_ledger()

  def test_all_observations_and_classifications_before_first_write(self):
    self.add_pending(3)
    before = self.ledger_path.read_bytes()
    observed = []
    classified = []
    real_classify = check_pending.classify_create_response

    def classify(response_value, slug, owner_id):
      classified.append(slug)
      return real_classify(response_value, slug, owner_id)

    def fetch(slug):
      self.assertEqual(self.ledger_path.read_bytes(), before)
      self.assertFalse(self.snapshot(0).exists())
      self.assertTrue(self.artifact(2).exists())
      observed.append(slug)
      return response(slug, 99 if slug == SLUGS[2] else 2)

    real_replace = terminal_create.atomic_replace_bytes
    real_collision = terminal_collision.atomic_replace_bytes

    def replace(path, content):
      self.assertEqual(observed, SLUGS)
      self.assertEqual(classified, SLUGS)
      return real_replace(path, content)

    def collision_replace(path, content):
      self.assertEqual(observed, SLUGS)
      self.assertEqual(classified, SLUGS)
      return real_collision(path, content)

    with patch.object(terminal_create, "atomic_replace_bytes", side_effect=replace), \
         patch.object(terminal_collision, "atomic_replace_bytes", side_effect=collision_replace), \
         patch.object(check_pending, "classify_create_response", side_effect=classify):
      result = check_pending_creates(self.root, fetch)
    self.assert_ready(result, True)

  def test_success_pending_partial_completion(self):
    self.add_pending(2)
    result = self.check(["SUCCESS", 404])
    self.assert_ready(result, False)
    self.assertEqual(set(self.read_ledger()["pending_next_state"]), {IDS[1]})
    self.assertEqual(set(self.read_ledger()["articles"]), {IDS[0]})
    self.assertFalse(self.snapshot(1).exists())

  def test_success_collision_ready(self):
    self.add_pending(2)
    result = self.check(["SUCCESS", "COLLISION"])
    self.assert_ready(result, True)
    self.assertFalse(self.artifact(1).exists())
    self.assertEqual(set(self.read_ledger()["articles"]), {IDS[0]})

  def test_success_pending_collision_and_rerun_only_unresolved(self):
    self.add_pending(3)
    result = self.check(["SUCCESS", 404, "COLLISION"])
    self.assert_ready(result, False)
    self.assertEqual(set(self.read_ledger()["pending_next_state"]), {IDS[1]})
    self.assertFalse(self.artifact(2).exists())
    fetch = Mock(return_value=response(SLUGS[1]))
    with patch("checker.check_pending.apply_collision") as collision, \
         patch("checker.check_pending.apply_create_success", wraps=terminal_create.apply_create_success) as success:
      result = check_pending_creates(self.root, fetch)
    fetch.assert_called_once_with(SLUGS[1])
    success.assert_called_once_with(self.root, IDS[1], OBSERVATION)
    collision.assert_not_called()
    self.assert_ready(result, True)

  def test_collision_preserves_snapshot_and_source(self):
    self.add_pending(1)
    self.snapshot(0).write_bytes(b"existing recovery snapshot")
    source = self.root / "source.md"
    source.write_bytes(b"authoring source")
    result = self.check(["COLLISION"])
    self.assert_ready(result, True)
    self.assertFalse(self.artifact(0).exists())
    self.assertEqual(self.read_ledger(), {"articles": {}, "pending_next_state": {}})
    self.assertEqual(self.snapshot(0).read_bytes(), b"existing recovery snapshot")
    self.assertEqual(source.read_bytes(), b"authoring source")

  def test_collision_interruption_after_cleanup_before_ledger(self):
    self.add_pending(1)
    before = self.ledger_path.read_bytes()
    with patch.object(terminal_collision, "atomic_replace_bytes", side_effect=OSError("interrupted")):
      result = self.check(["COLLISION"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertFalse(self.artifact(0).exists())
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assert_ready(result, False)
    self.assert_ready(self.check(["COLLISION"]), True)

  def test_success_interruption_after_snapshot_before_ledger(self):
    self.add_pending(2)
    original = terminal_create.atomic_replace_bytes

    def fail_ledger(path, content):
      if path == self.ledger_path:
        raise OSError("interrupted")
      return original(path, content)

    with patch.object(terminal_create, "atomic_replace_bytes", side_effect=fail_ledger):
      result = self.check(["SUCCESS", "COLLISION"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(self.snapshot(0).read_bytes(), self.artifact(0).read_bytes())
    self.assertEqual(set(self.read_ledger()["pending_next_state"]), {IDS[0]})
    self.assert_ready(self.check(["SUCCESS"]), True)

  def test_error_unknown_do_not_advance_and_success_still_applies(self):
    self.add_pending(3)
    result = self.check([500, 302, "SUCCESS"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(result.articles[IDS[1]].result, CreateResult.UNKNOWN)
    self.assertEqual(set(self.read_ledger()["articles"]), {IDS[2]})
    self.assertFalse(self.snapshot(0).exists())
    self.assertFalse(self.snapshot(1).exists())
    self.assert_ready(result, False)

  def test_network_error_does_not_stop_other_observations(self):
    self.add_pending(2)
    fetch = Mock(side_effect=[OSError("network"), response(SLUGS[1])])
    result = check_pending_creates(self.root, fetch)
    self.assertEqual(fetch.call_count, 2)
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(result.articles[IDS[1]].result, CreateResult.SUCCESS)

  def test_unsupported_operations_block_all_observation_and_apply(self):
    for operation in ("UPDATE", "DELETE", "RECREATE"):
      with self.subTest(operation=operation):
        self.add_pending(2)
        self.ledger["pending_next_state"][IDS[1]]["operation"] = operation
        self.write_ledger()
        before = self.ledger_path.read_bytes()
        fetch = Mock()
        result = check_pending_creates(self.root, fetch)
        fetch.assert_not_called()
        self.assertTrue(result.diagnostics)
        self.assertEqual(result.articles[IDS[1]].result, CreateResult.ERROR)
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertFalse(self.snapshot(0).exists())
        self.assertTrue(self.artifact(0).exists())

  def test_empty_pending_does_not_fetch_or_reapply(self):
    self.confirm()
    fetch = Mock()
    with patch("checker.check_pending.apply_create_success") as apply:
      result = check_pending_creates(self.root, fetch)
    fetch.assert_not_called()
    apply.assert_not_called()
    self.assert_ready(result, True)

  def test_mismatch_after_all_pending_resolved_not_ready(self):
    self.confirm()
    self.add_pending(2)
    del self.ledger["pending_next_state"][IDS[0]]
    self.write_ledger()
    self.artifact(0).write_bytes(b"mismatch")
    result = self.check(["SUCCESS", "SUCCESS"])
    self.assertEqual(self.read_ledger()["pending_next_state"], {})
    self.assert_ready(result, False)
    self.assertTrue(any("differs" in item for item in result.readiness.diagnostics))

  def test_unmanaged_artifact_not_ready(self):
    self.artifact(2).write_bytes(b"unmanaged")
    result = check_pending_creates(self.root, Mock())
    self.assert_ready(result, False)
    self.assertTrue(any("unmanaged" in item for item in result.readiness.diagnostics))

  def test_erased_snapshot_retained_and_artifact_absent_ready(self):
    self.confirm(state="erased")
    self.assertEqual(check_commit_readiness(self.root).result, CommitReadiness.COMMIT_READY)
    self.artifact(0).write_bytes(b"unexpected")
    result = check_commit_readiness(self.root)
    self.assertEqual(result.result, CommitReadiness.NOT_READY)
    self.assertTrue(any("Erased" in item for item in result.diagnostics))

  def test_missing_active_artifact_or_snapshot_not_ready(self):
    self.confirm()
    self.artifact(0).unlink()
    self.assertEqual(check_commit_readiness(self.root).result, CommitReadiness.NOT_READY)
    self.artifact(0).write_bytes(b"confirmed content")
    self.snapshot(0).unlink()
    self.assertEqual(check_commit_readiness(self.root).result, CommitReadiness.NOT_READY)

  def test_missing_erased_snapshot_not_ready(self):
    self.confirm(state="erased")
    self.snapshot(0).unlink()
    self.assertEqual(check_commit_readiness(self.root).result, CommitReadiness.NOT_READY)

  def test_malformed_ledger_not_ready(self):
    self.ledger_path.write_bytes(b"{broken")
    result = check_pending_creates(self.root, Mock())
    self.assert_ready(result, False)
    self.assertTrue(result.diagnostics)

  def test_invalid_transport_keeps_pending(self):
    self.add_pending(1)
    result = check_pending_creates(self.root, Mock(return_value={"status": 200}))
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(self.read_ledger(), self.ledger)

  def test_invalid_slug_never_deletes_outside(self):
    self.add_pending(1)
    outside = self.root / "outside.md"
    outside.write_bytes(b"retain")
    self.ledger["pending_next_state"][IDS[0]]["slug"] = "../outside"
    self.write_ledger()
    with self.assertRaises(ValueError):
      terminal_collision.apply_collision(self.root, IDS[0])
    self.assertEqual(outside.read_bytes(), b"retain")
    self.assertEqual(self.read_ledger(), self.ledger)

  def test_collision_refuses_artifact_symlink_including_dangling(self):
    self.add_pending(1)
    self.artifact(0).unlink()
    outside = self.root / "outside.md"
    for exists in (False, True):
      with self.subTest(exists=exists):
        if exists:
          outside.write_bytes(b"retain")
        self.artifact(0).symlink_to(outside)
        result = self.check(["COLLISION"])
        self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
        self.assertTrue(self.artifact(0).is_symlink())
        self.assertEqual(self.read_ledger(), self.ledger)
        self.artifact(0).unlink()
    self.assertEqual(outside.read_bytes(), b"retain")

  def test_collision_refuses_symlink_parent_directory(self):
    self.add_pending(1)
    articles = self.root / "articles"
    external = self.root / "external"
    articles.rename(external)
    articles.symlink_to(external, target_is_directory=True)
    result = self.check(["COLLISION"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertTrue((external / self.artifact(0).name).exists())
    self.assertEqual(self.read_ledger(), self.ledger)

  def test_collision_refuses_directory_artifact(self):
    self.add_pending(1)
    self.artifact(0).unlink()
    self.artifact(0).mkdir()
    result = self.check(["COLLISION"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertTrue(self.artifact(0).is_dir())

  def test_collision_refuses_symlink_ledger_before_cleanup(self):
    self.add_pending(1)
    external = self.meta / "external.json"
    self.ledger_path.rename(external)
    self.ledger_path.symlink_to(external)
    with self.assertRaises(ValueError):
      terminal_collision.apply_collision(self.root, IDS[0])
    self.assertTrue(self.artifact(0).exists())

  def test_duplicate_pending_slugs_block_cleanup(self):
    self.add_pending(2)
    self.ledger["pending_next_state"][IDS[1]]["slug"] = SLUGS[0]
    self.write_ledger()
    result = self.check(["COLLISION", "COLLISION"])
    self.fetch.assert_not_called()
    self.assertTrue(result.diagnostics)
    self.assertTrue(self.artifact(0).exists())

  def test_collision_refuses_confirmed_slug_overlap(self):
    self.confirm(1)
    self.add_pending(1)
    self.ledger["pending_next_state"][IDS[0]]["slug"] = SLUGS[1]
    self.write_ledger()
    with self.assertRaises(ValueError):
      terminal_collision.apply_collision(self.root, IDS[0])
    self.assertEqual(self.artifact(1).read_bytes(), b"confirmed content")

  def test_recreate_collision_preserves_confirmed_state_and_snapshot(self):
    self.confirm(state="erased")
    confirmed = dict(self.ledger["articles"])
    self.ledger["pending_next_state"][IDS[0]] = {
      "operation": "RECREATE", "slug": SLUGS[1], "state": "active", "release": 2}
    self.artifact(1).write_bytes(b"failed recreation")
    self.write_ledger()
    terminal_collision.apply_collision(self.root, IDS[0])
    self.assertEqual(self.read_ledger()["articles"], confirmed)
    self.assertEqual(self.read_ledger()["pending_next_state"], {})
    self.assertEqual(self.snapshot(0).read_bytes(), b"confirmed content")
    self.assertFalse(self.artifact(1).exists())

  def test_missing_success_artifact_error_other_collision_applies(self):
    self.add_pending(2)
    self.artifact(0).unlink()
    result = self.check(["SUCCESS", "COLLISION"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(result.articles[IDS[1]].result, CreateResult.COLLISION)
    self.assertEqual(set(self.read_ledger()["pending_next_state"]), {IDS[0]})

  def test_ledger_changed_during_observation_refuses_apply(self):
    self.add_pending(1)

    def fetch(slug):
      self.ledger["pending_next_state"][IDS[0]]["release"] = 2
      self.write_ledger()
      return response(slug)

    result = check_pending_creates(self.root, fetch)
    self.assertTrue(result.diagnostics)
    self.assertFalse(self.snapshot(0).exists())
    self.assertEqual(self.read_ledger()["articles"], {})

  def test_recreate_collision_can_clean_previous_confirmed_slug(self):
    self.confirm(state="erased")
    confirmed = dict(self.ledger["articles"])
    self.ledger["pending_next_state"][IDS[0]] = {
      "operation": "RECREATE", "slug": SLUGS[0], "state": "active", "release": 1}
    self.artifact(0).write_bytes(b"failed recreation")
    self.write_ledger()
    terminal_collision.apply_collision(self.root, IDS[0])
    self.assertEqual(self.read_ledger()["articles"], confirmed)
    self.assertEqual(self.snapshot(0).read_bytes(), b"confirmed content")
    self.assertEqual(check_commit_readiness(self.root).result, CommitReadiness.COMMIT_READY)

  def test_success_refuses_symlink_snapshot_parent(self):
    self.add_pending(1)
    released = self.meta / "released"
    external = self.root / "external-released"
    released.rename(external)
    released.symlink_to(external, target_is_directory=True)
    result = self.check(["SUCCESS"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(list(external.iterdir()), [])
    self.assertEqual(self.read_ledger(), self.ledger)

  def test_collision_refuses_special_file(self):
    self.add_pending(1)
    self.artifact(0).unlink()
    os.mkfifo(self.artifact(0))
    result = self.check(["COLLISION"])
    self.assertEqual(result.articles[IDS[0]].result, CreateResult.ERROR)
    self.assertEqual(self.read_ledger(), self.ledger)

  def test_readiness_refuses_unmanaged_symlink_directory(self):
    external = self.root / "external-articles"
    external.mkdir()
    (external / "hidden.md").write_bytes(b"unmanaged")
    (self.root / "articles" / "nested").symlink_to(external, target_is_directory=True)
    result = check_commit_readiness(self.root)
    self.assertEqual(result.result, CommitReadiness.NOT_READY)
    self.assertTrue(any("symlink" in item for item in result.diagnostics))

  def test_malformed_confirmed_state_does_not_hide_inconsistency(self):
    self.confirm()
    for key, value in (("state", "hold"), ("successful_release", True),
                       ("successful_zenn_observation", None), ("slug", "../outside")):
      with self.subTest(key=key):
        original = self.ledger["articles"][IDS[0]][key]
        self.ledger["articles"][IDS[0]][key] = value
        self.write_ledger()
        result = check_commit_readiness(self.root)
        self.assertEqual(result.result, CommitReadiness.NOT_READY)
        self.assertTrue(result.diagnostics)
        self.ledger["articles"][IDS[0]][key] = original
