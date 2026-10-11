"""隔離した記事 repository で画像 validation、diff、統合の安全性を検証します。"""

from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from converter import image_planning as images
from converter import repository_planning as integration
from converter.entry_guards import CheckedInputs, inspect_inputs
from converter.git_inspection import GuardError, head_entries
from converter.models import ImagePlanEntry, ImagePlanningResult
from test_article_integration import NOW, source
import test_article_integration as article_tests
from test_entry_guards import fixture_git, snapshot
import test_entry_guards as guard_tests


class DeletedImageGuardTests(unittest.TestCase):
  setUp = guard_tests.EntryGuardTests.setUp
  check = guard_tests.EntryGuardTests.check

  def test_unstaged_image_deletion(self):
    self.image.unlink()
    with self.assertRaisesRegex(GuardError, "Image"):
      self.check()

  def test_staged_image_deletion(self):
    fixture_git(self.root, "rm", "images/asset.png")
    with self.assertRaisesRegex(GuardError, "Image"):
      self.check()

  def test_committed_image_deletion(self):
    fixture_git(self.root, "rm", "images/asset.png")
    fixture_git(self.root, "commit", "-m", "delete")
    self.assertEqual(self.check().images, ())

  def test_rename_outside_images(self):
    fixture_git(self.root, "mv", "images/asset.png", "asset.png")
    with self.assertRaisesRegex(GuardError, "Image"):
      self.check()

  def test_rename_into_images(self):
    (self.root / "outside.png").write_bytes(b"outside")
    fixture_git(self.root, "add", ".")
    fixture_git(self.root, "commit", "-m", "outside")
    fixture_git(self.root, "mv", "outside.png", "images/new.png")
    with self.assertRaisesRegex(GuardError, "Image"):
      self.check()

  def test_unstaged_image_rename(self):
    self.image.rename(self.root / "elsewhere.png")
    with self.assertRaisesRegex(GuardError, "Image"):
      self.check()

  def test_unrelated_dirty_file_allowed(self):
    (self.root / ".gitignore").write_text("/_deploy/\nchanged\n")
    self.assertEqual(self.check().images, (self.image,))


class ImagePlanningTests(unittest.TestCase):
  setUp = article_tests.ArticleIntegrationTests.setUp
  commit = article_tests.ArticleIntegrationTests.commit
  save_ledger = article_tests.ArticleIntegrationTests.save_ledger
  write_source = article_tests.ArticleIntegrationTests.write_source
  confirm = article_tests.ArticleIntegrationTests.confirm

  def plan(self):
    before = snapshot(self.root)
    try:
      return integration.plan_repository(self.root, now=NOW)
    finally:
      self.assertEqual(snapshot(self.root), before)
      self.http_mock.assert_not_called()
      self.zenn_http_mock.assert_not_called()

  def put(self, root, name, content=b"content"):
    path = root / "images" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    self.commit(root)
    return path

  def changes(self):
    return self.plan().image_plan.images

  def test_empty_empty(self):
    for root in (self.root, self.deploy):
      (root / "images/asset.png").unlink()
      self.commit(root)
    result = self.plan()
    self.assertFalse(result.image_plan.has_image_changes)
    self.assertEqual(result.image_plan.images, ())

  def test_all_extensions(self):
    for extension in ("png", "jpg", "jpeg", "gif", "webp"):
      self.put(self.root, f"new.{extension}")
    self.assertEqual(len(self.changes()), 5)

  def test_uppercase_extensions(self):
    for extension in ("PNG", "JPG", "JPEG", "GIF", "WEBP", "Png"):
      with self.subTest(extension=extension):
        path = self.put(self.root, f"bad.{extension}")
        with self.assertRaisesRegex(GuardError, "extension"):
          self.plan()
        path.unlink()
        self.commit(self.root)

  def test_unsupported_extensions(self):
    for name in ("bad.svg", "bad.bmp", "bad.txt", "noextension"):
      with self.subTest(name=name):
        path = self.put(self.root, name)
        with self.assertRaisesRegex(GuardError, "extension"):
          self.plan()
        path.unlink()
        self.commit(self.root)

  def test_size_boundary_allowed(self):
    self.put(self.root, "large.png", b"x" * 3_000_000)
    self.assertEqual(len(self.changes()[0].content), 3_000_000)

  def test_size_boundary_rejected(self):
    self.put(self.root, "large.png", b"x" * 3_000_001)
    with self.assertRaisesRegex(GuardError, "exceeds"):
      self.plan()

  def test_normal_git_modes(self):
    path = self.put(self.root, "executable.jpg")
    path.chmod(0o755)
    fixture_git(self.root, "update-index", "--chmod=+x", "images/executable.jpg")
    self.commit(self.root)
    entries = head_entries(self.root)
    self.assertEqual(entries["images/asset.png"], ("100644", "blob"))
    self.assertEqual(entries["images/executable.jpg"], ("100755", "blob"))
    self.assertEqual(self.changes()[0].operation, "ADD")

  def test_git_symlink(self):
    (self.root / "images/link.png").symlink_to("asset.png")
    self.commit(self.root)
    with self.assertRaises(GuardError):
      self.plan()
    inputs = CheckedInputs(self.root, self.deploy, (), (self.root / "images/link.png",))
    with self.assertRaisesRegex(GuardError, "normal Git blob"):
      images.plan_images(inputs)

  def test_gitlink_even_without_filesystem_file(self):
    oid = fixture_git(self.root, "rev-parse", "HEAD").decode().strip()
    fixture_git(self.root, "update-index", "--add", "--cacheinfo", f"160000,{oid},images/submodule")
    fixture_git(self.root, "commit", "-m", "gitlink")
    (self.root / "images/submodule").mkdir()
    with self.assertRaisesRegex(GuardError, "normal Git blob"):
      self.plan()

  def test_missing_git_blob(self):
    inputs = inspect_inputs(self.root)
    path = self.root / "images/not-in-head.png"
    path.write_bytes(b"image")
    with self.assertRaisesRegex(GuardError, "normal Git blob"):
      images.plan_images(replace(inputs, images=(path,)))

  def test_filesystem_symlink_after_guard(self):
    inputs = inspect_inputs(self.root)
    path = self.root / "images/asset.png"
    path.unlink()
    path.symlink_to(self.root / ".gitignore")
    with self.assertRaisesRegex(GuardError, "Unsafe filesystem"):
      images.plan_images(inputs)

  def test_filesystem_special_after_guard(self):
    inputs = inspect_inputs(self.root)
    path = self.root / "images/asset.png"
    path.unlink()
    os.mkfifo(path)
    with self.assertRaisesRegex(GuardError, "Unsafe filesystem"):
      images.plan_images(inputs)

  def test_missing_file_after_guard(self):
    inputs = inspect_inputs(self.root)
    (self.root / "images/asset.png").unlink()
    with self.assertRaises(GuardError):
      images.plan_images(inputs)

  def test_unsafe_authoring_path(self):
    inputs = inspect_inputs(self.root)
    for path in (self.root / "images/../.gitignore", Path(self.temp.name) / "outside.png",
                 self.root / ".gitignore"):
      with self.subTest(path=path), self.assertRaises(GuardError):
        images.plan_images(replace(inputs, images=(path,)))

  def test_parent_symlink_after_guard(self):
    path = self.put(self.root, "nested/a.png")
    inputs = inspect_inputs(self.root)
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(self.deploy / "images", target_is_directory=True)
    with self.assertRaises(GuardError):
      images.plan_images(inputs)

  def test_spaces_newlines_nested(self):
    self.put(self.root, "nested/a b\nc.png")
    entry, = self.changes()
    self.assertEqual(entry.path, "images/nested/a b\nc.png")

  def test_no_additional_filename_constraint(self):
    self.put(self.root, "a b\t日本語:#?.png")
    self.assertEqual(self.changes()[0].path, "images/a b\t日本語:#?.png")

  def test_backslash_in_authoring_or_deploy_path_rejected(self):
    for root in (self.root, self.deploy):
      for name in ("a\\b.png", "a\\b/nested.png"):
        with self.subTest(root=root, name=name):
          path = self.put(root, name)
          with patch.object(integration, "finalize_publish_plan",
                            side_effect=AssertionError("Unexpected final plan")) as final:
            before = snapshot(self.root)
            with self.assertRaisesRegex(GuardError, "Unsafe.*image path"):
              integration.plan_publish(self.root, now=NOW)
            self.assertEqual(snapshot(self.root), before)
            final.assert_not_called()
          path.unlink()
          if path.parent != root / "images":
            path.parent.rmdir()
          self.commit(root)

  def test_unchanged_backslash_image_rejected(self):
    for root in (self.root, self.deploy):
      self.put(root, "a\\b.png")
    with patch.object(images, "_mirror_diff", side_effect=AssertionError("Unexpected diff")) as diff:
      with self.assertRaisesRegex(GuardError, "Unsafe image path"):
        self.plan()
      diff.assert_not_called()

  def test_deploy_backslash_empty_directory_rejected(self):
    (self.deploy / "images/a\\b").mkdir()
    with self.assertRaisesRegex(GuardError, "Unsafe deploy image path"):
      self.plan()

  def test_ignored_image(self):
    (self.root / ".gitignore").write_text("/_deploy/\n/images/ignored.png\n")
    self.commit(self.root)
    (self.root / "images/ignored.png").write_bytes(b"image")
    with self.assertRaisesRegex(GuardError, "Image"):
      self.plan()

  def test_untracked_image(self):
    (self.root / "images/untracked.png").write_bytes(b"image")
    with self.assertRaisesRegex(GuardError, "Image"):
      self.plan()

  def test_unchanged_invalid_image_is_validated(self):
    for root in (self.root, self.deploy):
      self.put(root, "invalid.svg")
    with self.assertRaisesRegex(GuardError, "extension"):
      self.plan()

  def test_validation_precedes_deploy_scan_and_diff(self):
    self.put(self.root, "bad.svg")
    with patch.object(images, "_deploy_images", side_effect=AssertionError("early deploy")) as deploy, patch.object(
        images, "_mirror_diff", side_effect=AssertionError("early diff")) as diff:
      with self.assertRaises(GuardError):
        self.plan()
      deploy.assert_not_called()
      diff.assert_not_called()

  def test_all_valid_inputs_checked_before_diff(self):
    self.put(self.root, "z.png")
    with patch.object(images, "_read_image", wraps=images._read_image) as read, patch.object(
        images, "_deploy_images", return_value={}) as deploy:
      def scanned(root):
        self.assertEqual(read.call_count, 2)
        return {}
      deploy.side_effect = scanned
      self.assertEqual(len(self.changes()), 2)

  def test_add(self):
    self.put(self.root, "new.png", b"added")
    self.assertEqual(self.changes(), (ImagePlanEntry("ADD", "images/new.png", b"added"),))

  def test_update_same_size_different_bytes(self):
    self.put(self.root, "asset.png", b"other")
    self.assertEqual(self.changes(), (ImagePlanEntry("UPDATE", "images/asset.png", b"other"),))

  def test_delete_committed_authoring_removal(self):
    (self.root / "images/asset.png").unlink()
    self.commit(self.root)
    self.assertEqual(self.changes(), (ImagePlanEntry("DELETE", "images/asset.png"),))

  def test_uncommitted_deletion_rejected(self):
    (self.root / "images/asset.png").unlink()
    with self.assertRaisesRegex(GuardError, "Image"):
      self.plan()

  def test_noop(self):
    self.assertEqual(self.changes(), ())

  def test_same_bytes_different_mtime(self):
    os.utime(self.root / "images/asset.png", ns=(1_000_000, 1_000_000))
    self.assertEqual(self.changes(), ())

  def test_mixed_deterministic_order(self):
    self.put(self.root, "z/new.png", b"new")
    self.put(self.root, "asset.png", b"updated")
    self.put(self.deploy, "b/removed.png", b"removed")
    expected = (ImagePlanEntry("UPDATE", "images/asset.png", b"updated"),
                ImagePlanEntry("DELETE", "images/b/removed.png"),
                ImagePlanEntry("ADD", "images/z/new.png", b"new"))
    self.assertEqual(self.changes(), expected)
    self.assertEqual(self.changes(), expected)

  def test_unused_images_retained(self):
    self.assertEqual(self.plan().article_plan.articles, ())
    self.assertEqual(self.changes(), ())

  def test_empty_directories_ignored(self):
    (self.root / "images/empty/nested").mkdir(parents=True)
    (self.deploy / "images/other-empty").mkdir()
    self.assertEqual(self.changes(), ())

  def test_missing_deploy_images(self):
    (self.deploy / "images/asset.png").unlink()
    (self.deploy / "images").rmdir()
    self.commit(self.deploy)
    self.assertEqual(self.changes(), (ImagePlanEntry("ADD", "images/asset.png", b"image"),))

  def _assert_deploy_gitlink_rejected(self, relative):
    oid = fixture_git(self.deploy, "rev-parse", "HEAD").decode().strip()
    if relative == "images":
      fixture_git(self.deploy, "rm", "-r", "images")
      (self.deploy / "images").mkdir()
    else:
      (self.deploy / relative).mkdir(parents=True)
    fixture_git(self.deploy, "update-index", "--add", "--cacheinfo", f"160000,{oid},{relative}")
    fixture_git(self.deploy, "commit", "-m", "deploy gitlink")
    self.assertEqual(head_entries(self.deploy)[relative], ("160000", "commit"))
    self.assertEqual(tuple((self.deploy / relative).iterdir()), ())
    before = snapshot(self.root)
    self.assertEqual(fixture_git(self.deploy, "--no-optional-locks", "status",
                                 "--porcelain=v1", "-z", "--untracked-files=all"), b"")
    with patch.object(images, "_mirror_diff", side_effect=AssertionError("Unexpected diff")) as diff:
      with self.assertRaisesRegex(GuardError, "Unsafe deploy image Git entry"):
        self.plan()
      diff.assert_not_called()
    self.assertEqual(snapshot(self.root), before)

  def test_deploy_empty_gitlink(self):
    self._assert_deploy_gitlink_rejected("images/submodule.png")

  def test_deploy_nested_empty_gitlink(self):
    self._assert_deploy_gitlink_rejected("images/nested/submodule.png")

  def test_deploy_images_root_gitlink(self):
    self._assert_deploy_gitlink_rejected("images")

  def test_deploy_normal_blob_modes(self):
    self.put(self.deploy, "executable.png", b"deploy-only")
    (self.deploy / "images/executable.png").chmod(0o755)
    fixture_git(self.deploy, "update-index", "--chmod=+x", "images/executable.png")
    self.commit(self.deploy)
    entries = head_entries(self.deploy)
    self.assertEqual(entries["images/asset.png"], ("100644", "blob"))
    self.assertEqual(entries["images/executable.png"], ("100755", "blob"))
    self.assertEqual(self.changes(), (ImagePlanEntry("DELETE", "images/executable.png"),))

  def test_authoring_validation_precedes_deploy_git_inspection(self):
    self.put(self.root, "invalid.svg")
    original = images.head_entries

    def inspect(root):
      self.assertEqual(root, self.root)
      return original(root)

    with patch.object(images, "head_entries", side_effect=inspect) as entries:
      with self.assertRaisesRegex(GuardError, "extension"):
        self.plan()
      entries.assert_called_once_with(self.root)

  def test_unsafe_deploy_symlink(self):
    (self.deploy / "images/link.png").symlink_to(self.root / ".gitignore")
    self.commit(self.deploy)
    with self.assertRaisesRegex(GuardError, "Unsafe deploy"):
      self.plan()

  def test_unsafe_deploy_directory_symlink(self):
    (self.deploy / "images/link").symlink_to(self.root, target_is_directory=True)
    self.commit(self.deploy)
    with self.assertRaisesRegex(GuardError, "Unsafe deploy"):
      self.plan()

  def test_unsafe_deploy_special_file(self):
    inputs = inspect_inputs(self.root)
    os.mkfifo(self.deploy / "images/pipe.png")
    with self.assertRaisesRegex(GuardError, "Unsafe deploy"):
      images.plan_images(inputs)

  def test_unsafe_deploy_images_root(self):
    inputs = inspect_inputs(self.root)
    (self.deploy / "images/asset.png").unlink()
    (self.deploy / "images").rmdir()
    (self.deploy / "images").symlink_to(self.root / "images", target_is_directory=True)
    with self.assertRaisesRegex(GuardError, "Unsafe deploy"):
      images.plan_images(inputs)

  def test_checked_input_set_is_authoritative(self):
    inputs = inspect_inputs(self.root)
    self.put(self.root, "later.png")
    self.assertEqual(images.plan_images(inputs).images, ())

  def test_content_fixed_after_planning(self):
    added = self.put(self.root, "new.png", b"new")
    updated = self.put(self.root, "asset.png", b"updated")
    result = self.plan().image_plan
    added.write_bytes(b"later")
    updated.write_bytes(b"later")
    self.assertEqual(tuple(e.content for e in result.images), (b"updated", b"new"))

  def test_immutable_result(self):
    self.put(self.root, "new.png")
    result = self.plan()
    with self.assertRaises(FrozenInstanceError):
      result.image_plan = None
    with self.assertRaises(FrozenInstanceError):
      result.image_plan.images = ()
    with self.assertRaises(FrozenInstanceError):
      result.image_plan.images[0].content = b"different"

  def test_image_only_preserves_article_state(self):
    self.confirm()
    self.write_source(source(release=3))
    self.put(self.root, "asset.png", b"replacement")
    before = snapshot(self.root)
    result = self.plan()
    self.assertEqual(result.article_plan.articles, ())
    self.assertTrue(result.image_plan.has_image_changes)
    self.assertEqual(snapshot(self.root), before)
    self.assertEqual(self.ledger["articles"][next(iter(self.ledger["articles"]))]["successful_release"], 3)
    self.assertEqual(self.ledger["pending_next_state"], {})

  def test_dirty_deploy_does_not_start_images(self):
    (self.deploy / "dirty").write_bytes(b"dirty")
    with patch.object(integration, "plan_images", side_effect=AssertionError("early images")) as planner:
      with self.assertRaisesRegex(GuardError, "Dirty deploy"):
        self.plan()
      planner.assert_not_called()

  def test_pending_does_not_start_images(self):
    self.ledger["pending_next_state"]["test"] = {}
    self.save_ledger()
    self.commit(self.deploy)
    with patch.object(integration, "plan_images", side_effect=AssertionError("early images")) as planner:
      with self.assertRaisesRegex(GuardError, "pending"):
        self.plan()
      planner.assert_not_called()

  def test_article_error_does_not_start_images(self):
    self.write_source(source(article_id="bad"))
    with patch.object(integration, "plan_images", side_effect=AssertionError("early images")) as planner:
      with self.assertRaisesRegex(ValueError, "UUID"):
        self.plan()
      planner.assert_not_called()

  def test_integration_once_and_preserves_identity(self):
    from converter import article_integration, planning
    with patch.object(integration, "plan_repository_articles", wraps=integration.plan_repository_articles) as articles, patch.object(
        integration, "plan_images", wraps=integration.plan_images) as planner, patch.object(
        article_integration, "inspect_inputs", wraps=inspect_inputs) as guards, patch.object(
        planning, "_classify", wraps=planning._classify) as classify:
      self.write_source()
      result = self.plan()
      articles.assert_called_once_with(self.root, now=NOW)
      planner.assert_called_once_with(result.inputs)
      guards.assert_called_once()
      classify.assert_called_once()

  def test_validation_error_does_not_return_partial_result(self):
    self.put(self.root, "invalid.svg")
    with patch.object(integration, "RepositoryPlanningResult", side_effect=AssertionError("partial result")) as result:
      with self.assertRaises(GuardError):
        self.plan()
      result.assert_not_called()


class ImageModelTests(unittest.TestCase):
  def test_reject_duplicate_operations(self):
    with self.assertRaises(ValueError):
      ImagePlanningResult((ImagePlanEntry("DELETE", "images/a.png"), ImagePlanEntry("ADD", "images/a.png", b"a")))

  def test_reject_mutable_entries_and_content(self):
    with self.assertRaises(TypeError):
      ImagePlanningResult([])
    with self.assertRaises(TypeError):
      ImagePlanEntry("ADD", "images/a.png", bytearray(b"a"))

  def test_entry_contracts(self):
    for operation, path, content in (("CREATE", "images/a.png", b"a"),
                                     ("DELETE", "images/a.png", b"a"),
                                     ("ADD", "images/a.png", None),
                                     ("DELETE", "images/../a.png", None),
                                     ("DELETE", "elsewhere/a.png", None)):
      with self.subTest(operation=operation, path=path), self.assertRaises((ValueError, TypeError)):
        ImagePlanEntry(operation, path, content)

  def test_model_sorts_paths(self):
    result = ImagePlanningResult((ImagePlanEntry("DELETE", "images/z.png"), ImagePlanEntry("DELETE", "images/a.png")))
    self.assertEqual(tuple(e.path for e in result.images), ("images/a.png", "images/z.png"))

  def test_all_operations_reject_backslash(self):
    for operation in ("ADD", "UPDATE", "DELETE"):
      for path in ("images/a\\b.png", "images/a\\b/nested.png"):
        with self.subTest(operation=operation, path=path), self.assertRaises(ValueError):
          ImagePlanEntry(operation, path, None if operation == "DELETE" else b"fixed")

  def test_all_operations_allow_other_unusual_characters(self):
    for operation in ("ADD", "UPDATE", "DELETE"):
      path = "images/nested/a b\nc\t日本語:#?.png"
      self.assertEqual(ImagePlanEntry(operation, path,
                       None if operation == "DELETE" else b"fixed").path, path)


class GitImageEntryTests(unittest.TestCase):
  def test_nul_parsing_with_spaces_newlines_tabs(self):
    output = b"100644 blob " + b"a" * 40 + b"\timages/a b\nc\td.png\0"
    with patch("converter.git_inspection.git", return_value=output) as command:
      self.assertEqual(head_entries(Path("/explicit")), {"images/a b\nc\td.png": ("100644", "blob")})
      command.assert_called_once_with(Path("/explicit"), "ls-tree", "-r", "-z", "HEAD")

  def test_malformed_entries(self):
    for output in (b"x", b"bad\0", b"100644 blob bad\tx.png\0",
                   b"100644 blob " + b"a" * 40 + b"\t../x.png\0"):
      with self.subTest(output=output), patch("converter.git_inspection.git", return_value=output):
        with self.assertRaises(GuardError):
          head_entries(Path("/explicit"))
