"""DELETE 判定、terminal apply、復旧とパス安全性を検証します。"""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import terminal_delete
from checker.create_observation import ArticleResponse, CreateResult
from checker.delete_observation import classify_delete_response, observe_delete_result
from checker.errors import LocalApplyError


ARTICLE_ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER_ID = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "p0-delete-test-article"
OBSERVATION = {key: "2026-10-10T00:00:00+09:00" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}


class DeleteTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    (self.root / "articles").mkdir()
    (self.root / ".zenn-authoring" / "released").mkdir(parents=True)
    self.artifact = self.root / "articles" / f"{SLUG}.md"
    self.snapshot = self.root / ".zenn-authoring" / "released" / f"{ARTICLE_ID}.md"
    self.ledger_path = self.root / ".zenn-authoring" / "ledger.json"
    self.snapshot.write_bytes(b"---\r\ntitle: retained\r\n---\r\n\x00\xff\n")
    self.ledger = {
      "articles": {ARTICLE_ID: {
        "state": "active", "slug": SLUG, "successful_release": 3,
        "successful_zenn_observation": OBSERVATION}},
      "pending_next_state": {ARTICLE_ID: {
        "operation": "DELETE", "state": "erased", "slug": SLUG, "release": 3}}}
    self.save()

  def save(self):
    self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")

  def classify(self, response, owner=2):
    pending = terminal_delete.read_delete_pending(self.ledger, ARTICLE_ID)
    return classify_delete_response(response, pending, owner)

  def response(self, owner=2, **metadata):
    return ArticleResponse(200, {"article": {
      "slug": SLUG, "user": {"id": owner}, **metadata}})

  def apply(self):
    terminal_delete.apply_delete_success(self.root, ARTICLE_ID)

  def assert_rejected(self):
    before = self.ledger_path.read_bytes()
    snapshot = self.snapshot.read_bytes() if self.snapshot.is_file() else None
    with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
      with self.assertRaises((LocalApplyError, OSError)):
        self.apply()
      replace.assert_not_called()
    self.assertEqual(self.ledger_path.read_bytes(), before)
    if snapshot is not None:
      self.assertEqual(self.snapshot.read_bytes(), snapshot)

  def test_404_success_without_metadata(self):
    for payload in (None, {}, [], "invalid"):
      with self.subTest(payload=payload):
        result = self.classify(ArticleResponse(404, payload))
        self.assertEqual(result.result, CreateResult.SUCCESS)
        self.assertIsNone(result.observation)

  def test_other_owner_success_without_timestamps(self):
    result = self.classify(self.response(9))
    self.assertEqual(result.result, CreateResult.SUCCESS)
    self.assertIsNone(result.observation)

  def test_own_owner_pending_without_local_writes(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
      for metadata in ({}, OBSERVATION, {**OBSERVATION, "body_updated_at": "changed"}):
        self.assertEqual(self.classify(self.response(**metadata)).result, CreateResult.PENDING)
      replace.assert_not_called()
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)

  def test_invalid_owner_no_username_fallback(self):
    for owner in (None, True, "2", 0, -1, 2.0):
      with self.subTest(owner=owner):
        self.assertEqual(self.classify(self.response(owner)).result, CreateResult.ERROR)
    for user in (None, [], {}, {"username": "configured-user"}):
      with self.subTest(user=user):
        self.assertEqual(self.classify(ArticleResponse(200, {"article": {
          "slug": SLUG, "user": user}})).result, CreateResult.ERROR)

  def test_invalid_configured_owner(self):
    for owner in (None, True, "2", 0, -1):
      for status in (200, 404):
        with self.subTest(owner=owner, status=status):
          self.assertEqual(self.classify(ArticleResponse(status), owner).result, CreateResult.ERROR)

  def test_invalid_schema_slug_and_transport(self):
    for response in (None, {}, ArticleResponse(200), ArticleResponse(200, []),
                     ArticleResponse(200, {"article": []}),
                     ArticleResponse(200, {"article": {"user": {"id": 9}}}),
                     ArticleResponse(200, {"article": {
                       "slug": "different-article-slug", "user": {"id": 9}}})):
      with self.subTest(response=response):
        self.assertEqual(self.classify(response).result, CreateResult.ERROR)

  def test_http_status_rules(self):
    for status, expected in ((401, CreateResult.ERROR), (403, CreateResult.ERROR),
                             (408, CreateResult.ERROR), (429, CreateResult.ERROR),
                             (500, CreateResult.ERROR), (503, CreateResult.ERROR),
                             (302, CreateResult.UNKNOWN), (204, CreateResult.UNKNOWN),
                             (True, CreateResult.ERROR), ("404", CreateResult.ERROR)):
      with self.subTest(status=status):
        self.assertEqual(self.classify(ArticleResponse(status)).result, expected)

  def test_communication_failures_preserve_local_state(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    pending = terminal_delete.read_delete_pending(self.ledger, ARTICLE_ID)
    for failure in (OSError("network failure"), TimeoutError("timeout"), ValueError("invalid JSON")):
      with self.subTest(failure=failure):
        fetch = Mock(side_effect=failure)
        result = observe_delete_result(pending, 2, fetch)
        self.assertEqual(result.result, CreateResult.ERROR)
        fetch.assert_called_once_with(SLUG)
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)

  def test_observation_helper_does_not_apply(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    fetch = Mock(return_value=ArticleResponse(404))
    result = observe_delete_result(self.ledger["pending_next_state"][ARTICLE_ID], 2, fetch)
    self.assertEqual(result.result, CreateResult.SUCCESS)
    fetch.assert_called_once_with(SLUG)
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)

  def test_error_classification_preserves_local_state(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
      self.assertEqual(self.classify(self.response(None)).result, CreateResult.ERROR)
      replace.assert_not_called()
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)

  def test_pending_inconsistencies(self):
    original = copy.deepcopy(self.ledger)
    for key, value in (("operation", "UPDATE"), ("operation", None), ("state", "active"),
                       ("slug", "another-article-slug"), ("slug", "../unsafe"), ("slug", None),
                       ("release", 4), ("release", 2), ("release", 0),
                       ("release", True), ("release", "3"), ("release", None)):
      with self.subTest(key=key, value=value):
        self.ledger = copy.deepcopy(original)
        self.ledger["pending_next_state"][ARTICLE_ID][key] = value
        self.save()
        self.assert_rejected()

  def test_confirmed_inconsistencies(self):
    original = copy.deepcopy(self.ledger)
    for key, value in (("state", "erased"), ("state", "hold"), ("state", None),
                       ("slug", "another-article-slug"), ("successful_release", True),
                       ("successful_release", 0), ("successful_release", "3"),
                       ("successful_zenn_observation", None), ("successful_zenn_observation", {}),
                       ("successful_zenn_observation", {**OBSERVATION, "published_at": False})):
      with self.subTest(key=key, value=value):
        self.ledger = copy.deepcopy(original)
        self.ledger["articles"][ARTICLE_ID][key] = value
        self.save()
        self.assert_rejected()

  def test_invalid_article_id(self):
    for article_id in ("../invalid", "", None):
      with self.subTest(article_id=article_id):
        with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
          with self.assertRaises(LocalApplyError):
            terminal_delete.apply_delete_success(self.root, article_id)
          replace.assert_not_called()

  def test_invalid_ledger_maps_and_missing_entries(self):
    for ledger in ([], {}, {"articles": [], "pending_next_state": {}},
                   {"articles": {}, "pending_next_state": {}},
                   {**self.ledger, "pending_next_state": {}},
                   {**self.ledger, "articles": {ARTICLE_ID: None}}):
      with self.subTest(ledger=ledger):
        self.ledger = ledger
        self.save()
        self.assert_rejected()

  def test_success_preserves_history_snapshot_and_other_entries(self):
    other = {"state": "active", "slug": "another-article-slug", "successful_release": 1,
             "successful_zenn_observation": OBSERVATION}
    other_pending = {"operation": "UPDATE", "state": "active", "release": 2}
    self.ledger["articles"][OTHER_ID] = other
    self.ledger["pending_next_state"][OTHER_ID] = other_pending
    self.ledger["extra"] = {"retained": True}
    self.save()
    snapshot = self.snapshot.read_bytes()
    snapshot_stat = self.snapshot.stat()
    real_replace = terminal_delete.atomic_replace_bytes

    def record(target, content):
      self.assertEqual(target, self.ledger_path)
      self.assertIn(ARTICLE_ID, json.loads(self.ledger_path.read_bytes())["pending_next_state"])
      self.assertEqual(self.snapshot.read_bytes(), snapshot)
      next_ledger = json.loads(content)
      self.assertEqual(next_ledger["articles"][ARTICLE_ID]["state"], "erased")
      self.assertNotIn(ARTICLE_ID, next_ledger["pending_next_state"])
      return real_replace(target, content)

    with patch.object(terminal_delete, "atomic_replace_bytes", side_effect=record) as replace:
      self.apply()
      replace.assert_called_once()
    expected = copy.deepcopy(self.ledger)
    expected["articles"][ARTICLE_ID]["state"] = "erased"
    del expected["pending_next_state"][ARTICLE_ID]
    self.assertEqual(json.loads(self.ledger_path.read_bytes()), expected)
    self.assertEqual(self.snapshot.read_bytes(), snapshot)
    self.assertEqual(self.snapshot.stat().st_ino, snapshot_stat.st_ino)
    self.assertEqual(self.snapshot.stat().st_mtime_ns, snapshot_stat.st_mtime_ns)

  def test_remaining_artifact_rejected(self):
    self.artifact.write_bytes(b"retained deploy content")
    self.assert_rejected()
    self.assertEqual(self.artifact.read_bytes(), b"retained deploy content")

  def test_missing_snapshot_rejected(self):
    self.snapshot.unlink()
    self.assert_rejected()
    self.assertFalse(self.snapshot.exists())

  def test_symlink_and_dangling_symlink_paths(self):
    for path in (self.artifact, self.snapshot, self.ledger_path):
      content = path.read_bytes() if path.exists() else None
      for dangling in (False, True):
        with self.subTest(path=path.name, dangling=dangling):
          external = self.root / "external"
          if not dangling:
            external.write_bytes(content or b"external content")
          if path.exists():
            path.unlink()
          path.symlink_to(external)
          try:
            with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
              with self.assertRaises(LocalApplyError):
                self.apply()
              replace.assert_not_called()
            if not dangling:
              self.assertEqual(external.read_bytes(), content or b"external content")
            else:
              self.assertFalse(external.exists())
          finally:
            path.unlink()
            if content is not None:
              path.write_bytes(content)
            if external.exists():
              external.unlink()

  def test_fifo_and_directory_paths(self):
    for path in (self.artifact, self.snapshot, self.ledger_path):
      content = path.read_bytes() if path.exists() else None
      for special in ("fifo", "directory"):
        with self.subTest(path=path.name, special=special):
          if path.exists():
            path.unlink()
          os.mkfifo(path) if special == "fifo" else path.mkdir()
          try:
            with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
              with self.assertRaises((LocalApplyError, OSError)):
                self.apply()
              replace.assert_not_called()
          finally:
            path.unlink() if special == "fifo" else path.rmdir()
            if content is not None:
              path.write_bytes(content)

  def test_unsafe_parents(self):
    for parent in (self.artifact.parent, self.snapshot.parent, self.ledger_path.parent):
      for kind in ("symlink", "file"):
        with self.subTest(parent=parent.name, kind=kind):
          backup = self.root / "backup"
          parent.rename(backup)
          if kind == "symlink":
            parent.symlink_to(backup, target_is_directory=True)
          else:
            parent.write_bytes(b"not directory")
          try:
            with patch.object(terminal_delete, "atomic_replace_bytes") as replace:
              with self.assertRaises((LocalApplyError, OSError)):
                self.apply()
              replace.assert_not_called()
          finally:
            parent.unlink()
            backup.rename(parent)

  def test_atomic_replace_failure_then_reobserve_and_retry(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    with patch("checker.atomic_write.os.replace", side_effect=OSError("interrupted")):
      with self.assertRaises(OSError):
        self.apply()
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)
    self.assertEqual(list(self.ledger_path.parent.glob(".ledger.json.*.tmp")), [])
    self.assertEqual(self.classify(ArticleResponse(404)).result, CreateResult.SUCCESS)
    self.apply()
    self.assertEqual(json.loads(self.ledger_path.read_bytes())["articles"][ARTICLE_ID]["state"],
                     "erased")
    self.assertEqual(self.snapshot.read_bytes(), before[1])

  def test_no_reapply_after_commit(self):
    self.apply()
    self.assert_rejected()


if __name__ == "__main__":
  unittest.main()
