"""隔離した記事 repository と fixture で read-only guards を検証します。"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from converter.entry_guards import inspect_inputs, validate_inputs
from converter.git_inspection import GuardError, StatusEntry, git, inspect_status, parse_status
from converter.source_parser import minimal_source_state


def source(state="active"):
  return f"---\ntitle: Test\n---\n<!-- #id:test -->\n<!-- #state:{state} -->\nBody\n"


def fixture_git(root, *args):
  env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
  env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
             GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
             GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
  return subprocess.run(["git", "-C", str(root), *args], env=env,
                        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def snapshot(root):
  return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                    p.stat().st_mtime_ns, p.stat().st_mode)
          for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class StatusTests(unittest.TestCase):
  def test_nul_paths_and_rename(self):
    self.assertEqual(parse_status(b" M articles/a b\nx.md\0R  articles/new.md\0articles/old.md\0"),
                     (StatusEntry(" M", "articles/a b\nx.md"),
                      StatusEntry("R ", "articles/new.md", "articles/old.md")))

  def test_untracked_directory_record(self):
    self.assertEqual(parse_status(b"?? _deploy/\0"), (StatusEntry("??", "_deploy/"),))

  def test_malformed(self):
    for raw in (b" M x", b" M \0", b"ZZ x\0", b"R  x\0", b" M ../x\0",
                b" M /x\0", b" M x\0\0", b"\xffM x\0", b" Mx\0"):
      with self.subTest(raw=raw), self.assertRaises(GuardError):
        parse_status(raw)

  def test_git_command_failure(self):
    with patch("converter.git_inspection.subprocess.run", side_effect=OSError("fail")):
      with self.assertRaises(GuardError):
        git(Path("/explicit"), "status")
    with patch("converter.git_inspection.subprocess.run") as run:
      run.return_value.returncode = 1
      run.return_value.stderr = b"failure"
      with self.assertRaises(GuardError):
        git(Path("/explicit"), "status")

  def test_status_command_is_read_only(self):
    with patch("converter.git_inspection.git", return_value=b" M x\0") as run:
      self.assertEqual(inspect_status(Path("/explicit")), (StatusEntry(" M", "x"),))
      run.assert_called_once_with(Path("/explicit"), "status", "--porcelain=v1", "-z",
                                  "--untracked-files=all")

  def test_git_environment_is_sanitized(self):
    with patch.dict(os.environ, {"GIT_DIR": "/wrong", "GIT_WORK_TREE": "/wrong",
                                "GIT_CONFIG_COUNT": "1"}), patch(
        "converter.git_inspection.subprocess.run") as run:
      run.return_value.returncode = 0
      run.return_value.stdout = b""
      git(Path("/explicit"), "status")
      env = run.call_args.kwargs["env"]
      self.assertNotIn("GIT_DIR", env)
      self.assertNotIn("GIT_WORK_TREE", env)
      self.assertNotIn("GIT_CONFIG_COUNT", env)
      self.assertEqual(run.call_args.args[0][:4], ["git", "--no-optional-locks", "-C", "/explicit"])
      self.assertNotIn("shell", run.call_args.kwargs)

  def test_pure_cleanliness(self):
    validate_inputs((("a", "hold"),), (), frozenset(), (StatusEntry("??", "a"),))
    for state in ("active", "erased", "invalid"):
      with self.subTest(state=state), self.assertRaises(GuardError):
        validate_inputs((("a", state),), (), frozenset(), ())


class MinimalStateTests(unittest.TestCase):
  def test_no_full_validation(self):
    text = source("hold").replace("title: Test", "[invalid YAML")
    text = text.replace("Body", "<!-- #release:not-a-number -->\n<!-- #observation-override:not JSON -->")
    self.assertEqual(minimal_source_state(text), "hold")

  def test_missing_invalid_and_duplicate(self):
    for text in (source("bad"), source().replace("<!-- #state:active -->", ""),
                 source().replace("Body", "<!-- #state:hold -->"),
                 source().replace("<!-- #state:active -->", "<!-- #state active -->"),
                 source().replace("<!-- #state:active -->", "<!-- ordinary -->\n<!-- #state:active -->"),
                 "body", "---\nno closing"):
      with self.subTest(text=text), self.assertRaises(ValueError):
        minimal_source_state(text)


class EntryGuardTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name) / "article repo"
    self.root.mkdir()
    fixture_git(self.root, "init", "-b", "main")
    fixture_git(self.root, "config", "worktree.useRelativePaths", "true")
    (self.root / "articles").mkdir()
    (self.root / "images").mkdir()
    self.article = self.root / "articles" / "source.md"
    self.article.write_text(source())
    self.image = self.root / "images" / "asset.png"
    self.image.write_bytes(b"image")
    (self.root / ".gitignore").write_text("/_deploy/\n/images/ignored.png\n")
    fixture_git(self.root, "add", ".")
    fixture_git(self.root, "commit", "-m", "initial")
    fixture_git(self.root, "worktree", "add", "--relative-paths", "-b", "deploy", "_deploy")
    self.deploy = self.root / "_deploy"
    (self.deploy / ".zenn-authoring").mkdir()
    self.ledger = self.deploy / ".zenn-authoring" / "ledger.json"
    self.ledger.write_text(json.dumps({"articles": {}, "pending_next_state": {}}))
    fixture_git(self.deploy, "add", ".")
    fixture_git(self.deploy, "commit", "-m", "ledger")

  def check(self):
    before = snapshot(self.root)
    try:
      return inspect_inputs(self.root)
    finally:
      self.assertEqual(snapshot(self.root), before)

  def test_clean_and_isolated(self):
    result = self.check()
    self.assertEqual(result.authoring_root, self.root)
    self.assertEqual(result.deploy_root, self.deploy)
    self.assertEqual(result.sources[0].state, "active")
    self.assertEqual(result.images, (self.image,))
    self.assertTrue(self.root.is_relative_to(Path(self.temp.name)))
    self.assertNotEqual(self.root, Path(__file__).resolve().parents[2])

  def test_clean_erased(self):
    self.article.write_text(source("erased"))
    fixture_git(self.root, "add", ".")
    fixture_git(self.root, "commit", "-m", "erased")
    self.assertEqual(self.check().sources[0].state, "erased")

  def test_dirty_hold_staged_and_unstaged(self):
    self.article.write_text(source("hold"))
    fixture_git(self.root, "add", ".")
    self.article.write_text(source("hold") + "edit")
    self.assertEqual(self.check().sources[0].state, "hold")

  def test_untracked_hold(self):
    (self.root / "articles" / "new.md").write_text(source("hold"))
    self.assertEqual(len(self.check().sources), 2)

  def test_untracked_active(self):
    (self.root / "articles" / "new.md").write_text(source())
    with self.assertRaisesRegex(GuardError, "new.md"):
      self.check()

  def test_staged_new_active(self):
    (self.root / "articles" / "new.md").write_text(source())
    fixture_git(self.root, "add", ".")
    with self.assertRaises(GuardError):
      self.check()

  def test_staged_modified_active(self):
    self.article.write_text(source() + "edit")
    fixture_git(self.root, "add", ".")
    with self.assertRaises(GuardError):
      self.check()

  def test_unstaged_modified_active(self):
    self.article.write_text(source() + "edit")
    with self.assertRaises(GuardError):
      self.check()

  def test_staged_deletion(self):
    self.article.unlink()
    fixture_git(self.root, "add", "-u")
    with self.assertRaisesRegex(GuardError, "removed"):
      self.check()

  def test_unstaged_deletion(self):
    self.article.unlink()
    with self.assertRaisesRegex(GuardError, "removed"):
      self.check()

  def test_spaces_newlines_nested_and_unrelated_dirty(self):
    nested = self.root / "articles" / "nested"
    nested.mkdir()
    self.article.rename(nested / "a b\nc.md")
    images = self.root / "images" / "nested"
    images.mkdir()
    self.image.rename(images / "a b\nc.png")
    fixture_git(self.root, "add", ".")
    fixture_git(self.root, "commit", "-m", "nested")
    (self.root / "notes.txt").write_text("unrelated")
    (self.root / ".gitignore").write_text("changed")
    self.assertEqual(len(self.check().sources), 1)

  def test_deploy_dirty_cases(self):
    for kind in ("staged", "unstaged", "untracked"):
      with self.subTest(kind=kind):
        path = self.deploy / ("unknown file\nname" if kind == "untracked" else "articles/source.md")
        original = path.read_bytes() if path.exists() else None
        path.write_bytes(b"dirty")
        if kind == "staged":
          fixture_git(self.deploy, "add", ".")
        with self.assertRaisesRegex(GuardError, "Dirty deploy"):
          self.check()
        if original is None:
          path.unlink()
        else:
          path.write_bytes(original)
        if kind == "staged":
          fixture_git(self.deploy, "add", ".")

  def test_images_dirty_cases(self):
    for kind in ("unstaged", "staged", "untracked", "ignored"):
      with self.subTest(kind=kind):
        path = self.image if kind in ("unstaged", "staged") else self.root / "images" / (
          "ignored.png" if kind == "ignored" else "new.png")
        path.write_bytes(b"dirty")
        if kind == "staged":
          fixture_git(self.root, "add", ".")
        with self.assertRaisesRegex(GuardError, "Image"):
          self.check()
        if path == self.image:
          path.write_bytes(b"image")
          if kind == "staged":
            fixture_git(self.root, "add", ".")
        else:
          path.unlink()

  def test_invalid_or_missing_state(self):
    for text in (source("bad"), source().replace("<!-- #state:active -->", "")):
      self.article.write_text(text)
      with self.assertRaisesRegex(GuardError, "source state"):
        self.check()

  def test_ledger_missing_invalid_and_pending(self):
    for content in (None, "invalid", "{}", '{"articles":[],"pending_next_state":{}}',
                    json.dumps({"articles": {}, "pending_next_state": {"id": {}}})):
      with self.subTest(content=content):
        if content is None:
          self.ledger.unlink()
        else:
          self.ledger.write_text(content)
        fixture_git(self.deploy, "add", "-A")
        fixture_git(self.deploy, "commit", "-m", "ledger case")
        with self.assertRaises(GuardError):
          self.check()

  def test_pending_after_worktree_checks_and_no_full_processing(self):
    self.ledger.write_text(json.dumps({"articles": {}, "pending_next_state": {"id": {}}}))
    fixture_git(self.deploy, "add", ".")
    fixture_git(self.deploy, "commit", "-m", "pending")
    with patch("converter.source_parser.parse_source", side_effect=AssertionError("full parse")), patch(
        "converter.planning.plan_articles", side_effect=AssertionError("planning")):
      with self.assertRaisesRegex(GuardError, "pending_next_state"):
        self.check()
      self.article.write_text(source() + "dirty")
      with patch("converter.entry_guards.read_ledger", side_effect=AssertionError("early ledger")):
        with self.assertRaisesRegex(GuardError, "Source must"):
          self.check()
      (self.deploy / "untracked").write_text("dirty")
      with patch("converter.entry_guards.minimal_source_state", side_effect=AssertionError("early parse")):
        with self.assertRaisesRegex(GuardError, "Dirty deploy"):
          self.check()

  def test_missing_incorrect_and_development_roots(self):
    for root in (None, self.root / "missing", self.root / "articles",
                 Path(__file__).resolve().parents[2]):
      with self.subTest(root=root), self.assertRaises(GuardError):
        inspect_inputs(root)

  def test_development_layout_rejected_even_with_valid_worktrees(self):
    (self.root / "docs").mkdir()
    (self.root / "docs" / "architecture.md").write_text("spec")
    path = self.root / "publisher" / "converter"
    path.mkdir(parents=True)
    (path / "source_parser.py").write_text("implementation")
    with self.assertRaisesRegex(GuardError, "Development repository"):
      self.check()

  def test_wrong_branches(self):
    fixture_git(self.root, "branch", "-m", "wrong")
    with self.assertRaisesRegex(GuardError, "branch"):
      self.check()
    fixture_git(self.root, "branch", "-m", "master")
    self.check()
    fixture_git(self.deploy, "branch", "-m", "wrong-deploy")
    with self.assertRaisesRegex(GuardError, "branch"):
      self.check()

  def test_unrelated_deploy_repository(self):
    fixture_git(self.root, "worktree", "remove", "_deploy")
    self.deploy.mkdir()
    fixture_git(self.deploy, "init", "-b", "deploy")
    (self.deploy / "file").write_text("file")
    fixture_git(self.deploy, "add", ".")
    fixture_git(self.deploy, "commit", "-m", "unrelated")
    with self.assertRaisesRegex(GuardError, "metadata"):
      self.check()

  def test_identity_failure(self):
    with patch("converter.git_inspection.git", side_effect=GuardError("identity unavailable")):
      with self.assertRaisesRegex(GuardError, "identity unavailable"):
        self.check()

  def test_inconsistent_links(self):
    (self.deploy / ".git").write_text("gitdir: ../missing\n")
    with self.assertRaises(GuardError):
      self.check()

  def test_symlink_and_special_inputs(self):
    for directory in ("images", "articles"):
      path = self.root / directory / "unsafe"
      path.symlink_to(self.article)
      with self.assertRaisesRegex(GuardError, "Unsafe"):
        self.check()
      path.unlink()
      os.mkfifo(path)
      with self.assertRaisesRegex(GuardError, "Unsafe"):
        self.check()
      path.unlink()

  def test_inherited_git_redirection(self):
    with patch.dict(os.environ, {"GIT_DIR": "/missing", "GIT_WORK_TREE": "/wrong",
                                "GIT_INDEX_FILE": "/missing/index"}):
      self.check()
