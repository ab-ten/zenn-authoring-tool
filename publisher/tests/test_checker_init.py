"""隔離した orphan deploy repository で初期化と中断復旧を検証します。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import cli, initialization
from checker.local_state import ledger_bytes, safe_path
from checker.zenn_user_http import ZennUser
from converter import git_inspection
from converter.entry_guards import inspect_inputs
from converter.git_inspection import GuardError, identify_worktrees, inspect_status
from test_checker_cli import invoke
from test_entry_guards import fixture_git, snapshot
from test_unified_check import IDS, SLUGS, BASELINE
from test_zenn_http import FakeResponse


EMPTY = ledger_bytes({'articles': {}, 'pending_next_state': {}})
USER = ZennUser(12345, 'resolved_user')
CONFIG = ledger_bytes({'zenn_user': {'id': USER.id, 'username': USER.username}})


class InitArgumentsTests(unittest.TestCase):
  def test_help_and_argument_errors(self):
    cases = [(['--help'], 0), (['init', '--help'], 0), (['init'], 2),
             (['init', '--article-root', '/explicit'], 2), (['init', 'example'], 2),
             (['init', 'example', '--article-root'], 2),
             (['init', 'example', '--article-root', ''], 2),
             (['init', 'example', '--article-ro', '/explicit'], 2),
             (['init', 'example', '--article-root', '/explicit', '--force'], 2)]
    with patch.object(cli, 'initialize') as run, patch.object(Path, 'cwd', side_effect=AssertionError('CWD')):
      for args, expected in cases:
        with self.subTest(args=args):
          code, stdout, stderr = invoke(args)
          self.assertEqual(code, expected)
          self.assertIn('usage:', stdout if code == 0 else stderr)
          if args == ['init', '--help']:
            self.assertIn('USERNAME', stdout)
            self.assertIn('--article-root PATH', stdout)
          if args == ['--help']:
            self.assertIn('init', stdout)
            self.assertIn('check', stdout)
      run.assert_not_called()

  def test_explicit_relative_root_is_forwarded_once(self):
    result = initialization.InitResult(Path('/deploy'), USER, True)
    with patch.object(cli, 'initialize', return_value=result) as run:
      self.assertEqual(invoke(['init', 'example', '--article-root', '../article repo'])[0], 0)
    run.assert_called_once_with(Path('../article repo'), 'example')

  def test_fatal_and_interrupt_codes(self):
    for error, expected in ((ValueError('invalid state'), 2), (KeyboardInterrupt(), 130),
                            (RuntimeError('private details'), 2)):
      with self.subTest(error=error), patch.object(cli, 'initialize', side_effect=error) as run:
        code, stdout, stderr = invoke(['init', 'example', '--article-root', '/explicit'])
        self.assertEqual(code, expected)
        self.assertEqual(stdout, '')
        self.assertIn('不完全な状態を commit/push しない', stderr)
        self.assertNotIn('private details', stderr)
        run.assert_called_once()

  def test_module_entrypoint_init_help(self):
    result = subprocess.run([sys.executable, '-m', 'checker', 'init', '--help'],
                            capture_output=True, text=True, check=False)
    self.assertEqual(result.returncode, 0)
    self.assertIn('USERNAME', result.stdout)
    self.assertIn('--article-root PATH', result.stdout)
    self.assertEqual(result.stderr, '')


class InitIntegrationTests(unittest.TestCase):
  def setUp(self):
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    self.root = Path(temp.name) / 'article repo'
    self.root.mkdir()
    fixture_git(self.root, 'init', '-b', 'main')
    fixture_git(self.root, 'config', 'worktree.useRelativePaths', 'true')
    (self.root / '.gitignore').write_text('/_deploy/\n')
    fixture_git(self.root, 'add', '.')
    fixture_git(self.root, 'commit', '-m', 'initial authoring')
    fixture_git(self.root, 'worktree', 'add', '--orphan', '-b', 'deploy', '_deploy')
    self.deploy = self.root / '_deploy'
    self.meta = self.deploy / '.zenn-authoring'
    self.meta.mkdir()
    self.ledger = self.meta / 'ledger.json'
    self.config = self.meta / 'config.json'
    self.lookup = Mock(return_value=USER)
    self.calls = self.enterContext(patch.object(cli, 'initialize',
      side_effect=lambda root, username: initialization.initialize(root, username, self.lookup)))
    for target in ('checker.zenn_user_http.urlopen', 'checker.zenn_http.urlopen', 'urllib.request.urlopen'):
      run = self.enterContext(patch(target, side_effect=AssertionError('Real HTTP')))
      self.addCleanup(run.assert_not_called)

  def init(self):
    metadata = snapshot(self.root / '.git')
    with patch.object(git_inspection, 'git', wraps=git_inspection.git) as run:
      result = invoke(['init', 'requested_user', '--article-root', str(self.root)])
    self.assertEqual(snapshot(self.root / '.git'), metadata)
    self.assertTrue(all(call.args[1] in ('rev-parse', 'symbolic-ref', 'show-ref', 'ls-files', 'status')
                        for call in run.call_args_list))
    return result

  def commit(self):
    fixture_git(self.deploy, 'add', '.')
    fixture_git(self.deploy, 'commit', '--allow-empty', '-m', 'Initialize Zenn management state [skip ci]')

  def initialized(self):
    self.assertEqual(self.init()[0], 0)
    self.commit()
    self.lookup.reset_mock()
    self.calls.reset_mock()

  def assert_rejected_unchanged(self):
    before = snapshot(self.deploy)
    code, stdout, stderr = self.init()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('ERROR', stderr)
    self.assertEqual(snapshot(self.deploy), before)

  def test_end_to_end_unborn_then_initial_commit(self):
    with self.assertRaises(GuardError):
      identify_worktrees(self.root)
    self.assertEqual(identify_worktrees(self.root, allow_unborn_deploy=True).deploy, self.deploy)
    code, stdout, stderr = self.init()
    self.assertEqual(code, 0, stderr)
    self.assertEqual(stderr, '')
    self.assertEqual(self.ledger.read_bytes(), EMPTY)
    self.assertEqual(self.config.read_bytes(), CONFIG)
    self.lookup.assert_called_once_with('requested_user')
    self.calls.assert_called_once()
    for text in ('Git status', '差分', 'stage', '[skip ci]', 'push', 'clean', 'Git commit は未実行',
                 '初期化は Zenn deployment 成功の確認ではありません'):
      self.assertIn(text, stdout)
    with self.assertRaises(GuardError):
      identify_worktrees(self.root)
    self.commit()
    self.assertEqual(identify_worktrees(self.root).deploy, self.deploy)
    self.assertEqual(inspect_status(self.deploy), ())

  def test_cli_uses_default_user_adapter_once(self):
    body = json.dumps({'user': {'id': USER.id, 'username': USER.username}}).encode()
    with patch.object(cli, 'initialize', wraps=initialization.initialize) as initialize, \
         patch('checker.zenn_user_http.urlopen', return_value=FakeResponse(200, body)) as http:
      code, _, stderr = self.init()
    self.assertEqual(code, 0, stderr)
    initialize.assert_called_once_with(self.root, 'requested_user')
    http.assert_called_once()
    self.assertEqual(http.call_args.args[0].get_method(), 'GET')
    self.assertEqual(self.config.read_bytes(), CONFIG)
    self.lookup.assert_not_called()

  def test_default_adapter_invalid_response_creates_nothing(self):
    with patch.object(cli, 'initialize', wraps=initialization.initialize), \
         patch('checker.zenn_user_http.urlopen', return_value=FakeResponse(200, b'{"user":{"id":true}}')):
      self.assert_rejected_unchanged()
    self.assertFalse(self.ledger.exists())
    self.assertFalse(self.config.exists())

  def test_check_and_convert_reject_unborn(self):
    with self.assertRaises(GuardError):
      inspect_inputs(self.root)
    code, stdout, stderr = invoke(['check', '--article-root', str(self.root)])
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('ERROR', stderr)
    self.lookup.assert_not_called()

  def test_noop_same_id_different_username_preserves_bytes_and_metadata(self):
    self.initialized()
    self.lookup.return_value = ZennUser(USER.id, 'renamed_user')
    before = snapshot(self.deploy)
    with patch.object(initialization, 'atomic_replace_bytes', side_effect=AssertionError('No-op write')):
      code, stdout, stderr = self.init()
    self.assertEqual(code, 0)
    self.assertIn('ALREADY_INITIALIZED', stdout)
    self.assertEqual(stderr, '')
    self.assertEqual(snapshot(self.deploy), before)
    self.lookup.assert_called_once()

  def test_different_owner_is_rejected_without_overwrite(self):
    self.initialized()
    self.lookup.return_value = ZennUser(67890, USER.username)
    self.assert_rejected_unchanged()

  def test_completed_but_uncommitted_state_is_dirty(self):
    self.assertEqual(self.init()[0], 0)
    self.lookup.reset_mock()
    self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_resume_preserves_ledger_and_writes_only_config(self):
    self.ledger.write_bytes(EMPTY)
    ledger_stat = self.ledger.stat()
    real = initialization.atomic_replace_bytes
    with patch.object(initialization, 'atomic_replace_bytes', wraps=real) as replace:
      self.assertEqual(self.init()[0], 0)
    replace.assert_called_once_with(self.config, CONFIG)
    self.assertEqual(self.ledger.stat().st_ino, ledger_stat.st_ino)
    self.assertEqual(self.ledger.stat().st_mtime_ns, ledger_stat.st_mtime_ns)
    self.assertEqual(self.ledger.read_bytes(), EMPTY)

  def test_staged_new_canonical_ledger_is_resumable(self):
    self.ledger.write_bytes(EMPTY)
    fixture_git(self.deploy, 'add', '.zenn-authoring/ledger.json')
    self.assertEqual(self.init()[0], 0)
    self.assertEqual(self.ledger.read_bytes(), EMPTY)

  def test_committed_canonical_ledger_is_resumable(self):
    self.ledger.write_bytes(EMPTY)
    self.commit()
    self.assertEqual(self.init()[0], 0)

  def test_missing_directory_is_not_created(self):
    self.meta.rmdir()
    self.assert_rejected_unchanged()
    self.assertFalse(self.meta.exists())
    self.lookup.assert_not_called()

  def test_initial_lookup_failures_do_not_create_management_files(self):
    for error in (ValueError('not found'), OSError('timeout'), KeyboardInterrupt()):
      with self.subTest(error=error):
        self.lookup.side_effect = error
        self.assertEqual(self.init()[0], 130 if isinstance(error, KeyboardInterrupt) else 2)
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.config.exists())

  def test_invalid_lookup_result_is_rejected(self):
    self.lookup.return_value = {'id': 1, 'username': 'example'}
    self.assert_rejected_unchanged()
    self.assertFalse(self.ledger.exists())

  def test_config_without_ledger_is_not_regenerated(self):
    self.config.write_bytes(CONFIG)
    self.commit()
    self.assert_rejected_unchanged()
    self.assertFalse(self.ledger.exists())
    self.lookup.assert_not_called()

  def test_noncanonical_empty_ledger_is_rejected(self):
    values = [b'invalid', b'{}', b'{"articles":[],"pending_next_state":{}}',
              b'{"articles":{},"pending_next_state":{}}', EMPTY + b'\n',
              ledger_bytes({'articles': {}, 'pending_next_state': {}, 'extra': {}}),
              ledger_bytes({'articles': {'id': {}}, 'pending_next_state': {}})]
    for value in values:
      with self.subTest(value=value):
        self.ledger.write_bytes(value)
        self.assert_rejected_unchanged()
        self.assertFalse(self.config.exists())
    self.lookup.assert_not_called()

  def test_config_absent_clean_nonempty_ledger_is_rejected(self):
    confirmed = {'state': 'erased', 'slug': SLUGS[0], 'successful_release': 1,
                 'successful_zenn_observation': BASELINE}
    self.ledger.write_bytes(ledger_bytes({'articles': {IDS[0]: confirmed}, 'pending_next_state': {}}))
    self.commit()
    self.assert_rejected_unchanged()
    self.assertFalse(self.config.exists())
    self.lookup.assert_not_called()

  def test_config_absent_clean_extra_empty_management_state_is_rejected(self):
    self.ledger.write_bytes(ledger_bytes({'articles': {}, 'pending_next_state': {}, 'extra': 'unexpected'}))
    self.commit()
    self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_invalid_existing_ledger_is_not_overwritten(self):
    self.initialized()
    for value in (b'invalid', b'{}', ledger_bytes({'articles': {IDS[0]: {}}, 'pending_next_state': {}})):
      with self.subTest(value=value):
        self.ledger.write_bytes(value)
        self.commit()
        self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_invalid_existing_config_is_not_overwritten(self):
    self.initialized()
    for value in (b'invalid', b'{}', ledger_bytes({'zenn_user': {'id': True, 'username': 'example'}}),
                  ledger_bytes({'zenn_user': {'id': 1}})):
      with self.subTest(value=value):
        self.config.write_bytes(value)
        self.commit()
        self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_nonempty_valid_ledger_noop_does_not_require_readiness(self):
    self.initialized()
    confirmed = {'state': 'active', 'slug': SLUGS[0], 'successful_release': 1,
                 'successful_zenn_observation': BASELINE}
    self.ledger.write_bytes(ledger_bytes({'articles': {IDS[0]: confirmed}, 'pending_next_state': {}}))
    self.commit()
    before = snapshot(self.deploy)
    self.assertEqual(self.init()[0], 0)
    self.assertEqual(snapshot(self.deploy), before)

  def test_existing_pending_ledger_noop_preserves_pending(self):
    self.initialized()
    pending = {'operation': 'CREATE', 'state': 'active', 'slug': SLUGS[0], 'release': 1}
    self.ledger.write_bytes(ledger_bytes({'articles': {}, 'pending_next_state': {IDS[0]: pending}}))
    self.commit()
    before = snapshot(self.deploy)
    self.assertEqual(self.init()[0], 0)
    self.assertEqual(snapshot(self.deploy), before)

  def test_dirty_contamination_and_resume_contamination(self):
    for resume in (False, True):
      with self.subTest(resume=resume):
        if resume:
          self.ledger.write_bytes(EMPTY)
        (self.deploy / 'unrelated').write_text('dirty')
        self.assert_rejected_unchanged()
        (self.deploy / 'unrelated').unlink()
    self.lookup.assert_not_called()

  def test_staged_and_unstaged_contamination(self):
    tracked = self.deploy / 'unrelated'
    tracked.write_text('original')
    self.commit()
    tracked.write_text('unstaged')
    self.assert_rejected_unchanged()
    fixture_git(self.deploy, 'add', 'unrelated')
    self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_resume_does_not_accept_modified_preexisting_ledger(self):
    self.ledger.write_text('previous state')
    self.commit()
    self.ledger.write_bytes(EMPTY)
    self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_staged_ledger_contamination_is_not_resumable(self):
    self.ledger.write_text('different staged content')
    fixture_git(self.deploy, 'add', '.zenn-authoring/ledger.json')
    self.ledger.write_bytes(EMPTY)
    self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_unreliable_index_flags_are_rejected(self):
    self.initialized()
    for flag in ('assume-unchanged', 'skip-worktree'):
      with self.subTest(flag=flag):
        fixture_git(self.deploy, 'update-index', '--' + flag, '.zenn-authoring/config.json')
        self.assert_rejected_unchanged()
        fixture_git(self.deploy, 'update-index', '--no-' + flag, '.zenn-authoring/config.json')
    self.lookup.assert_not_called()

  def test_interrupt_after_ledger_then_resume(self):
    real = initialization.atomic_replace_bytes
    def replace(path, content):
      if path == self.config:
        raise KeyboardInterrupt()
      return real(path, content)
    with patch.object(initialization, 'atomic_replace_bytes', side_effect=replace):
      code, stdout, stderr = self.init()
    self.assertEqual(code, 130)
    self.assertEqual(stdout, '')
    self.assertIn('部分変更', stderr)
    self.assertEqual(self.ledger.read_bytes(), EMPTY)
    self.assertFalse(self.config.exists())
    self.lookup.reset_mock()
    self.assertEqual(self.init()[0], 0)
    self.lookup.assert_called_once()

  def test_config_atomic_replace_failure_cleans_temp_then_resume(self):
    real = os.replace
    def replace(source, target):
      if Path(target) == self.config:
        raise OSError('config replacement failed')
      return real(source, target)
    with patch('checker.atomic_write.os.replace', side_effect=replace):
      self.assertEqual(self.init()[0], 2)
    self.assertEqual(tuple(self.meta.iterdir()), (self.ledger,))
    self.assertEqual(self.ledger.read_bytes(), EMPTY)
    self.assertEqual(self.init()[0], 0)

  def test_ledger_atomic_replace_failure_creates_nothing(self):
    with patch('checker.atomic_write.os.replace', side_effect=OSError('ledger replacement failed')):
      self.assertEqual(self.init()[0], 2)
    self.assertEqual(tuple(self.meta.iterdir()), ())

  def test_resume_lookup_failure_retains_ledger(self):
    self.ledger.write_bytes(EMPTY)
    self.lookup.side_effect = ValueError('404')
    self.assert_rejected_unchanged()
    self.assertFalse(self.config.exists())

  def test_symlink_special_and_directory_management_files(self):
    for path in (self.ledger, self.config):
      for kind in ('symlink', 'fifo', 'directory'):
        with self.subTest(path=path.name, kind=kind):
          if kind == 'symlink':
            path.symlink_to(self.root / '.gitignore')
          elif kind == 'fifo':
            os.mkfifo(path)
          else:
            path.mkdir()
          self.assert_rejected_unchanged()
          path.rmdir() if kind == 'directory' else path.unlink()
    self.lookup.assert_not_called()

  def test_management_directory_symlink_rejected(self):
    self.meta.rmdir()
    self.meta.symlink_to(self.root, target_is_directory=True)
    self.assertEqual(self.init()[0], 2)
    self.lookup.assert_not_called()

  def test_unsafe_path_helper_rejects_traversal(self):
    with self.assertRaises(ValueError):
      safe_path(self.deploy, '..', 'elsewhere')

  def test_wrong_branches_rejected_in_init_mode(self):
    fixture_git(self.root, 'branch', '-m', 'wrong')
    self.assert_rejected_unchanged()
    fixture_git(self.root, 'branch', '-m', 'main')
    fixture_git(self.deploy, 'symbolic-ref', 'HEAD', 'refs/heads/wrong-deploy')
    self.assert_rejected_unchanged()
    self.lookup.assert_not_called()

  def test_development_repository_rejected(self):
    root = Path(__file__).resolve().parents[2]
    with self.assertRaisesRegex(GuardError, 'Development repository'):
      identify_worktrees(root, allow_unborn_deploy=True)

  def test_unrelated_deploy_repository_rejected(self):
    fixture_git(self.root, 'worktree', 'remove', '_deploy')
    self.deploy.mkdir()
    fixture_git(self.deploy, 'init', '-b', 'deploy')
    with self.assertRaisesRegex(GuardError, 'metadata'):
      identify_worktrees(self.root, allow_unborn_deploy=True)

  def test_broken_and_absolute_worktree_links_rejected(self):
    link = self.deploy / '.git'
    original = link.read_text()
    metadata = (self.deploy / original[8:].strip()).resolve()
    for content in ('gitdir: ../missing\n', f'gitdir: {metadata}\n'):
      with self.subTest(content=content):
        link.write_text(content)
        with self.assertRaises(GuardError):
          identify_worktrees(self.root, allow_unborn_deploy=True)
    link.write_text(original)
    (metadata / 'gitdir').write_text(str(link) + '\n')
    with self.assertRaises(GuardError):
      identify_worktrees(self.root, allow_unborn_deploy=True)

  def test_corrupt_and_dangling_deploy_ref_are_not_unborn(self):
    ref = self.root / '.git' / 'refs' / 'heads' / 'deploy'
    for content in ('broken\n', '0' * 40 + '\n', '1' * 40 + '\n'):
      with self.subTest(content=content):
        ref.write_text(content)
        with self.assertRaises(GuardError):
          identify_worktrees(self.root, allow_unborn_deploy=True)

  def test_broken_authoring_head_is_not_relaxed(self):
    (self.root / '.git' / 'refs' / 'heads' / 'main').write_text('1' * 40 + '\n')
    with self.assertRaises(GuardError):
      identify_worktrees(self.root, allow_unborn_deploy=True)

  def test_ref_probe_errors_are_not_interpreted_as_absence(self):
    real = git_inspection.git
    for error in (GuardError('ref failure', returncode=1), GuardError('Git failed', returncode=128),
                  GuardError('execution failed')):
      def run(root, *args):
        if args == ('show-ref', '--exists', 'refs/heads/deploy'):
          raise error
        return real(root, *args)
      with self.subTest(error=error), patch.object(git_inspection, 'git', side_effect=run):
        with self.assertRaises(GuardError):
          identify_worktrees(self.root, allow_unborn_deploy=True)

  def test_valid_existing_ref_still_requires_commit(self):
    oid = fixture_git(self.root, 'hash-object', '-w', '.gitignore').decode().strip()
    (self.root / '.git' / 'refs' / 'heads' / 'deploy').write_text(oid + '\n')
    with self.assertRaises(GuardError):
      identify_worktrees(self.root, allow_unborn_deploy=True)
