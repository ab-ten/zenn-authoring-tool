"""隔離した Git fixture で discard preflight と無変更性を検証します。"""

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from checker.discard_preflight import LocalValidation, preflight_discard
from checker.commit_readiness import CommitReadiness, check_commit_readiness
from checker import terminal_create, terminal_update, terminal_recreate, terminal_delete
from converter import git_inspection
from test_entry_guards import fixture_git, snapshot
from test_unified_check import IDS, SLUGS, CURRENT
import test_unified_check as fixture_data


class DiscardPreflightTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name) / 'article repo'
    self.root.mkdir()
    fixture_git(self.root, 'init', '-b', 'main')
    fixture_git(self.root, 'config', 'worktree.useRelativePaths', 'true')
    (self.root / '.gitignore').write_text('/_deploy/\n')
    fixture_git(self.root, 'add', '.')
    fixture_git(self.root, 'commit', '-m', 'initial')
    fixture_git(self.root, 'worktree', 'add', '--relative-paths', '-b', 'deploy', '_deploy')
    self.deploy = self.root / '_deploy'
    data = fixture_data.UnifiedCheckTests()
    data.setUp()
    self.addCleanup(data.doCleanups)
    shutil.copytree(data.root, self.deploy, dirs_exist_ok=True)
    self.path = self.deploy / '.zenn-authoring/ledger.json'
    self.pending_ledger = copy.deepcopy(data.ledger)
    previous = copy.deepcopy(data.ledger)
    previous['pending_next_state'] = {}
    self.save(previous)
    for index in (0, 1, 3):
      self.artifact(index).unlink()
    for index in (1, 2):
      self.artifact(index).write_bytes(self.released(index).read_bytes())
    self.commit('previous successful state')
    self.parent = fixture_git(self.deploy, 'rev-parse', 'HEAD').decode().strip()
    self.save(self.pending_ledger)
    for index in (0, 1, 3):
      self.artifact(index).write_bytes(f'current {index}\n'.encode())
    self.artifact(2).unlink()
    self.commit('deploy pending transitions')
    self.head = fixture_git(self.deploy, 'rev-parse', 'HEAD').decode().strip()
    for target in ('checker.zenn_http.urlopen', 'urllib.request.urlopen'):
      self.enterContext(patch(target, side_effect=AssertionError('Actual HTTP forbidden')))

  def artifact(self, index):
    return self.deploy / 'articles' / f'{SLUGS[index]}.md'

  def released(self, index):
    return self.deploy / '.zenn-authoring/released' / f'{IDS[index]}.md'

  def ledger(self):
    return json.loads(self.path.read_bytes())

  def save(self, ledger):
    self.path.write_text(json.dumps(ledger))

  def commit(self, message):
    fixture_git(self.deploy, 'add', '.')
    fixture_git(self.deploy, 'commit', '-m', message)

  def run_preflight(self, root=None):
    before = snapshot(self.root)
    real = git_inspection.git
    with patch.object(git_inspection, 'git', wraps=real) as calls:
      result = preflight_discard(self.root if root is None else root)
    self.assertEqual(snapshot(self.root), before)
    allowed = {'rev-parse', 'symbolic-ref', 'rev-list', 'ls-tree', 'ls-files', 'status', 'cat-file',
               'for-each-ref'}
    self.assertTrue(all(call.args[1] in allowed for call in calls.call_args_list))
    return result

  def assert_rejected(self, result=None):
    result = self.run_preflight() if result is None else result
    self.assertEqual(result.local_validation, LocalValidation.LOCAL_VALIDATION_REJECTED)
    self.assertIsNone(result.recovery_plan)
    self.assertTrue(result.diagnostics or result.snapshot_anomalies)
    self.assertTrue(result.remote_confirmation_pending)
    return result

  def test_mixed_plan_immutable_and_remote_pending(self):
    result = self.run_preflight()
    self.assertEqual(result.diagnostics, ())
    self.assertEqual(result.snapshot_anomalies, ())
    self.assertEqual(result.local_validation, LocalValidation.LOCAL_PREREQUISITES_VERIFIED)
    plan = result.recovery_plan
    self.assertEqual((result.head_commit, result.parent_commit), (self.head, self.parent))
    self.assertEqual(result.ledger_sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
    self.assertEqual(plan.article_paths, tuple(f'articles/{slug}.md' for slug in SLUGS[:4]))
    self.assertEqual(plan.restore_argv, ('git', 'restore', f'--source={self.parent}', '--worktree', '--', *plan.article_paths))
    self.assertTrue(result.remote_confirmation_pending)
    self.assertEqual(len(plan.remote_requirements), 6)
    self.assertIn('terminal', plan.remote_requirements[0])
    self.assertIn('Article API', plan.remote_requirements[1])
    self.assertTrue(any('[skip ci]' in text for text in plan.post_discard_guidance))
    with self.assertRaises(FrozenInstanceError):
      result.head_commit = 'changed'
    with self.assertRaises(FrozenInstanceError):
      plan.source_commit = 'changed'
    self.assertNotIn('COMMIT_READY', result.local_validation)

  def test_partial_success_all_operations(self):
    appliers = (terminal_create.apply_create_success, terminal_update.apply_update_success,
                terminal_delete.apply_delete_success, terminal_recreate.apply_recreate_success)
    for index, apply in enumerate(appliers):
      with self.subTest(operation=index):
        ledger_before = self.path.read_bytes()
        snapshot_before = self.released(index).read_bytes() if self.released(index).exists() else None
        if index == 2:
          apply(self.deploy, IDS[index])
        else:
          apply(self.deploy, IDS[index], CURRENT)
        result = self.run_preflight()
        self.assertEqual(result.local_validation, LocalValidation.LOCAL_PREREQUISITES_VERIFIED,
                         (result.diagnostics, result.snapshot_anomalies))
        self.assertNotIn(f'articles/{SLUGS[index]}.md', result.recovery_plan.article_paths)
        self.path.write_bytes(ledger_before)
        if snapshot_before is None:
          self.released(index).unlink()
        else:
          self.released(index).write_bytes(snapshot_before)

  def test_partial_collision(self):
    from checker.terminal_collision import apply_collision
    for index in (0, 3):
      apply_collision(self.deploy, IDS[index])
    result = self.run_preflight()
    self.assertIsNotNone(result.recovery_plan, result.diagnostics)
    self.assertEqual(len(result.pending), 2)

  def test_empty_pending(self):
    ledger = self.ledger()
    ledger['pending_next_state'] = {}
    self.save(ledger)
    self.assert_rejected()

  def test_all_entries_validated_before_rejection(self):
    ledger = self.ledger()
    ledger['pending_next_state'][IDS[0]]['release'] = False
    ledger['pending_next_state'][IDS[3]]['slug'] = '../unsafe'
    self.save(ledger)
    result = self.assert_rejected()
    self.assertEqual(len(result.pending), 4)
    self.assertTrue(result.pending[0].diagnostics)
    self.assertTrue(result.pending[3].diagnostics)

  def test_invalid_pending_values(self):
    original = self.ledger()
    cases = [(0, 'operation', 'bad'), (0, 'slug', 'short'), (0, 'release', 0),
             (0, 'release', 2), (0, 'baseline_observation', CURRENT),
             (0, 'state', 'hold'), (1, 'baseline_observation', {}),
             (1, 'release', 1), (2, 'release', 2), (3, 'release', 99)]
    for index, key, value in cases:
      with self.subTest(index=index, key=key):
        ledger = copy.deepcopy(original)
        ledger['pending_next_state'][IDS[index]][key] = value
        self.save(ledger)
        self.assert_rejected()
    ledger = copy.deepcopy(original)
    ledger['pending_next_state']['invalid UUID'] = ledger['pending_next_state'].pop(IDS[0])
    self.save(ledger)
    self.assert_rejected()

  def test_invalid_confirmed_and_slug_collision(self):
    original = self.ledger()
    for value in ('hold', None):
      ledger = copy.deepcopy(original)
      ledger['articles'][IDS[1]]['state'] = value
      self.save(ledger)
      self.assert_rejected()
    ledger = copy.deepcopy(original)
    ledger['pending_next_state'][IDS[0]]['slug'] = SLUGS[1]
    self.save(ledger)
    self.assert_rejected()

  def test_artifact_presence_for_each_operation(self):
    for index in (0, 1, 3):
      content = self.artifact(index).read_bytes()
      self.artifact(index).unlink()
      self.assert_rejected()
      self.artifact(index).write_bytes(content)
    self.artifact(2).write_bytes(b'unexpected')
    self.assert_rejected()

  def test_symlink_dangling_special_and_directory(self):
    target = self.artifact(0)
    content = target.read_bytes()
    for kind in ('symlink', 'dangling', 'fifo', 'directory'):
      with self.subTest(kind=kind):
        target.unlink()
        if kind in ('symlink', 'dangling'):
          target.symlink_to(self.artifact(1) if kind == 'symlink' else self.deploy / 'missing')
        elif kind == 'fifo':
          os.mkfifo(target)
        else:
          target.mkdir()
        self.assert_rejected()
        if kind == 'directory':
          target.rmdir()
        else:
          target.unlink()
        target.write_bytes(content)

  def test_unsafe_parent(self):
    articles = self.deploy / 'articles'
    moved = self.deploy / 'moved'
    articles.rename(moved)
    articles.symlink_to(moved, target_is_directory=True)
    self.assert_rejected()

  def test_dirty_staged_unrelated_and_direct_edit(self):
    self.artifact(0).write_bytes(b'edited directly')
    result = self.assert_rejected()
    self.assertTrue(any('Unexplained deploy change' in text for text in result.diagnostics))
    fixture_git(self.deploy, 'add', 'articles')
    self.assert_rejected()
    fixture_git(self.deploy, 'reset', '--hard', self.head)
    (self.deploy / 'unrelated').write_bytes(b'untracked')
    self.assert_rejected()

  def test_flags_both_worktrees(self):
    for target, path in ((self.root, '.gitignore'), (self.deploy, 'articles/' + SLUGS[0] + '.md')):
      for flag in ('assume-unchanged', 'skip-worktree'):
        fixture_git(target, 'update-index', '--' + flag, path)
        self.assert_rejected()
        fixture_git(target, 'update-index', '--no-' + flag, path)

  def test_snapshot_anomalies_for_all_operations(self):
    for index in range(4):
      with self.subTest(index=index):
        old = self.released(index).read_bytes() if self.released(index).exists() else None
        self.released(index).write_bytes(b'interrupted or corrupt snapshot')
        result = self.assert_rejected()
        self.assertTrue(result.snapshot_anomalies)
        self.assertTrue(any('article restore alone is insufficient' in text for text in result.snapshot_anomalies))
        if old is None:
          self.released(index).unlink()
        else:
          self.released(index).write_bytes(old)

  def test_missing_snapshot(self):
    self.released(1).unlink()
    self.assertTrue(self.assert_rejected().snapshot_anomalies)

  def test_orphan_create_readiness_gap_reproduced_without_production_mutation(self):
    # fixture のみで interrupted CREATE と仮想 discard/restore を再現します。
    self.released(0).write_bytes(self.artifact(0).read_bytes())
    result = self.assert_rejected()
    self.assertTrue(any('orphan' in text for text in result.snapshot_anomalies))
    ledger = self.ledger()
    ledger['pending_next_state'] = {}
    self.save(ledger)
    fixture_git(self.deploy, 'restore', '--source=' + self.parent, '--worktree', '--',
                *(f'articles/{slug}.md' for slug in SLUGS[:4]))
    self.assertTrue(self.released(0).exists())
    readiness = check_commit_readiness(self.deploy)
    self.assertEqual(readiness.result, CommitReadiness.COMMIT_READY, readiness.diagnostics)

  def test_later_commit_and_parent_pending(self):
    (self.deploy / 'later').write_bytes(b'new commit')
    self.commit('later unrelated commit')
    self.assert_rejected()

  def test_head_without_pending(self):
    fixture_git(self.deploy, 'reset', '--hard', self.parent)
    self.save(self.pending_ledger)
    for index in (0, 1, 3):
      self.artifact(index).write_bytes(b'pending')
    self.artifact(2).unlink()
    self.assert_rejected()

  def test_current_pending_conflicts_with_head(self):
    ledger = self.ledger()
    ledger['pending_next_state'][IDS[1]]['baseline_observation'] = CURRENT
    self.save(ledger)
    self.assert_rejected()
    ledger = copy.deepcopy(self.pending_ledger)
    ledger['pending_next_state'][IDS[4]] = {**ledger['pending_next_state'][IDS[0]], 'slug': SLUGS[4]}
    self.save(ledger)
    self.artifact(4).write_bytes(b'extra')
    self.assert_rejected()

  def test_operation_diff_contradiction(self):
    fixture_git(self.deploy, 'reset', '--hard', self.parent)
    self.save(self.pending_ledger)
    for index in (0, 3):
      self.artifact(index).write_bytes(b'pending')
    self.artifact(2).unlink()
    self.commit('update without article change')
    self.assert_rejected()

  def test_recreate_same_content_release_and_slug(self):
    fixture_git(self.deploy, 'reset', '--hard', self.parent)
    ledger = copy.deepcopy(self.pending_ledger)
    ledger['pending_next_state'][IDS[3]]['release'] = 1
    self.save(ledger)
    for index in (0, 1):
      self.artifact(index).write_bytes(b'pending')
    self.artifact(3).write_bytes(self.released(3).read_bytes())
    self.artifact(2).unlink()
    self.commit('recreate same content')
    result = self.run_preflight()
    self.assertIsNotNone(result.recovery_plan, result.diagnostics)
    ledger['pending_next_state'][IDS[3]]['release'] = 2
    self.save(ledger)
    self.commit('invalid later commit')
    self.assert_rejected()

  def test_recreate_content_release_contradiction_at_introduction(self):
    fixture_git(self.deploy, 'reset', '--hard', self.parent)
    ledger = copy.deepcopy(self.pending_ledger)
    ledger['pending_next_state'][IDS[3]]['release'] = 1
    self.save(ledger)
    for index in (0, 1, 3):
      self.artifact(index).write_bytes(b'new content')
    self.artifact(2).unlink()
    self.commit('invalid recreate release')
    self.assert_rejected()

  def test_parent_snapshot_unavailable(self):
    fixture_git(self.deploy, 'reset', '--hard', self.parent)
    self.released(1).unlink()
    self.commit('broken baseline')
    self.save(self.pending_ledger)
    for index in (0, 1, 3):
      self.artifact(index).write_bytes(b'pending')
    self.artifact(2).unlink()
    self.commit('pending with missing baseline')
    result = self.assert_rejected()
    self.assertTrue(result.snapshot_anomalies)

  def test_wrong_repository_branch_links_development(self):
    self.assert_rejected(self.run_preflight(self.deploy))
    self.assert_rejected(preflight_discard(None))
    self.assert_rejected(preflight_discard(Path(__file__).resolve().parents[2]))
    fixture_git(self.deploy, 'checkout', '-b', 'wrong')
    self.assert_rejected()
    fixture_git(self.deploy, 'checkout', 'deploy')
    link = self.deploy / '.git'
    link.write_text('gitdir: ../missing\n')
    self.assert_rejected()

  def test_unborn_deploy(self):
    fixture_git(self.deploy, 'checkout', '--orphan', 'unborn')
    fixture_git(self.deploy, 'symbolic-ref', 'HEAD', 'refs/heads/deploy')
    fixture_git(self.root, 'update-ref', '-d', 'refs/heads/deploy')
    self.assert_rejected()

  def test_missing_parent_and_merge_commit(self):
    with patch('checker.discard_preflight.commit_and_parent', side_effect=ValueError('Missing parent')):
      self.assert_rejected()
    fixture_git(self.deploy, 'checkout', '-b', 'side', self.parent)
    (self.deploy / 'side').write_bytes(b'side')
    self.commit('side')
    fixture_git(self.deploy, 'checkout', 'deploy')
    fixture_git(self.deploy, 'merge', '--no-ff', 'side', '-m', 'merge')
    self.assert_rejected()

  def test_malformed_outputs_and_missing_objects(self):
    real = git_inspection.git
    for command, output in (('rev-list', b'bad\n'), ('ls-tree', b'bad\0'),
                            ('cat-file', b'not JSON'), ('ls-files', b'H bad')):
      def run(root, *args):
        return output if args[0] == command else real(root, *args)
      with self.subTest(command=command), patch.object(git_inspection, 'git', side_effect=run):
        self.assert_rejected()
    def missing(root, *args):
      if args[0] == 'cat-file':
        raise git_inspection.GuardError('Missing object')
      return real(root, *args)
    with patch.object(git_inspection, 'git', side_effect=missing):
      self.assert_rejected()

  def test_duplicate_json_key(self):
    self.path.write_bytes(b'{"articles":{},"articles":{},"pending_next_state":{}}')
    self.assert_rejected()

  def test_actual_missing_git_blob_object(self):
    oid = fixture_git(self.deploy, 'rev-parse', f'HEAD:articles/{SLUGS[0]}.md').decode().strip()
    object_path = self.root / '.git/objects' / oid[:2] / oid[2:]
    self.assertTrue(object_path.is_file())
    object_path.unlink()
    self.assert_rejected()

  def test_changed_head_or_ledger_detected(self):
    real = git_inspection.commit_and_parent
    with patch('checker.discard_preflight.commit_and_parent',
               side_effect=[real(self.deploy), ('0' * 40, self.parent)]):
      self.assert_rejected()

  def test_ledger_change_during_validation(self):
    real = Path.read_bytes
    reads = 0
    def read(path):
      nonlocal reads
      if path == self.path:
        reads += 1
        if reads == 2:
          return real(path) + b'\n'
      return real(path)
    with patch.object(Path, 'read_bytes', read):
      result = preflight_discard(self.root)
    self.assert_rejected(result)
    self.assertIn('HEAD or ledger changed during validation', result.diagnostics)

  def test_git_operation_metadata_and_replacement_refs(self):
    metadata = Path(fixture_git(self.deploy, 'rev-parse', '--absolute-git-dir').decode().strip())
    for name in ('MERGE_HEAD', 'index.lock'):
      path = metadata / name
      path.write_bytes(b'in progress')
      self.assert_rejected()
      path.unlink()
    fixture_git(self.deploy, 'replace', self.head, self.parent)
    self.assert_rejected()

  def test_symlink_git_metadata(self):
    metadata = Path(fixture_git(self.deploy, 'rev-parse', '--absolute-git-dir').decode().strip())
    head = metadata / 'HEAD'
    external = self.root / 'head-copy'
    external.write_bytes(head.read_bytes())
    head.unlink()
    head.symlink_to(external)
    self.assert_rejected()

  def test_unexplained_pending_removal_and_metadata_change(self):
    original = self.ledger()
    ledger = copy.deepcopy(original)
    del ledger['pending_next_state'][IDS[0]]
    self.save(ledger)
    self.assert_rejected()
    ledger = copy.deepcopy(original)
    ledger['extra'] = 'edited'
    self.save(ledger)
    self.assert_rejected()

  def test_multiple_partial_success_with_remaining_delete(self):
    for index, apply in ((0, terminal_create.apply_create_success),
                         (1, terminal_update.apply_update_success),
                         (3, terminal_recreate.apply_recreate_success)):
      apply(self.deploy, IDS[index], CURRENT)
    result = self.run_preflight()
    self.assertIsNotNone(result.recovery_plan, result.diagnostics)
    self.assertEqual(result.recovery_plan.article_paths, (f'articles/{SLUGS[2]}.md',))

  def test_actual_interrupted_snapshot_replacement(self):
    for index, module, apply in (
        (0, terminal_create, terminal_create.apply_create_success),
        (1, terminal_update, terminal_update.apply_update_success),
        (3, terminal_recreate, terminal_recreate.apply_recreate_success)):
      old = self.released(index).read_bytes() if self.released(index).exists() else None
      before = self.path.read_bytes()
      real = module.atomic_replace_bytes
      def interrupted(path, content):
        if path == self.path:
          raise OSError('Interrupted ledger replacement')
        return real(path, content)
      with patch.object(module, 'atomic_replace_bytes', side_effect=interrupted):
        with self.assertRaises(OSError):
          apply(self.deploy, IDS[index], CURRENT)
      self.assertEqual(before, self.path.read_bytes())
      self.assertEqual(self.released(index).read_bytes(), self.artifact(index).read_bytes())
      self.assertTrue(self.assert_rejected().snapshot_anomalies)
      if old is None:
        self.released(index).unlink()
      else:
        self.released(index).write_bytes(old)


class GitRecoveryInspectionTests(unittest.TestCase):
  def test_commit_parent_malformed_missing_root_merge(self):
    head, parent = 'a' * 40, 'b' * 40
    outputs = [(b'bad\n',), (head.encode() + b'\n', head.encode() + b'\n'),
               (head.encode() + b'\n', f'{head} {parent} {parent}\n'.encode()),
               (head.encode() + b'\n', f'{head} {parent}\n'.encode(), b'bad\n')]
    for values in outputs:
      with patch.object(git_inspection, 'git', side_effect=values):
        with self.assertRaises(ValueError):
          git_inspection.commit_and_parent(Path('/explicit'))

  def test_tree_rejects_unsafe_and_malformed_entries(self):
    oid = b'a' * 40
    for raw in (b'bad', b'100644 blob ' + oid + b'\t../bad\0',
                b'120000 blob ' + oid + b'\tlink\0',
                b'100644 blob ' + oid + b'\tback\\slash\0',
                b'100644 blob bad\tfile\0', b'100644 tree ' + oid + b'\tfile\0'):
      with self.subTest(raw=raw), patch.object(git_inspection, 'git', return_value=raw):
        with self.assertRaises(ValueError):
          git_inspection.commit_tree(Path('/explicit'), 'a' * 40)
