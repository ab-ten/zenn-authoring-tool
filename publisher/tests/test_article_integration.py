"""隔離した実 Git worktree と実ファイルで read-only article integration を検証します。"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from converter import article_integration as integration
from converter import planning
from converter.entry_guards import inspect_inputs, CheckedSource
from converter.filesystem_inputs import load_ledger, load_sources, load_released_snapshots
from converter.source_parser import JST, parse_source
from test_entry_guards import fixture_git, snapshot


ID = "6a5c05e8-401a-4e2b-bc92-2c9aa61be305"
OTHER = "95660b64-4620-4292-b509-02f8b6f49599"
SLUG = "integration-article-slug"
FRONT = '---\ntitle: Test\nemoji: ""\ntype: tech\ntopics: []\npublished: true\n'
NOW = datetime(2026, 10, 11, tzinfo=JST)
OBSERVATION = {key: "timestamp" for key in (
  "body_updated_at", "source_repo_updated_at", "published_at")}


def source(article_id=ID, state="active", slug=SLUG, release=1, body="Body\n", date=None):
  front = FRONT + (f"published_at: '{date}'\n" if date is not None else "") + "---\n"
  comments = f"<!-- #id:{article_id} -->\n<!-- #state:{state} -->\n"
  if slug is not None:
    comments += f"<!-- #slug:{slug} -->\n"
  if release is not None:
    comments += f"<!-- #release:{release} -->\n"
  return front + comments + body


class ArticleIntegrationTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name) / "article repo"
    self.root.mkdir()
    fixture_git(self.root, "init", "-b", "main")
    fixture_git(self.root, "config", "worktree.useRelativePaths", "true")
    (self.root / "articles").mkdir()
    (self.root / "images").mkdir()
    (self.root / "images" / "asset.png").write_bytes(b"image")
    (self.root / ".gitignore").write_text("/_deploy/\n")
    self.commit(self.root)
    fixture_git(self.root, "worktree", "add", "--relative-paths", "-b", "deploy", "_deploy")
    self.deploy = self.root / "_deploy"
    self.management = self.deploy / ".zenn-authoring"
    (self.management / "released").mkdir(parents=True)
    self.ledger_path = self.management / "ledger.json"
    self.ledger = {"articles": {}, "pending_next_state": {}}
    self.save_ledger()
    self.commit(self.deploy)
    self.http = patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected HTTP request"))
    self.http_mock = self.http.start()
    self.addCleanup(self.http.stop)
    zenn_http = patch("checker.zenn_http.urlopen", side_effect=AssertionError("Unexpected Zenn HTTP request"))
    self.zenn_http_mock = zenn_http.start()
    self.addCleanup(zenn_http.stop)

  def commit(self, root):
    fixture_git(root, "add", "-A")
    fixture_git(root, "commit", "--allow-empty", "-m", "fixture")

  def save_ledger(self):
    self.ledger_path.write_text(json.dumps(self.ledger))

  def write_source(self, markdown=None, name="filename unrelated to slug.md", commit=True):
    path = self.root / "articles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((source() if markdown is None else markdown).encode("utf-8"))
    if commit:
      self.commit(self.root)
    return path

  def confirm(self, markdown=None, article_id=ID, state="active", slug=SLUG, release=3):
    markdown = source(article_id, release=release, slug=slug) if markdown is None else markdown
    content = parse_source(markdown).record.candidate_content
    self.ledger["articles"][article_id] = {
      "state": state, "slug": slug, "successful_release": release,
      "successful_zenn_observation": dict(OBSERVATION)}
    path = self.management / "released" / f"{article_id}.md"
    path.write_bytes(content.encode("utf-8"))
    if state == "active":
      (self.deploy / "articles").mkdir(exist_ok=True)
      (self.deploy / "articles" / f"{slug}.md").write_bytes(content.encode("utf-8"))
    self.save_ledger()
    self.commit(self.deploy)
    return path

  def plan(self):
    before = snapshot(self.root)
    try:
      return integration.plan_repository_articles(self.root, now=NOW)
    finally:
      self.assertEqual(snapshot(self.root), before)
      self.http_mock.assert_not_called()
      self.zenn_http_mock.assert_not_called()

  def fail_without_planner(self, pattern=None):
    with patch.object(integration, "plan_articles", side_effect=AssertionError("Unexpected planning")) as plan:
      with self.assertRaisesRegex(ValueError, pattern or "."):
        self.plan()
      plan.assert_not_called()

  def test_empty_ledger_no_operations(self):
    result = self.plan()
    self.assertEqual(result.article_plan.articles, ())
    self.assertEqual(result.inputs.sources, ())
    self.assertEqual(result.inputs.images, (self.root / "images" / "asset.png",))

  def test_create_end_to_end(self):
    path = self.write_source()
    result = self.plan()
    entry, = result.article_plan.articles
    self.assertEqual((entry.article_id, entry.operation, entry.release), (ID, "CREATE", 1))
    self.assertEqual(entry.candidate_content, parse_source(source()).record.candidate_content)
    self.assertEqual(result.inputs.sources[0].path, path)
    self.assertFalse((self.management / "released" / f"{ID}.md").exists())

  def test_update(self):
    self.confirm()
    self.write_source(source(release=4, body="Updated\n"))
    self.assertEqual(self.plan().article_plan.articles[0].operation, "UPDATE")

  def test_delete(self):
    self.confirm()
    self.write_source(source(state="erased", release=3))
    entry, = self.plan().article_plan.articles
    self.assertEqual(entry.operation, "DELETE")
    self.assertIsNone(entry.candidate_content)

  def test_recreate_retained_erased_snapshot(self):
    path = self.confirm(state="erased")
    original = path.read_bytes()
    self.write_source(source(release=3))
    self.assertEqual(self.plan().article_plan.articles[0].operation, "RECREATE")
    self.assertEqual(path.read_bytes(), original)

  def test_recreate_changed_content(self):
    self.confirm(state="erased")
    self.write_source(source(release=4, body="Recreated\n"))
    self.assertEqual(self.plan().article_plan.articles[0].operation, "RECREATE")

  def test_active_noop(self):
    self.confirm()
    self.write_source(source(release=3))
    with patch.object(integration, "validate_publication_dates", wraps=integration.validate_publication_dates) as dates:
      self.assertEqual(self.plan().article_plan.articles, ())
      self.assertEqual(dates.call_args.args[0][0].record.state, "active")

  def test_erased_noop_retains_snapshot(self):
    self.confirm(state="erased")
    self.write_source(source(state="erased", release=3))
    self.assertEqual(self.plan().article_plan.articles, ())

  def test_hold_no_transition_validation(self):
    self.write_source(source(state="hold", slug="invalid slug", release=999, date="2099-01-01"))
    self.assertEqual(self.plan().article_plan.articles, ())

  def test_tracked_dirty_hold(self):
    path = self.write_source(source(state="hold", slug=None, release=None))
    path.write_text(source(state="hold", slug=None, release=None, body="Dirty\n"))
    self.assertEqual(self.plan().article_plan.articles, ())

  def test_untracked_hold(self):
    self.write_source(source(state="hold", slug=None, release=None), commit=False)
    self.assertEqual(self.plan().article_plan.articles, ())

  def test_hold_rename_identity_uses_uuid(self):
    self.confirm()
    path = self.write_source(source(state="hold", release=999, slug="work in progress"))
    renamed = path.rename(path.with_name("different filename.md"))
    result = self.plan()
    self.assertEqual(result.article_plan.articles, ())
    self.assertEqual(result.inputs.sources[0].path, renamed)

  def test_hold_rename_with_changed_id_detects_missing_ledger_source(self):
    self.confirm()
    path = self.write_source(source(state="hold"))
    renamed = path.rename(path.with_name("new filename.md"))
    renamed.write_text(source(article_id=OTHER, state="hold"))
    with self.assertRaisesRegex(ValueError, "Ledger-managed article has no source"):
      self.plan()

  def test_nested_spaces_and_newlines_filename(self):
    path = self.write_source(name="nested/a b\nc.md")
    result = self.plan()
    self.assertEqual(result.inputs.sources[0].path, path)
    self.assertEqual(result.article_plan.articles[0].article_id, ID)

  def test_multiple_mixed_operations(self):
    ids = [str(UUID(int=i + 1)) for i in range(6)]
    expected = ("CREATE", "UPDATE", "DELETE", "RECREATE")
    for i, article_id in enumerate(ids):
      slug = f"mixed-article-{i:02}"
      if i != 0:
        self.confirm(article_id=article_id, slug=slug, state="erased" if i == 3 else "active")
      state = "erased" if i == 2 else "hold" if i == 5 else "active"
      release = 1 if i == 0 else 4 if i == 1 else 3
      body = "Changed\n" if i == 1 else "Body\n"
      self.write_source(source(article_id, state, slug, release, body), name=f"{i}.md")
    self.assertEqual(tuple(e.operation for e in self.plan().article_plan.articles), expected)

  def test_newlines_and_trailing_whitespace_are_preserved(self):
    for newline in ("\n", "\r\n"):
      for body in ("Body", "Body\n", "Body\n\n", "Body  \n \t\n"):
        with self.subTest(newline=newline, body=body):
          markdown = source(body=body).replace("\n", newline)
          self.write_source(markdown)
          content = self.plan().article_plan.articles[0].candidate_content
          self.assertEqual(content, parse_source(markdown).record.candidate_content)
          self.assertTrue(content.endswith(body.replace("\n", newline)))

  def test_whitespace_only_body_difference_is_update(self):
    self.confirm()
    self.write_source(source(release=4, body="Body\n \t\n"))
    self.assertEqual(self.plan().article_plan.articles[0].operation, "UPDATE")

  def test_management_only_change_is_noop(self):
    self.confirm()
    markdown = source(release=3).replace("<!-- #id:", "  <!-- #id:")
    self.write_source(markdown)
    self.assertEqual(self.plan().article_plan.articles, ())

  def test_snapshot_crlf_exact_comparison(self):
    markdown = source(release=3).replace("\n", "\r\n")
    self.confirm(markdown)
    self.write_source(markdown)
    self.assertEqual(self.plan().article_plan.articles, ())

  def test_duplicate_normalized_id_including_hold(self):
    self.write_source(source(state="hold"), name="first.md")
    self.write_source(source(article_id=ID.upper(), state="hold"), name="second.md")
    with self.assertRaisesRegex(ValueError, "Duplicate normalized source UUID"):
      self.plan()

  def test_ledger_managed_source_missing(self):
    self.confirm()
    with self.assertRaisesRegex(ValueError, "Ledger-managed article has no source"):
      self.plan()

  def test_invalid_source_uuid_including_hold(self):
    self.write_source(source(article_id="invalid", state="hold"))
    with self.assertRaisesRegex(ValueError, "Invalid source UUID"):
      self.plan()

  def test_missing_snapshot(self):
    path = self.confirm()
    self.write_source(source(release=3))
    path.unlink()
    self.commit(self.deploy)
    self.fail_without_planner("Missing regular Markdown")

  def test_invalid_snapshot_utf8(self):
    path = self.confirm()
    self.write_source(source(release=3))
    path.write_bytes(b"\xff")
    self.commit(self.deploy)
    self.fail_without_planner("Cannot load Markdown")

  def test_snapshot_symlink(self):
    path = self.confirm()
    target = self.write_source(source(release=3))
    path.unlink()
    path.symlink_to(target)
    self.commit(self.deploy)
    self.fail_without_planner("Refusing symlink")

  def test_snapshot_directory(self):
    path = self.confirm()
    self.write_source(source(release=3))
    path.unlink()
    path.mkdir()
    self.commit(self.deploy)
    self.fail_without_planner("Missing regular Markdown")

  def test_missing_snapshot_for_erased_article(self):
    path = self.confirm(state="erased")
    self.write_source(source(state="erased", release=3))
    path.unlink()
    self.commit(self.deploy)
    self.fail_without_planner("Missing regular Markdown")

  def test_malformed_ledger(self):
    self.ledger_path.write_text("{}")
    self.commit(self.deploy)
    self.fail_without_planner("Invalid ledger maps")

  def test_invalid_confirmed_observation_all_states(self):
    self.write_source(source(state="hold"))
    for state in ("active", "erased"):
      self.confirm(state=state)
      for observation in (None, {}, {**OBSERVATION, "published_at": ""}, {**OBSERVATION, "body_updated_at": 1}):
        with self.subTest(state=state, observation=observation):
          self.ledger["articles"][ID]["successful_zenn_observation"] = observation
          self.save_ledger()
          self.commit(self.deploy)
          self.fail_without_planner("Invalid confirmed observation")

  def test_confirmed_state_release_and_slug_use_existing_planner(self):
    self.confirm()
    self.write_source(source(state="hold"))
    for field, value, error in (("state", "hold", "confirmed state"),
                                ("successful_release", True, "successful release"),
                                ("slug", "bad", "confirmed slug")):
      original = self.ledger["articles"][ID][field]
      self.ledger["articles"][ID][field] = value
      self.save_ledger()
      self.commit(self.deploy)
      with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
        self.plan()
      self.ledger["articles"][ID][field] = original

  def test_noncanonical_ledger_id_rejected_before_snapshot_path(self):
    self.confirm()
    confirmed = self.ledger["articles"][ID]
    for article_id in (ID.upper(), "../../outside", ID.replace("-", "")):
      with self.subTest(article_id=article_id):
        self.ledger["articles"] = {article_id: confirmed}
        self.save_ledger()
        self.commit(self.deploy)
        self.fail_without_planner("canonical lowercase UUID")

  def test_invalid_management_comments(self):
    markdown = source(state="hold").replace(f"<!-- #id:{ID} -->", "")
    self.write_source(markdown)
    self.fail_without_planner("Missing required id")

  def test_malformed_front_matter_on_hold(self):
    markdown = source(state="hold").replace("topics: []", "topics: not-a-list")
    self.write_source(markdown)
    self.fail_without_planner("topics")

  def test_malformed_yaml_parser_failure(self):
    self.write_source(source(state="hold").replace("topics: []", "topics: ["))
    self.fail_without_planner("Invalid Front Matter YAML")

  def test_invalid_published_at_transition(self):
    self.confirm(source(release=3, date="2026-10-10"))
    self.write_source(source(release=4, date="2026-10-11"))
    with patch.object(integration, "CheckedArticlePlanning", side_effect=AssertionError("Published result")):
      with self.assertRaisesRegex(ValueError, "Established published_at"):
        self.plan()

  def test_future_date_and_now_boundary(self):
    self.write_source(source(date="2026-10-11 00:00"))
    self.assertEqual(self.plan().article_plan.articles[0].operation, "CREATE")
    self.write_source(source(date="2026-10-11 00:01"))
    with self.assertRaisesRegex(ValueError, "Future published_at"):
      self.plan()

  def test_state_mismatch(self):
    self.write_source()
    real_guard = inspect_inputs

    def mismatched(root):
      inputs = real_guard(root)
      return replace(inputs, sources=tuple(replace(s, state="hold") for s in inputs.sources))

    with patch.object(integration, "inspect_inputs", side_effect=mismatched):
      self.fail_without_planner("Source state differs")

  def test_pending_does_not_parse_or_load_snapshots(self):
    self.write_source()
    self.ledger["pending_next_state"][ID] = {}
    self.save_ledger()
    self.commit(self.deploy)
    with patch.object(integration, "parse_source", side_effect=AssertionError("Full parse")), patch.object(
        integration, "load_released_snapshots", side_effect=AssertionError("Snapshot load")):
      self.fail_without_planner("pending_next_state")

  def test_reloaded_pending_is_checked_before_source_processing(self):
    self.write_source()
    with patch("converter.filesystem_inputs.read_ledger", return_value={
        "articles": {}, "pending_next_state": {ID: {}}}), patch.object(
        integration, "load_sources", side_effect=AssertionError("Source load")):
      self.fail_without_planner("pending_next_state")

  def test_dirty_deploy_does_not_load_snapshots(self):
    self.write_source()
    (self.deploy / "dirty").write_text("dirty")
    with patch.object(integration, "load_released_snapshots", side_effect=AssertionError("Snapshot load")):
      self.fail_without_planner("Dirty deploy")

  def test_dirty_active_does_not_plan(self):
    path = self.write_source()
    path.write_text(source(body="Dirty\n"))
    self.fail_without_planner("Source must")

  def test_development_target_rejected(self):
    (self.root / "docs").mkdir()
    (self.root / "docs" / "architecture.md").write_text("specification")
    path = self.root / "publisher" / "converter"
    path.mkdir(parents=True)
    (path / "source_parser.py").write_text("implementation")
    with patch.object(integration, "load_sources", side_effect=AssertionError("Source load")):
      self.fail_without_planner("Development repository")

  def test_pipeline_reuses_inputs_ledger_and_parsed_tuple(self):
    self.confirm()
    self.write_source(source(release=4, body="Changed\n"))
    captured_ledger, captured_parsed = [], []

    def capture_ledger(root):
      ledger = load_ledger(root)
      captured_ledger.append(ledger)
      return ledger

    def capture_parser(markdown):
      parsed = parse_source(markdown)
      captured_parsed.append(parsed)
      return parsed

    with patch.object(integration, "load_ledger", side_effect=capture_ledger) as ledger, patch.object(
        integration, "parse_source", side_effect=capture_parser) as parser, patch.object(
        integration, "plan_articles", wraps=integration.plan_articles) as planner, patch.object(
        integration, "validate_publication_dates", wraps=integration.validate_publication_dates) as dates, patch.object(
        planning, "_classify", wraps=planning._classify) as classify:
      result = self.plan()
      ledger.assert_called_once()
      parser.assert_called_once()
      planner.assert_called_once()
      classify.assert_called_once()
      dates.assert_called_once()
      parsed, plan, snapshots = dates.call_args.args
      self.assertIsInstance(parsed, tuple)
      self.assertIs(parsed[0], captured_parsed[0])
      self.assertIs(planner.call_args.args[0][0], parsed[0].record)
      self.assertIs(planner.call_args.args[1], captured_ledger[0])
      self.assertIs(planner.call_args.args[2], snapshots)
      self.assertIs(plan, result.article_plan)
      self.assertEqual(dates.call_args.kwargs["now"], NOW)
    with self.assertRaises(FrozenInstanceError):
      result.article_plan = None
    with self.assertRaises(FrozenInstanceError):
      result.inputs.images = ()

  def test_loader_uses_only_checked_source_set(self):
    self.write_source()
    checked = inspect_inputs(self.root)
    self.write_source(source(article_id=OTHER, state="hold"), name="later.md", commit=False)
    self.assertEqual(len(load_sources(checked)), 1)

  def test_loader_rejects_source_invalid_utf8(self):
    path = self.write_source()
    checked = inspect_inputs(self.root)
    path.write_bytes(b"\xff")
    with self.assertRaisesRegex(ValueError, "Cannot load Markdown"):
      load_sources(checked)

  def test_loader_rejects_source_symlink_and_traversal(self):
    path = self.write_source()
    checked = inspect_inputs(self.root)
    path.unlink()
    path.symlink_to(self.root / ".gitignore")
    with self.assertRaisesRegex(ValueError, "Refusing symlink"):
      load_sources(checked)
    path.unlink()
    os.mkfifo(path)
    with self.assertRaisesRegex(ValueError, "Refusing special file"):
      load_sources(checked)
    path.unlink()
    bad = replace(checked, sources=(CheckedSource(self.root / "articles" / ".." / ".gitignore", "hold"),))
    with self.assertRaisesRegex(ValueError, "Unsafe local path"):
      load_sources(bad)
    bad = replace(checked, sources=(CheckedSource(Path(self.temp.name) / "outside.md", "hold"),))
    with self.assertRaises(ValueError):
      load_sources(bad)

  def test_loader_rejects_snapshot_special_file(self):
    path = self.confirm()
    ledger = load_ledger(self.deploy)
    path.unlink()
    os.mkfifo(path)
    with self.assertRaisesRegex(ValueError, "Refusing special file"):
      load_released_snapshots(self.deploy, ledger)

  def test_snapshot_mapping_is_read_only(self):
    self.confirm()
    loaded = load_released_snapshots(self.deploy, load_ledger(self.deploy))
    with self.assertRaises(TypeError):
      loaded[ID] = "changed"
