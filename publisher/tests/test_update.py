"""UPDATE 判定、terminal apply、復旧とパス安全性を検証します。"""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checker import terminal_update
from checker.create_observation import ArticleResponse, CreateResult
from checker.errors import LocalApplyError
from checker.update_observation import classify_update_response


ARTICLE_ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER_ID = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "p0-update-test-article"
BASELINE = {key: "2026-10-10T00:00:00+09:00" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}
CURRENT = {**BASELINE, "body_updated_at": "2026-10-10T01:00:00+09:00"}


class UpdateTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name)
    (self.root / "articles").mkdir()
    (self.root / ".zenn-authoring" / "released").mkdir(parents=True)
    self.artifact = self.root / "articles" / f"{SLUG}.md"
    self.snapshot = self.root / ".zenn-authoring" / "released" / f"{ARTICLE_ID}.md"
    self.ledger_path = self.root / ".zenn-authoring" / "ledger.json"
    self.artifact.write_bytes(b"new release\n")
    self.snapshot.write_bytes(b"old release\n")
    self.ledger = {
      "articles": {ARTICLE_ID: {
        "state": "active", "slug": SLUG, "successful_release": 3,
        "successful_zenn_observation": BASELINE}},
      "pending_next_state": {ARTICLE_ID: {
        "operation": "UPDATE", "state": "active", "slug": SLUG,
        "release": 4, "baseline_observation": BASELINE}}}
    self.save()

  def save(self):
    self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")

  def classify(self, observation=CURRENT, owner=2, status=200):
    pending = terminal_update.read_update_pending(self.ledger, ARTICLE_ID)
    return classify_update_response(ArticleResponse(status, {"article": {
      "slug": SLUG, "user": {"id": owner}, **observation}}), pending, 2)

  def apply(self):
    terminal_update.apply_update_success(self.root, ARTICLE_ID, CURRENT)

  def assert_rejected(self):
    before = self.ledger_path.read_bytes()
    with patch.object(terminal_update, "atomic_replace_bytes") as replace:
      with self.assertRaises((LocalApplyError, OSError)):
        self.apply()
      replace.assert_not_called()
    self.assertEqual(self.ledger_path.read_bytes(), before)

  def test_changed_observation_success(self):
    result = self.classify()
    self.assertEqual(result.result, CreateResult.SUCCESS)
    self.assertEqual(result.observation, CURRENT)
    self.assertEqual(self.ledger["pending_next_state"][ARTICLE_ID]["baseline_observation"], BASELINE)

  def test_unchanged_pending_without_writes(self):
    before = self.ledger_path.read_bytes(), self.snapshot.read_bytes()
    with patch.object(terminal_update, "atomic_replace_bytes") as replace:
      self.assertEqual(self.classify(BASELINE).result, CreateResult.PENDING)
      replace.assert_not_called()
    self.assertEqual((self.ledger_path.read_bytes(), self.snapshot.read_bytes()), before)

  def test_each_observation_field_detects_change(self):
    for key in BASELINE:
      with self.subTest(key=key):
        self.assertEqual(self.classify({**BASELINE, key: "changed"}).result, CreateResult.SUCCESS)

  def test_other_owner_unknown(self):
    self.assertEqual(self.classify(owner=9).result, CreateResult.UNKNOWN)

  def test_http_results(self):
    for status, expected in ((404, CreateResult.UNKNOWN), (500, CreateResult.ERROR),
                             (403, CreateResult.ERROR), (302, CreateResult.UNKNOWN),
                             (True, CreateResult.ERROR)):
      with self.subTest(status=status):
        self.assertEqual(self.classify(status=status).result, expected)

  def test_bad_owner(self):
    for owner in (None, True, "2", 0, -1):
      with self.subTest(owner=owner):
        self.assertEqual(self.classify(owner=owner).result, CreateResult.ERROR)

  def test_bad_response_schema_and_slug(self):
    pending = self.ledger["pending_next_state"][ARTICLE_ID]
    for payload in (None, {}, {"article": []}, {"article": {
        "slug": "different-slug", "user": {"id": 2}, **CURRENT}}):
      with self.subTest(payload=payload):
        result = classify_update_response(ArticleResponse(200, payload), pending, 2)
        self.assertEqual(result.result, CreateResult.ERROR)
    self.assertEqual(classify_update_response(None, pending, 2).result, CreateResult.ERROR)
    self.assertEqual(classify_update_response(ArticleResponse(200), pending, True).result,
                     CreateResult.ERROR)

  def test_bad_current_observation(self):
    for key in CURRENT:
      for value in (None, "", 4, True):
        with self.subTest(key=key, value=value):
          self.assertEqual(self.classify({**CURRENT, key: value}).result, CreateResult.ERROR)
          with patch.object(terminal_update, "atomic_replace_bytes") as replace:
            with self.assertRaises(LocalApplyError):
              terminal_update.apply_update_success(self.root, ARTICLE_ID, {**CURRENT, key: value})
            replace.assert_not_called()
    self.assertEqual(self.classify({}).result, CreateResult.ERROR)

  def test_bad_baseline(self):
    for value in (None, {}, {**BASELINE, "published_at": False}):
      with self.subTest(value=value):
        self.ledger["pending_next_state"][ARTICLE_ID]["baseline_observation"] = value
        self.save()
        self.assert_rejected()
        result = classify_update_response(ArticleResponse(200),
            self.ledger["pending_next_state"][ARTICLE_ID], 2)
        self.assertEqual(result.result, CreateResult.ERROR)

  def test_pending_inconsistencies(self):
    original = copy.deepcopy(self.ledger)
    for key, value in (("operation", "CREATE"), ("state", "erased"),
                       ("slug", "another-article-slug"), ("slug", "../unsafe"),
                       ("release", 3), ("release", 5), ("release", 0),
                       ("release", True), ("release", "4")):
      with self.subTest(key=key, value=value):
        self.ledger = copy.deepcopy(original)
        self.ledger["pending_next_state"][ARTICLE_ID][key] = value
        self.save()
        self.assert_rejected()

  def test_confirmed_inconsistencies(self):
    original = copy.deepcopy(self.ledger)
    for key, value in (("state", "erased"), ("slug", "another-article-slug"),
                       ("successful_release", True), ("successful_release", 0),
                       ("successful_zenn_observation", {})):
      with self.subTest(key=key):
        self.ledger = copy.deepcopy(original)
        self.ledger["articles"][ARTICLE_ID][key] = value
        self.save()
        self.assert_rejected()

  def test_invalid_article_id(self):
    with patch.object(terminal_update, "atomic_replace_bytes") as replace:
      with self.assertRaises(LocalApplyError):
        terminal_update.apply_update_success(self.root, "../invalid", CURRENT)
      replace.assert_not_called()

  def test_success_preserves_state_slug_and_other_articles(self):
    other = {"state": "erased", "slug": "another-article-slug", "successful_release": 2,
             "successful_zenn_observation": BASELINE}
    other_pending = {"operation": "RECREATE", "state": "active",
                     "slug": "another-new-article", "release": 3}
    self.ledger["articles"][OTHER_ID] = other
    self.ledger["pending_next_state"][OTHER_ID] = other_pending
    self.save()
    real_replace = terminal_update.atomic_replace_bytes
    calls = []

    def record(target, content):
      calls.append(target)
      self.assertIn(ARTICLE_ID, json.loads(self.ledger_path.read_bytes())["pending_next_state"])
      if target == self.ledger_path:
        self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
        next_ledger = json.loads(content)
        self.assertNotIn(ARTICLE_ID, next_ledger["pending_next_state"])
        self.assertEqual(next_ledger["articles"][ARTICLE_ID]["successful_release"], 4)
      return real_replace(target, content)

    with patch.object(terminal_update, "atomic_replace_bytes", side_effect=record):
      self.apply()
    ledger = json.loads(self.ledger_path.read_bytes())
    self.assertEqual(calls, [self.snapshot, self.ledger_path])
    self.assertEqual(ledger["articles"][ARTICLE_ID], {
      **self.ledger["articles"][ARTICLE_ID], "successful_release": 4,
      "successful_zenn_observation": CURRENT})
    self.assertEqual(ledger["articles"][OTHER_ID], other)
    self.assertEqual(ledger["pending_next_state"], {OTHER_ID: other_pending})

  def test_interruption_then_fresh_success_recovery(self):
    before = self.ledger_path.read_bytes()
    real_replace = terminal_update.atomic_replace_bytes

    def interrupt(target, content):
      if target == self.ledger_path:
        raise OSError("interrupted before ledger commit")
      return real_replace(target, content)

    with patch.object(terminal_update, "atomic_replace_bytes", side_effect=interrupt):
      with self.assertRaises(OSError):
        self.apply()
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    self.assertEqual(self.classify().result, CreateResult.SUCCESS)
    with patch("checker.atomic_write.os.replace", wraps=os.replace) as replace:
      self.apply()
      self.assertEqual(replace.call_count, 1)
      self.assertEqual(Path(replace.call_args.args[1]), self.ledger_path)
    self.assertNotIn(ARTICLE_ID, json.loads(self.ledger_path.read_bytes())["pending_next_state"])

  def test_identical_snapshot_noop(self):
    self.snapshot.write_bytes(self.artifact.read_bytes())
    with patch("checker.atomic_write.os.replace", wraps=os.replace) as replace:
      self.apply()
      self.assertEqual(replace.call_count, 1)
      self.assertEqual(Path(replace.call_args.args[1]), self.ledger_path)

  def test_missing_artifact(self):
    self.artifact.unlink()
    self.assert_rejected()
    self.assertEqual(self.snapshot.read_bytes(), b"old release\n")

  def test_no_reapply_after_commit(self):
    self.apply()
    self.assert_rejected()

  def test_snapshot_failure_keeps_ledger_and_snapshot(self):
    before = self.ledger_path.read_bytes()
    with patch("checker.atomic_write.os.replace", side_effect=OSError("failure")):
      with self.assertRaises(OSError):
        self.apply()
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(self.snapshot.read_bytes(), b"old release\n")

  def test_symlink_and_dangling_symlink_targets(self):
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
            with patch.object(terminal_update, "atomic_replace_bytes") as replace:
              with self.assertRaises(LocalApplyError):
                self.apply()
              replace.assert_not_called()
            if not dangling:
              self.assertEqual(external.read_bytes(), content)
            else:
              self.assertFalse(external.exists())
          finally:
            path.unlink()
            path.write_bytes(content)
            if external.exists():
              external.unlink()

  def test_special_files_and_directories(self):
    for path in (self.artifact, self.snapshot, self.ledger_path):
      content = path.read_bytes()
      for special in ("fifo", "directory"):
        with self.subTest(path=path.name, special=special):
          path.unlink()
          os.mkfifo(path) if special == "fifo" else path.mkdir()
          try:
            with patch.object(terminal_update, "atomic_replace_bytes") as replace:
              with self.assertRaises((LocalApplyError, OSError)):
                self.apply()
              replace.assert_not_called()
          finally:
            path.unlink() if special == "fifo" else path.rmdir()
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
            with patch.object(terminal_update, "atomic_replace_bytes") as replace:
              with self.assertRaises((LocalApplyError, OSError)):
                self.apply()
              replace.assert_not_called()
          finally:
            parent.unlink()
            backup.rename(parent)


if __name__ == "__main__":
  unittest.main()
