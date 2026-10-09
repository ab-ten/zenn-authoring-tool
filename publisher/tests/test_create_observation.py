"""P0-CHECK-02: safe CREATE classification and single-pending integration."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checker.create_observation import (
  ArticleResponse,
  CreateResult,
  check_single_create,
  classify_create_response,
)
from checker import terminal_create


ARTICLE_ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER_ID = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "p0-create-test-article"
OBSERVATION = {
  "body_updated_at": "2026-10-10T01:00:00+09:00",
  "source_repo_updated_at": "2026-10-10T01:00:00+09:00",
  "published_at": "2026-10-10T01:00:00+09:00",
}


def response(user_id=2, slug=SLUG, **overrides):
  """Build a decoded Zenn article response fixture."""
  article = {"slug": slug, "user": {"id": user_id, "username": "untrusted"},
             **OBSERVATION, **overrides}
  return ArticleResponse(200, {"article": article})


class CreateObservationTests(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.root = Path(self.tempdir.name)
    self.meta = self.root / ".zenn-authoring"
    (self.meta / "released").mkdir(parents=True)
    (self.root / "articles").mkdir()
    self.ledger_path = self.meta / "ledger.json"
    self.config_path = self.meta / "config.json"
    self.artifact = self.root / "articles" / f"{SLUG}.md"
    self.snapshot = self.meta / "released" / f"{ARTICLE_ID}.md"
    self.artifact.write_bytes(b"---\ntitle: Test\n---\n\nHello\n")
    self.ledger = {"articles": {}, "pending_next_state": {ARTICLE_ID: {
      "operation": "CREATE", "slug": SLUG, "state": "active", "release": 1,
    }}}
    self.write_ledger()
    self.config_path.write_text(json.dumps({
      "zenn_user": {"id": 2, "username": "zenn"}
    }), encoding="utf-8")

  def write_ledger(self):
    self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")

  def read_ledger(self):
    return json.loads(self.ledger_path.read_text(encoding="utf-8"))

  def check(self, observed):
    return check_single_create(self.root, ARTICLE_ID, lambda slug: observed)

  def assert_unmodified(self):
    self.assertEqual(self.read_ledger(), self.ledger)
    self.assertFalse(self.snapshot.exists())

  def test_404_remains_pending(self):
    self.assertEqual(self.check(ArticleResponse(404)).result, CreateResult.PENDING)
    self.assert_unmodified()

  def test_200_own_user_applies_create_success(self):
    result = self.check(response())
    self.assertEqual(result.result, CreateResult.SUCCESS)
    self.assertEqual(result.observation, OBSERVATION)
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    ledger = self.read_ledger()
    self.assertEqual(ledger["pending_next_state"], {})
    self.assertEqual(ledger["articles"][ARTICLE_ID], {
      "slug": SLUG, "state": "active", "successful_release": 1,
      "successful_zenn_observation": OBSERVATION,
    })

  def test_200_other_user_is_collision_without_cleanup_yet(self):
    self.assertEqual(self.check(response(user_id=21)).result, CreateResult.COLLISION)
    self.assert_unmodified()
    self.assertTrue(self.artifact.exists())

  def test_200_missing_owner_is_error(self):
    self.assertEqual(self.check(response(user_id=None)).result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_200_bool_owner_is_error(self):
    self.assertEqual(self.check(response(user_id=True)).result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_owner_username_never_used_as_fallback(self):
    invalid = response(user_id=None, user={"username": "zenn"})
    self.assertEqual(self.check(invalid).result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_200_invalid_observation_is_error(self):
    self.assertEqual(self.check(response(body_updated_at=None)).result,
                     CreateResult.ERROR)
    self.assert_unmodified()

  def test_article_slug_mismatch_is_error(self):
    self.assertEqual(self.check(response(slug="another-slug-test")).result,
                     CreateResult.ERROR)
    self.assert_unmodified()

  def test_200_missing_article_is_error(self):
    self.assertEqual(self.check(ArticleResponse(200, {})).result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_http_500_is_error(self):
    self.assertEqual(self.check(ArticleResponse(500)).result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_http_302_is_unknown(self):
    self.assertEqual(self.check(ArticleResponse(302)).result, CreateResult.UNKNOWN)
    self.assert_unmodified()

  def test_network_failure_is_error(self):
    def fail(_slug):
      raise TimeoutError("request timed out")
    self.assertEqual(check_single_create(self.root, ARTICLE_ID, fail).result,
                     CreateResult.ERROR)
    self.assert_unmodified()

  def test_bad_owner_config_fails_before_fetch(self):
    self.config_path.write_text('{"zenn_user":{"id":"2"}}', encoding="utf-8")
    calls = []
    outcome = check_single_create(self.root, ARTICLE_ID,
                                  lambda slug: calls.append(slug))
    self.assertEqual(outcome.result, CreateResult.ERROR)
    self.assertEqual(calls, [])
    self.assert_unmodified()

  def test_missing_config_fails_before_fetch(self):
    self.config_path.unlink()
    outcome = check_single_create(self.root, ARTICLE_ID,
                                  lambda slug: self.fail("unexpected request"))
    self.assertEqual(outcome.result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_multi_pending_not_partially_applied(self):
    self.ledger["pending_next_state"][OTHER_ID] = {
      "operation": "DELETE", "slug": "other-article-slug",
      "state": "erased", "release": 3,
    }
    self.write_ledger()
    outcome = self.check(response())
    self.assertEqual(outcome.result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_local_apply_failure_is_error_and_retry_succeeds(self):
    original = terminal_create.atomic_replace_bytes
    calls = 0

    def fail_second_write(path, content):
      nonlocal calls
      calls += 1
      if calls == 2:
        raise OSError("injected ledger write failure")
      return original(path, content)

    with patch.object(terminal_create, "atomic_replace_bytes",
                      side_effect=fail_second_write):
      result = self.check(response())
    self.assertEqual(result.result, CreateResult.ERROR)
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    self.assertEqual(self.read_ledger(), self.ledger)

    result = self.check(response())
    self.assertEqual(result.result, CreateResult.SUCCESS)
    self.assertEqual(self.read_ledger()["pending_next_state"], {})

  def test_missing_artifact_blocks_success(self):
    self.artifact.unlink()
    result = self.check(response())
    self.assertEqual(result.result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_single_pending_required(self):
    self.ledger["pending_next_state"] = {}
    self.write_ledger()
    result = self.check(response())
    self.assertEqual(result.result, CreateResult.ERROR)
    self.assert_unmodified()

  def test_transport_must_return_article_response(self):
    result = self.check({"status": 200})
    self.assertEqual(result.result, CreateResult.ERROR)
    self.assert_unmodified()


class CreateClassificationPureTests(unittest.TestCase):
  def test_404_does_not_require_article_payload(self):
    got = classify_create_response(ArticleResponse(404, None), SLUG, 2)
    self.assertEqual(got.result, CreateResult.PENDING)

  def test_invalid_config_id_is_error_even_on_404(self):
    got = classify_create_response(ArticleResponse(404), SLUG, True)
    self.assertEqual(got.result, CreateResult.ERROR)


if __name__ == "__main__":
  unittest.main()
