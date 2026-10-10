"""RECREATE 判定、terminal apply、中断復旧とパス安全性を検証します。"""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import terminal_recreate, terminal_collision
from checker.create_observation import ArticleResponse, CreateResult
from checker.errors import LocalApplyError
from checker.recreate_observation import classify_recreate_response, observe_recreate_result


ARTICLE_ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER_ID = "95660b64-4620-4292-b509-02f8b6f49599"
OLD_SLUG = "old-recreate-article"
SLUG = "new-recreate-article"
BASELINE = {key: "2026-10-10T00:00:00+09:00" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}
CURRENT = {**BASELINE, "body_updated_at": "2026-10-11T00:00:00+09:00"}


class RecreateTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    (self.root / "articles").mkdir()
    (self.root / ".zenn-authoring" / "released").mkdir(parents=True)
    self.artifact = self.root / "articles" / f"{SLUG}.md"
    self.snapshot = self.root / ".zenn-authoring" / "released" / f"{ARTICLE_ID}.md"
    self.ledger_path = self.root / ".zenn-authoring" / "ledger.json"
    self.artifact.write_bytes(b"new content\r\n\x00\xff")
    self.snapshot.write_bytes(b"old content\n")
    self.ledger = {
      "articles": {ARTICLE_ID: {
        "state": "erased", "slug": OLD_SLUG, "successful_release": 3,
        "successful_zenn_observation": BASELINE}},
      "pending_next_state": {ARTICLE_ID: {
        "operation": "RECREATE", "state": "active", "slug": SLUG, "release": 4}}}
    self.save()

  def save(self):
    self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")

  def read(self):
    return json.loads(self.ledger_path.read_bytes())

  def response(self, owner=2, observation=CURRENT):
    return ArticleResponse(200, {"article": {
      "slug": self.ledger["pending_next_state"][ARTICLE_ID]["slug"],
      "user": {"id": owner}, **observation}})

  def classify(self, response=None, owner=2):
    pending = terminal_recreate.read_recreate_pending(self.ledger, ARTICLE_ID)
    return classify_recreate_response(response or self.response(), pending, owner)

  def apply(self):
    terminal_recreate.apply_recreate_success(self.root, ARTICLE_ID, CURRENT)

  def assert_rejected(self, apply=None):
    before = self.ledger_path.read_bytes()
    with patch.object(terminal_recreate, "atomic_replace_bytes") as replace:
      with self.assertRaises((LocalApplyError, OSError)):
        (apply or self.apply)()
      replace.assert_not_called()
    self.assertEqual(self.ledger_path.read_bytes(), before)

  def test_404_pending(self):
    self.assertEqual(self.classify(ArticleResponse(404)).result, CreateResult.PENDING)

  def test_own_owner_success(self):
    result = self.classify()
    self.assertEqual(result.result, CreateResult.SUCCESS)
    self.assertEqual(result.observation, CURRENT)

  def test_other_owner_collision(self):
    result = self.classify(self.response(owner=9))
    self.assertEqual(result.result, CreateResult.COLLISION)
    self.assertIsNone(result.observation)

  def test_invalid_owner(self):
    for owner in (None, True, "2", 0, -1, 2.0):
      with self.subTest(owner=owner):
        self.assertEqual(self.classify(self.response(owner)).result, CreateResult.ERROR)
        self.assertEqual(self.classify(owner=owner).result, CreateResult.ERROR)

  def test_invalid_schema_slug_transport(self):
    pending = self.ledger["pending_next_state"][ARTICLE_ID]
    for response in (None, {}, ArticleResponse(200), ArticleResponse(200, []),
                     ArticleResponse(200, {"article": []}), ArticleResponse(200, {
                       "article": {"slug": OLD_SLUG, "user": {"id": 2}, **CURRENT}})):
      with self.subTest(response=response):
        self.assertEqual(classify_recreate_response(response, pending, 2).result, CreateResult.ERROR)

  def test_invalid_metadata_for_either_owner(self):
    for owner in (2, 9):
      for key in CURRENT:
        for value in (None, "", True, 3):
          with self.subTest(owner=owner, key=key, value=value):
            self.assertEqual(self.classify(self.response(owner, {**CURRENT, key: value})).result,
                             CreateResult.ERROR)

  def test_http_status_rules(self):
    for status, expected in ((401, "ERROR"), (403, "ERROR"), (408, "ERROR"),
                             (429, "ERROR"), (500, "ERROR"), (503, "ERROR"),
                             (302, "UNKNOWN"), (204, "UNKNOWN"),
                             (True, "ERROR"), ("404", "ERROR")):
      with self.subTest(status=status):
        self.assertEqual(self.classify(ArticleResponse(status)).result, expected)

  def test_network_errors_preserve_state(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes(), self.artifact.read_bytes()
    pending = self.ledger["pending_next_state"][ARTICLE_ID]
    for failure in (OSError("network"), TimeoutError("timeout"), ValueError("invalid JSON")):
      fetch = Mock(side_effect=failure)
      self.assertEqual(observe_recreate_result(pending, 2, fetch).result, CreateResult.ERROR)
      fetch.assert_called_once_with(SLUG)
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes(),
                      self.artifact.read_bytes()), before)

  def test_only_pending_slug_observed_without_local_writes(self):
    fetch = Mock(return_value=self.response())
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    result = observe_recreate_result(self.ledger["pending_next_state"][ARTICLE_ID], 2, fetch)
    fetch.assert_called_once_with(SLUG)
    self.assertEqual(result.result, CreateResult.SUCCESS)
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)

  def test_invalid_confirmed(self):
    original = copy.deepcopy(self.ledger)
    for key, value in (("state", "active"), ("state", "hold"), ("slug", "../bad"),
                       ("successful_release", True), ("successful_release", "3"),
                       ("successful_release", 0), ("successful_zenn_observation", {})):
      with self.subTest(key=key, value=value):
        self.ledger = copy.deepcopy(original)
        self.ledger["articles"][ARTICLE_ID][key] = value
        self.save()
        self.assert_rejected()

  def test_missing_confirmed_pending_and_bad_maps(self):
    for ledger in ({}, {"articles": [], "pending_next_state": {}},
                   {"articles": {}, "pending_next_state": self.ledger["pending_next_state"]},
                   {"articles": self.ledger["articles"], "pending_next_state": {}}):
      with self.subTest(ledger=ledger):
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        self.assert_rejected()

  def test_invalid_pending(self):
    original = copy.deepcopy(self.ledger)
    for key, value in (("operation", "CREATE"), ("operation", "UPDATE"), ("state", "erased"),
                       ("slug", "../bad"), ("slug", True), ("release", 2), ("release", 5),
                       ("release", True), ("release", "4"), ("release", 4.0), ("release", None)):
      with self.subTest(key=key, value=value):
        self.ledger = copy.deepcopy(original)
        self.ledger["pending_next_state"][ARTICLE_ID][key] = value
        self.save()
        self.assert_rejected()

  def test_invalid_article_id(self):
    for article_id in ("../bad", True, None, ARTICLE_ID.upper()):
      with self.subTest(article_id=article_id):
        self.assert_rejected(lambda: terminal_recreate.apply_recreate_success(
          self.root, article_id, CURRENT))

  def test_invalid_current_observation(self):
    for observation in (None, {}, {**CURRENT, "published_at": True}):
      with self.subTest(observation=observation):
        self.assert_rejected(lambda: terminal_recreate.apply_recreate_success(
          self.root, ARTICLE_ID, observation))

  def test_release_n_success(self):
    self.ledger["pending_next_state"][ARTICLE_ID]["release"] = 3
    self.artifact.write_bytes(self.snapshot.read_bytes())
    self.save()
    self.apply()
    self.assertEqual(self.read()["articles"][ARTICLE_ID]["successful_release"], 3)

  def test_changed_slug_success_preserves_identity_and_others(self):
    other = {"state": "erased", "slug": "other-recreate-article", "successful_release": 2,
             "successful_zenn_observation": BASELINE}
    other_pending = {"operation": "RECREATE", "state": "active",
                     "slug": "other-new-recreate", "release": 3}
    self.ledger["articles"][OTHER_ID] = other
    self.ledger["pending_next_state"][OTHER_ID] = other_pending
    self.ledger["extra"] = {"retained": True}
    self.save()
    real_replace = terminal_recreate.atomic_replace_bytes
    calls = []

    def record(target, content):
      calls.append(target)
      self.assertEqual(self.read()["articles"][ARTICLE_ID]["state"], "erased")
      self.assertIn(ARTICLE_ID, self.read()["pending_next_state"])
      if target == self.ledger_path:
        self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
      return real_replace(target, content)

    with patch.object(terminal_recreate, "atomic_replace_bytes", side_effect=record):
      self.apply()
    ledger = self.read()
    self.assertEqual(calls, [self.snapshot, self.ledger_path])
    self.assertEqual(set(ledger["articles"]), {ARTICLE_ID, OTHER_ID})
    self.assertEqual(ledger["articles"][ARTICLE_ID], {
      "state": "active", "slug": SLUG, "successful_release": 4,
      "successful_zenn_observation": CURRENT})
    self.assertEqual(ledger["articles"][OTHER_ID], other)
    self.assertEqual(ledger["pending_next_state"], {OTHER_ID: other_pending})
    self.assertEqual(ledger["extra"], {"retained": True})

  def test_same_slug_success(self):
    self.ledger["articles"][ARTICLE_ID]["slug"] = SLUG
    self.save()
    self.apply()
    self.assertEqual(self.read()["articles"][ARTICLE_ID]["slug"], SLUG)
    self.assertEqual(self.read()["articles"][ARTICLE_ID]["state"], "active")
    self.assertEqual(self.read()["pending_next_state"], {})
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())

  def test_interrupted_recovery_release_n(self):
    self.recover_success(3)

  def test_interrupted_recovery_release_n_plus_one(self):
    self.recover_success(4)

  def recover_success(self, release):
    self.ledger["pending_next_state"][ARTICLE_ID]["release"] = release
    if release == 3:
      self.artifact.write_bytes(self.snapshot.read_bytes())
    self.save()
    before = self.ledger_path.read_bytes()
    real_replace = terminal_recreate.atomic_replace_bytes

    def interrupt(target, content):
      if target == self.ledger_path:
        raise OSError("interrupted before ledger commit")
      return real_replace(target, content)

    with patch.object(terminal_recreate, "atomic_replace_bytes", side_effect=interrupt):
      with self.assertRaises(OSError):
        self.apply()
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    fetch = Mock(return_value=self.response())
    result = observe_recreate_result(self.read()["pending_next_state"][ARTICLE_ID], 2, fetch)
    self.assertEqual(result.result, CreateResult.SUCCESS)
    fetch.assert_called_once_with(SLUG)
    with patch("checker.atomic_write.os.replace", wraps=os.replace) as replace:
      terminal_recreate.apply_recreate_success(self.root, ARTICLE_ID, result.observation)
      self.assertEqual(replace.call_count, 1)
      self.assertEqual(Path(replace.call_args.args[1]), self.ledger_path)
    self.assertEqual(self.read()["pending_next_state"], {})
    self.assertEqual(self.read()["articles"][ARTICLE_ID]["successful_release"], release)

  def test_identical_snapshot_noop(self):
    self.snapshot.write_bytes(self.artifact.read_bytes())
    with patch("checker.atomic_write.os.replace", wraps=os.replace) as replace:
      self.apply()
      self.assertEqual(replace.call_count, 1)
      self.assertEqual(Path(replace.call_args.args[1]), self.ledger_path)

  def test_missing_artifact_preserves_pending_and_snapshot(self):
    self.artifact.unlink()
    self.assert_rejected()
    self.assertEqual(self.snapshot.read_bytes(), b"old content\n")

  def test_snapshot_replace_failure(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    with patch("checker.atomic_write.os.replace", side_effect=OSError("failure")):
      with self.assertRaises(OSError):
        self.apply()
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)
    self.assertEqual(list(self.snapshot.parent.glob("*.tmp")), [])

  def test_double_apply_rejected(self):
    self.apply()
    self.assert_rejected()

  def test_local_slug_overlap_rejected(self):
    for collection in ("articles", "pending_next_state"):
      for slug in (OLD_SLUG, SLUG):
        with self.subTest(collection=collection, slug=slug):
          self.ledger[collection][OTHER_ID] = {"slug": slug}
          self.save()
          self.assert_rejected()
          before = self.artifact.read_bytes()
          with self.assertRaises(LocalApplyError):
            terminal_collision.apply_collision(self.root, ARTICLE_ID)
          self.assertEqual(self.artifact.read_bytes(), before)
          del self.ledger[collection][OTHER_ID]

  def test_symlink_and_dangling_targets(self):
    for path in (self.artifact, self.snapshot, self.ledger_path):
      content = path.read_bytes()
      for dangling in (False, True):
        with self.subTest(path=path.name, dangling=dangling):
          external = self.root / "external"
          if not dangling:
            external.write_bytes(content)
          path.unlink()
          path.symlink_to(external)
          try:
            with patch.object(terminal_recreate, "atomic_replace_bytes") as replace:
              with self.assertRaises(LocalApplyError):
                self.apply()
              replace.assert_not_called()
            self.assertTrue(path.is_symlink())
            if not dangling:
              self.assertEqual(external.read_bytes(), content)
            else:
              self.assertFalse(external.exists())
          finally:
            path.unlink()
            path.write_bytes(content)
            external.unlink(missing_ok=True)

  def test_special_files_and_directory_targets(self):
    for path in (self.artifact, self.snapshot, self.ledger_path):
      content = path.read_bytes()
      for kind in ("fifo", "directory"):
        with self.subTest(path=path.name, kind=kind):
          path.unlink()
          os.mkfifo(path) if kind == "fifo" else path.mkdir()
          try:
            with patch.object(terminal_recreate, "atomic_replace_bytes") as replace:
              with self.assertRaises((LocalApplyError, OSError)):
                self.apply()
              replace.assert_not_called()
          finally:
            path.unlink() if kind == "fifo" else path.rmdir()
            path.write_bytes(content)

  def test_unsafe_parents(self):
    for parent in (self.artifact.parent, self.snapshot.parent, self.ledger_path.parent):
      for kind in ("symlink", "dangling", "file", "fifo"):
        with self.subTest(parent=parent.name, kind=kind):
          backup = self.root / "backup"
          parent.rename(backup)
          if kind in ("symlink", "dangling"):
            parent.symlink_to(backup if kind == "symlink" else self.root / "absent",
                              target_is_directory=True)
          elif kind == "fifo":
            os.mkfifo(parent)
          else:
            parent.write_bytes(b"not directory")
          try:
            with patch.object(terminal_recreate, "atomic_replace_bytes") as replace:
              with self.assertRaises((LocalApplyError, OSError)):
                self.apply()
              replace.assert_not_called()
          finally:
            parent.unlink()
            backup.rename(parent)

  def test_collision_preserves_confirmed_snapshot_and_other_entries(self):
    self.ledger["articles"][OTHER_ID] = {"slug": "other-article-slug", "state": "active"}
    self.ledger["pending_next_state"][OTHER_ID] = {"slug": "other-pending-slug"}
    for slug in (OLD_SLUG, SLUG):
      with self.subTest(slug=slug):
        self.ledger["pending_next_state"][ARTICLE_ID]["slug"] = slug
        artifact = self.root / "articles" / f"{slug}.md"
        artifact.write_bytes(b"failed recreation")
        self.save()
        before = copy.deepcopy(self.ledger)
        terminal_collision.apply_collision(self.root, ARTICLE_ID)
        del before["pending_next_state"][ARTICLE_ID]
        self.assertEqual(self.read(), before)
        self.assertFalse(artifact.exists())
        self.assertEqual(self.snapshot.read_bytes(), b"old content\n")

  def test_collision_interrupted_cleanup_recovery(self):
    before = self.ledger_path.read_bytes()
    with patch.object(terminal_collision, "atomic_replace_bytes", side_effect=OSError("interrupted")):
      with self.assertRaises(OSError):
        terminal_collision.apply_collision(self.root, ARTICLE_ID)
    self.assertFalse(self.artifact.exists())
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(self.classify(self.response(9)).result, CreateResult.COLLISION)
    terminal_collision.apply_collision(self.root, ARTICLE_ID)
    self.assertEqual(self.read()["articles"], self.ledger["articles"])
    self.assertEqual(self.snapshot.read_bytes(), b"old content\n")
    self.assertEqual(self.read()["pending_next_state"], {})
    with self.assertRaises(LocalApplyError):
      terminal_collision.apply_collision(self.root, ARTICLE_ID)

  def test_collision_invalid_release_preserves_artifact(self):
    for release in (2, 5, True, "4"):
      with self.subTest(release=release):
        self.ledger["pending_next_state"][ARTICLE_ID]["release"] = release
        self.save()
        with self.assertRaises(LocalApplyError):
          terminal_collision.apply_collision(self.root, ARTICLE_ID)
        self.assertEqual(self.read(), self.ledger)
        self.assertTrue(self.artifact.is_file())


if __name__ == "__main__":
  unittest.main()
