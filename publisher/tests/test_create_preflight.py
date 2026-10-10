"""CREATE preflight の fixture、通信 mock、不変性を検証します。"""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker.create_observation import ArticleResponse
from checker.create_preflight import (
  ArticlePlan, PublishPlan, PreflightStatus, preflight_create,
)


ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "p0-create-test-article"
SECOND = "other-article-slug"


def response(owner=3, slug=SLUG):
  return ArticleResponse(200, {"article": {
    "slug": slug, "user": {"id": owner, "username": "same-name"},
  }})


class CreatePreflightTests(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    self.meta = self.root / ".zenn-authoring"
    (self.meta / "released").mkdir(parents=True)
    (self.root / "articles").mkdir()
    (self.root / "authoring").mkdir()
    self.ledger = {"articles": {}, "pending_next_state": {}}
    self.write_ledger()
    self.config = self.meta / "config.json"
    self.config.write_text(json.dumps({"zenn_user": {"id": 2, "username": "same-name"}}))
    (self.root / "articles" / f"{SLUG}.md").write_bytes(b"deploy\n")
    (self.meta / "released" / f"{OTHER}.md").write_bytes(b"snapshot\n")
    (self.root / "authoring" / "source.md").write_bytes(b"source\n")
    self.plan = PublishPlan((ArticlePlan(ID, "CREATE", SLUG),))

  def write_ledger(self):
    (self.meta / "ledger.json").write_text(json.dumps(self.ledger))

  def tree(self):
    return {str(path.relative_to(self.root)): (
      "link", str(path.readlink())) if path.is_symlink() else (
      "directory",) if path.is_dir() else ("file", path.read_bytes())
      for path in self.root.rglob("*")}

  def check(self, expected, observed=None, *, plan=None, failure=None, calls=1):
    plan = self.plan if plan is None else plan
    before = self.tree()
    original_entries = plan.articles
    original_values = tuple((e.article_id, e.operation, e.slug) for e in plan.articles)
    fetch = Mock(return_value=observed, side_effect=failure)
    result = preflight_create(self.root, plan, fetch)
    self.assertEqual(result.result, expected)
    self.assertEqual(fetch.call_count, calls)
    self.assertEqual(self.tree(), before)
    self.assertIs(plan.articles, original_entries)
    self.assertEqual(tuple((e.article_id, e.operation, e.slug) for e in plan.articles),
                     original_values)
    self.assertFalse(hasattr(result, "observation"))
    for item in result.articles:
      self.assertFalse(hasattr(item, "observation"))
    return result

  def test_404_ok(self):
    self.check(PreflightStatus.OK, ArticleResponse(404))

  def test_other_owner_collision(self):
    result = self.check(PreflightStatus.COLLISION, response())
    self.assertEqual((result.articles[0].article_id, result.articles[0].slug), (ID, SLUG))

  def test_own_owner_error(self):
    self.check(PreflightStatus.ERROR, response(2))

  def test_missing_config(self):
    self.config.unlink()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_configs(self):
    for value in (None, [], {}, {"zenn_user": []},
                  *({"zenn_user": {"id": bad, "username": "same-name"}}
                    for bad in (None, "2", True, 0, -1, 2.0))):
      with self.subTest(value=value):
        self.config.write_text(json.dumps(value))
        self.check(PreflightStatus.ERROR, calls=0)
    self.config.write_text("invalid JSON")
    self.check(PreflightStatus.ERROR, calls=0)

  def test_pending_guard(self):
    self.ledger["pending_next_state"][OTHER] = {"operation": "DELETE"}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)
    self.check(PreflightStatus.ERROR, plan=PublishPlan(), calls=0)

  def test_confirmed_id(self):
    self.ledger["articles"][ID] = {"state": "erased"}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_remote_owner(self):
    for owner in (None, "3", True, False, 0, -1, 3.0):
      with self.subTest(owner=owner):
        self.check(PreflightStatus.ERROR, response(owner))

  def test_remote_slug_mismatch(self):
    self.check(PreflightStatus.ERROR, response(slug=SECOND))

  def test_malformed_remote_schema(self):
    for payload in (None, [], {}, {"article": []}, {"article": {"slug": SLUG}},
                    {"article": {"slug": SLUG, "user": {"username": "same-name"}}}):
      with self.subTest(payload=payload):
        self.check(PreflightStatus.ERROR, ArticleResponse(200, payload))

  def test_unexpected_status(self):
    for status in (201, 302, 401, 403, 429, 500, True, "404", None):
      with self.subTest(status=status):
        self.check(PreflightStatus.ERROR, ArticleResponse(status))

  def test_transport_failures(self):
    for failure in (OSError("network"), TimeoutError("timeout"),
                    ValueError("invalid JSON"), RuntimeError("adapter failure")):
      with self.subTest(failure=failure):
        self.check(PreflightStatus.ERROR, failure=failure)

  def test_invalid_transport_response(self):
    self.check(PreflightStatus.ERROR, {"status": 404})

  def multi_plan(self):
    return PublishPlan((*self.plan.articles, ArticlePlan(OTHER, "CREATE", SECOND)))

  def test_multiple_ok_collision(self):
    result = self.check(PreflightStatus.COLLISION, plan=self.multi_plan(), calls=2,
                        failure=[ArticleResponse(404), response(slug=SECOND)])
    self.assertEqual([item.result for item in result.articles],
                     [PreflightStatus.OK, PreflightStatus.COLLISION])

  def test_multiple_all_ok(self):
    self.check(PreflightStatus.OK, ArticleResponse(404), plan=self.multi_plan(), calls=2)

  def test_error_priority_and_all_observed(self):
    result = self.check(PreflightStatus.ERROR, plan=self.multi_plan(), calls=2,
                        failure=[OSError("failed"), response(slug=SECOND)])
    self.assertEqual(result.articles[1].result, PreflightStatus.COLLISION)

  def test_empty_plan_skips_config_and_remote(self):
    self.config.unlink()
    self.check(PreflightStatus.OK, plan=PublishPlan(), calls=0)

  def test_unsupported_operations_block_all_requests(self):
    for operation in ("UPDATE", "DELETE", "RECREATE", "", None):
      with self.subTest(operation=operation):
        plan = PublishPlan((*self.plan.articles, ArticlePlan(OTHER, operation, SECOND)))
        result = self.check(PreflightStatus.ERROR, plan=plan, calls=0)
        self.assertEqual(result.articles[0].article_id, OTHER)

  def test_invalid_plan_identity_and_slug(self):
    for entry in (ArticlePlan("../bad", "CREATE", SLUG),
                  ArticlePlan(ID.upper(), "CREATE", SLUG),
                  ArticlePlan(ID, "CREATE", "short"),
                  ArticlePlan(ID, "CREATE", "../unsafe-slug")):
      with self.subTest(entry=entry):
        self.check(PreflightStatus.ERROR, plan=PublishPlan((entry,)), calls=0)

  def test_duplicate_ids_and_slugs(self):
    for entry in (ArticlePlan(ID, "CREATE", SECOND), ArticlePlan(OTHER, "CREATE", SLUG)):
      self.check(PreflightStatus.ERROR,
                 plan=PublishPlan((*self.plan.articles, entry)), calls=0)

  def test_confirmed_slug_overlap(self):
    self.ledger["articles"][OTHER] = {"slug": SLUG}
    self.write_ledger()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_invalid_and_missing_ledger(self):
    ledger = self.meta / "ledger.json"
    for content in ("invalid", "[]", '{"articles":{}}'):
      ledger.write_text(content)
      self.check(PreflightStatus.ERROR, calls=0)
    ledger.unlink()
    self.check(PreflightStatus.ERROR, calls=0)

  def test_unsafe_config_and_ledger(self):
    for path in (self.config, self.meta / "ledger.json"):
      content = path.read_bytes()
      target = self.root / "target.json"
      target.write_bytes(content)
      path.unlink()
      path.symlink_to(target)
      self.check(PreflightStatus.ERROR, calls=0)
      path.unlink()
      path.write_bytes(content)

  def test_models_are_frozen(self):
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles = ()
    with self.assertRaises(FrozenInstanceError):
      self.plan.articles[0].operation = "UPDATE"
    with self.assertRaises(TypeError):
      PublishPlan([])

  def test_structural_plan_passed_directly(self):
    class ConverterPlan:
      @property
      def articles(inner):
        return self.plan.articles
    self.check(PreflightStatus.OK, ArticleResponse(404), plan=ConverterPlan())

  def test_default_http_adapter_with_mocked_transport(self):
    before = self.tree()
    with patch("checker.zenn_http.urlopen", side_effect=OSError("mock failure")) as http:
      result = preflight_create(self.root, self.plan)
    self.assertEqual(result.result, PreflightStatus.ERROR)
    http.assert_called_once()
    self.assertEqual(self.tree(), before)


if __name__ == "__main__":
  unittest.main()
