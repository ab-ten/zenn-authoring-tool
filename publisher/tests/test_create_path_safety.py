"""直接 apply と単一 helper のパス安全性と状態保持を検証します。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import terminal_create
from checker.create_observation import ArticleResponse, CreateResult, check_single_create
from checker.errors import LocalApplyError


ARTICLE_ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER_ID = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "p0-create-test-article"
OBSERVATION = {key: "2026-10-10T00:00:00+09:00" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}


class CreatePathSafetyTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.base = Path(temp.name)
    self.root = self.base / "deploy"
    self.meta = self.root / ".zenn-authoring"
    self.released = self.meta / "released"
    self.released.mkdir(parents=True)
    self.articles = self.root / "articles"
    self.articles.mkdir()
    self.ledger_path = self.meta / "ledger.json"
    self.config = self.meta / "config.json"
    self.artifact = self.articles / f"{SLUG}.md"
    self.snapshot = self.released / f"{ARTICLE_ID}.md"
    self.snapshot.write_bytes(b"existing snapshot")
    self.artifact.write_bytes(b"pending content")
    self.config.write_bytes(b'{"zenn_user":{"id":2}}')
    self.ledger = {
      "articles": {OTHER_ID: {
        "slug": "existing-article-slug", "state": "active", "successful_release": 1,
        "successful_zenn_observation": OBSERVATION}},
      "pending_next_state": {ARTICLE_ID: {
        "operation": "CREATE", "slug": SLUG, "state": "active", "release": 1}}}
    self.ledger_content = json.dumps(self.ledger).encode()
    self.ledger_path.write_bytes(self.ledger_content)
    self.refs = {}

  def external(self, name, content):
    path = self.base / name
    path.write_bytes(content)
    self.refs[path] = content
    return path

  def fetch(self):
    return Mock(return_value=ArticleResponse(200, {"article": {
      "slug": SLUG, "user": {"id": 2}, **OBSERVATION}}))

  def assert_rejected(self, *, direct=True, readable_ledger=True, no_fetch=False):
    fetch = self.fetch()
    with patch.object(terminal_create, "atomic_replace_bytes") as replace:
      if direct:
        with self.assertRaises((LocalApplyError, OSError)):
          terminal_create.apply_create_success(self.root, ARTICLE_ID, OBSERVATION)
      result = check_single_create(self.root, ARTICLE_ID, fetch)
      self.assertEqual(result.result, CreateResult.ERROR)
      self.assertTrue(result.detail)
      replace.assert_not_called()
    if no_fetch:
      fetch.assert_not_called()
    if readable_ledger:
      self.assertEqual(self.ledger_path.read_bytes(), self.ledger_content)
      self.assertEqual(json.loads(self.ledger_path.read_bytes()), self.ledger)
    for path, content in self.refs.items():
      self.assertEqual(path.read_bytes(), content)

  def test_released_parent_symlink_rejected_without_external_changes(self):
    external = self.base / "outside-released"
    self.released.rename(external)
    self.released.symlink_to(external, target_is_directory=True)
    self.refs[external / self.snapshot.name] = b"existing snapshot"
    self.assert_rejected()

  def test_snapshot_symlink_rejected_without_external_changes(self):
    external = self.external("outside-snapshot.md", b"external snapshot")
    self.snapshot.unlink()
    self.snapshot.symlink_to(external)
    self.assert_rejected()
    self.assertTrue(self.snapshot.is_symlink())

  def test_dangling_snapshot_symlink_rejected(self):
    external = self.base / "absent-snapshot.md"
    self.snapshot.unlink()
    self.snapshot.symlink_to(external)
    self.assert_rejected()
    self.assertFalse(external.exists())
    self.assertTrue(self.snapshot.is_symlink())

  def test_artifact_parent_symlink_rejected_without_external_changes(self):
    external = self.base / "outside-articles"
    self.articles.rename(external)
    self.articles.symlink_to(external, target_is_directory=True)
    self.refs[external / self.artifact.name] = b"pending content"
    self.refs[self.snapshot] = b"existing snapshot"
    self.assert_rejected()

  def test_artifact_symlink_rejected_without_external_changes(self):
    external = self.external("outside-article.md", b"external content")
    self.artifact.unlink()
    self.artifact.symlink_to(external)
    self.refs[self.snapshot] = b"existing snapshot"
    self.assert_rejected()

  def test_dangling_artifact_symlink_rejected(self):
    external = self.base / "absent-article.md"
    self.artifact.unlink()
    self.artifact.symlink_to(external)
    self.assert_rejected()
    self.assertFalse(external.exists())

  def test_ledger_symlink_rejected_before_snapshot_write(self):
    external = self.external("outside-ledger.json", self.ledger_content)
    self.ledger_path.unlink()
    self.ledger_path.symlink_to(external)
    self.refs[self.snapshot] = b"existing snapshot"
    self.assert_rejected(no_fetch=True)

  def test_dangling_ledger_symlink_rejected(self):
    external = self.base / "absent-ledger.json"
    self.ledger_path.unlink()
    self.ledger_path.symlink_to(external)
    self.refs[self.snapshot] = b"existing snapshot"
    self.assert_rejected(readable_ledger=False, no_fetch=True)
    self.assertFalse(external.exists())

  def test_config_symlink_returns_error_before_fetch(self):
    external = self.external("outside-config.json", self.config.read_bytes())
    self.config.unlink()
    self.config.symlink_to(external)
    self.refs[self.snapshot] = b"existing snapshot"
    self.assert_rejected(direct=False, no_fetch=True)

  def test_dangling_config_symlink_returns_error_before_fetch(self):
    external = self.base / "absent-config.json"
    self.config.unlink()
    self.config.symlink_to(external)
    self.refs[self.snapshot] = b"existing snapshot"
    self.assert_rejected(direct=False, no_fetch=True)
    self.assertFalse(external.exists())

  def test_special_files_rejected_before_reads_or_writes(self):
    for path in (self.snapshot, self.artifact, self.ledger_path, self.config):
      with self.subTest(path=path.name):
        content = path.read_bytes()
        path.unlink()
        os.mkfifo(path)
        try:
          self.assert_rejected(
            direct=path != self.config, readable_ledger=path != self.ledger_path,
            no_fetch=path in (self.config, self.ledger_path))
        finally:
          path.unlink()
          path.write_bytes(content)
        self.assertEqual(self.ledger_path.read_bytes(), self.ledger_content)
        self.assertEqual(self.snapshot.read_bytes(), b"existing snapshot")

  def test_non_directory_parents_rejected_before_writes(self):
    for parent in (self.released, self.articles, self.meta):
      with self.subTest(parent=parent.name):
        backup = self.base / f"backup-{parent.name}"
        parent.rename(backup)
        parent.write_bytes(b"not a directory")
        try:
          self.assert_rejected(readable_ledger=parent != self.meta,
                               no_fetch=parent == self.meta)
          self.assertEqual(parent.read_bytes(), b"not a directory")
        finally:
          parent.unlink()
          backup.rename(parent)
        self.assertEqual(self.ledger_path.read_bytes(), self.ledger_content)
        self.assertEqual(self.snapshot.read_bytes(), b"existing snapshot")

  def test_management_parent_symlink_rejected(self):
    external = self.base / "outside-management"
    self.meta.rename(external)
    self.meta.symlink_to(external, target_is_directory=True)
    self.refs[external / "ledger.json"] = self.ledger_content
    self.refs[external / "released" / self.snapshot.name] = b"existing snapshot"
    self.assert_rejected(no_fetch=True)

  def test_snapshot_directory_rejected_before_atomic_replacement(self):
    self.snapshot.unlink()
    self.snapshot.mkdir()
    self.assert_rejected()
    self.assertTrue(self.snapshot.is_dir())

  def test_exception_import_remains_compatible(self):
    self.assertIs(terminal_create.LocalApplyError, LocalApplyError)
