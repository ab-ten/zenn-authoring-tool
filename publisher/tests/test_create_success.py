"""Fixture-based safety tests for the CREATE SUCCESS local apply."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checker.atomic_write import atomic_replace_bytes
from checker import terminal_create


ARTICLE_ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER_ID = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "p0-create-test-article"
OBSERVATION = {
  "body_updated_at": "2026-10-10T00:00:00+09:00",
  "source_repo_updated_at": "2026-10-10T00:00:00+09:00",
  "published_at": "2026-10-10T00:00:00+09:00",
}


class CreateSuccessTests(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.root = Path(self.tempdir.name)
    (self.root / "articles").mkdir()
    (self.root / ".zenn-authoring" / "released").mkdir(parents=True)
    self.artifact = self.root / "articles" / f"{SLUG}.md"
    self.artifact.write_bytes(b"---\ntitle: Test\n---\n\nHello\n")
    self.ledger_path = self.root / ".zenn-authoring" / "ledger.json"
    self.snapshot = self.root / ".zenn-authoring" / "released" / f"{ARTICLE_ID}.md"
    self.ledger = {
      "articles": {},
      "pending_next_state": {
        ARTICLE_ID: {"operation": "CREATE", "slug": SLUG,
                     "state": "active", "release": 1},
      },
    }
    self.write_ledger()

  def write_ledger(self):
    self.ledger_path.write_text(json.dumps(self.ledger) + "\n", encoding="utf-8")

  def read_ledger(self):
    return json.loads(self.ledger_path.read_text(encoding="utf-8"))

  def apply(self):
    terminal_create.apply_create_success(self.root, ARTICLE_ID, OBSERVATION)

  def test_normal_create_success(self):
    self.apply()
    ledger = self.read_ledger()
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    self.assertEqual(ledger["pending_next_state"], {})
    self.assertEqual(ledger["articles"][ARTICLE_ID]["state"], "active")
    self.assertEqual(ledger["articles"][ARTICLE_ID]["slug"], SLUG)
    self.assertEqual(ledger["articles"][ARTICLE_ID]["successful_release"], 1)
    self.assertEqual(ledger["articles"][ARTICLE_ID]["successful_zenn_observation"],
                     OBSERVATION)

  def test_interruption_after_snapshot_before_ledger(self):
    real_replace = terminal_create.atomic_replace_bytes
    calls = 0

    def fail_at_ledger(target, content):
      nonlocal calls
      calls += 1
      if calls == 2:
        raise OSError("injected failure before ledger replacement")
      return real_replace(target, content)

    before = self.ledger_path.read_bytes()
    with patch.object(terminal_create, "atomic_replace_bytes",
                      side_effect=fail_at_ledger):
      with self.assertRaises(OSError):
        self.apply()

    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertIn(ARTICLE_ID, self.read_ledger()["pending_next_state"])

    self.apply()
    self.assertEqual(self.read_ledger()["pending_next_state"], {})
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())

  def test_snapshot_retry_is_noop(self):
    self.snapshot.write_bytes(self.artifact.read_bytes())
    real_replace = terminal_create.atomic_replace_bytes
    results = []

    def record_replace(target, content):
      result = real_replace(target, content)
      results.append(result)
      return result

    with patch.object(terminal_create, "atomic_replace_bytes",
                      side_effect=record_replace):
      self.apply()
    self.assertEqual(results, [False, True])
    self.assertEqual(self.read_ledger()["pending_next_state"], {})

  def test_missing_artifact_preserves_state(self):
    self.artifact.unlink()
    before = self.ledger_path.read_bytes()
    with self.assertRaises(terminal_create.LocalApplyError):
      self.apply()
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertFalse(self.snapshot.exists())

  def test_pending_mismatch_preserves_state(self):
    self.ledger["pending_next_state"][ARTICLE_ID]["operation"] = "UPDATE"
    self.write_ledger()
    before = self.ledger_path.read_bytes()
    with self.assertRaises(terminal_create.LocalApplyError):
      self.apply()
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertFalse(self.snapshot.exists())

  def test_unrelated_pending_is_preserved(self):
    self.ledger["pending_next_state"][OTHER_ID] = {
      "operation": "DELETE", "slug": "another-article-slug",
      "state": "erased", "release": 3,
    }
    self.write_ledger()
    self.apply()
    self.assertIn(OTHER_ID, self.read_ledger()["pending_next_state"])
    self.assertNotIn(ARTICLE_ID, self.read_ledger()["pending_next_state"])

  def test_no_reapply_after_ledger_commit(self):
    self.apply()
    before = self.ledger_path.read_bytes()
    with self.assertRaises(terminal_create.LocalApplyError):
      self.apply()
    self.assertEqual(self.ledger_path.read_bytes(), before)

  def test_atomic_replace_failure_keeps_old_file(self):
    self.snapshot.write_bytes(b"previous content")
    with patch("checker.atomic_write.os.replace", side_effect=OSError("fail")):
      with self.assertRaises(OSError):
        atomic_replace_bytes(self.snapshot, b"new content")
    self.assertEqual(self.snapshot.read_bytes(), b"previous content")
    self.assertEqual(list(self.snapshot.parent.glob(f".{self.snapshot.name}.*.tmp")), [])

  def test_bad_observation_fails_before_write(self):
    with self.assertRaises(terminal_create.LocalApplyError):
      terminal_create.apply_create_success(self.root, ARTICLE_ID, {})
    self.assertFalse(self.snapshot.exists())
    self.assertIn(ARTICLE_ID, self.read_ledger()["pending_next_state"])


if __name__ == "__main__":
  unittest.main()
