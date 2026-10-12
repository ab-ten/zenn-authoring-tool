"""公式 CLI と隔離した記事 repository による一回実行を検証します。"""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from checker import cli
from checker import check_pending as core
from checker import terminal_create
from checker.commit_readiness import CommitReadiness, ReadinessResult, check_commit_readiness
from checker.create_observation import ArticleResponse
from converter.git_inspection import Worktrees
from test_entry_guards import fixture_git, snapshot
import test_unified_check as fixture_data
from test_unified_check import IDS, SLUGS, response


def invoke(argv):
  stdout, stderr = io.StringIO(), io.StringIO()
  with redirect_stdout(stdout), redirect_stderr(stderr):
    try:
      code = cli.main(argv)
    except SystemExit as exc:
      code = exc.code
  return code, stdout.getvalue(), stderr.getvalue()


class CheckerArgumentTests(unittest.TestCase):
  def test_help_and_errors_never_execute(self):
    cases = [(['--help'], 0), (['check', '--help'], 0), ([], 2),
             (['check'], 2), (['unknown'], 2), (['check', '--article-root'], 2),
             (['check', '--article-root', ''], 2), (['check', 'path'], 2),
             (['check', '--article-ro', '/explicit'], 2),
             (['check', '--article-root', '/explicit', '--push'], 2)]
    with patch.object(cli, 'check_pending') as run, patch.object(cli, 'identify_worktrees') as guard, \
         patch.object(Path, 'cwd', side_effect=AssertionError('CWD fallback')):
      for args, expected in cases:
        with self.subTest(args=args):
          code, stdout, stderr = invoke(args)
          self.assertEqual(code, expected)
          self.assertIn('usage:', stdout if expected == 0 else stderr)
      run.assert_not_called()
      guard.assert_not_called()

  def test_structured_flags_override_ready_without_parsing_text(self):
    root = Path('/explicit')
    for check_fatal, readiness_fatal in ((False, False), (True, False), (False, True)):
      outcome = core.CheckResult({}, ReadinessResult(CommitReadiness.COMMIT_READY,
                                ('arbitrary diagnostic',), readiness_fatal),
                                fatal_error=check_fatal)
      with self.subTest(check_fatal=check_fatal, readiness_fatal=readiness_fatal), \
           patch.object(cli, 'identify_worktrees', return_value=Worktrees(root, root / '_deploy')), \
           patch.object(cli, 'load_configured_user_id'), \
           patch.object(cli, 'read_ledger', return_value={'pending_next_state': {}}), \
           patch.object(cli, 'check_pending', return_value=outcome) as run:
        code, stdout, stderr = invoke(['check', '--article-root', str(root)])
        self.assertEqual(code, 2 if check_fatal or readiness_fatal else 0)
        if code == 2:
          self.assertNotIn('COMMIT_READY', stdout + stderr)
        run.assert_called_once_with(root / '_deploy')


class CheckerCliIntegrationTests(unittest.TestCase):
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
    self.fixture = fixture_data.UnifiedCheckTests()
    self.fixture.setUp()
    self.addCleanup(self.fixture.doCleanups)
    shutil.copytree(self.fixture.root, self.deploy, dirs_exist_ok=True)
    self.ledger_path = self.deploy / '.zenn-authoring' / 'ledger.json'
    self.config_path = self.deploy / '.zenn-authoring' / 'config.json'
    fixture_git(self.deploy, 'add', '.')
    fixture_git(self.deploy, 'commit', '-m', 'fixture')
    self.fetch = Mock(side_effect=self.fixture.fetch)
    self.runner = self.enterContext(patch.object(cli, 'check_pending',
      side_effect=lambda root: core.check_pending(root, self.fetch)))
    for target in ('checker.zenn_http.urlopen', 'urllib.request.urlopen'):
      mock = self.enterContext(patch(target, side_effect=AssertionError('Actual HTTP')))
      self.addCleanup(mock.assert_not_called)

  def ledger(self):
    return json.loads(self.ledger_path.read_bytes())

  def save(self, ledger):
    self.ledger_path.write_text(json.dumps(ledger))

  def check(self):
    metadata = snapshot(self.root / '.git')
    with patch('converter.git_inspection.git', wraps=cli.identify_worktrees.__globals__['git']) as git:
      result = invoke(['check', '--article-root', str(self.root)])
    self.assertEqual(snapshot(self.root / '.git'), metadata)
    allowed = {'rev-parse', 'symbolic-ref'}
    self.assertTrue(all(call.args[1] in allowed for call in git.call_args_list))
    self.assertLessEqual(self.runner.call_count, 1)
    return result

  def empty(self):
    self.save({'articles': {}, 'pending_next_state': {}})
    for path in (self.deploy / 'articles').glob('*.md'):
      path.unlink()

  def test_all_success(self):
    code, stdout, stderr = self.check()
    self.assertEqual(code, 0)
    self.assertEqual(stderr, '')
    for article_id, operation in zip(IDS, ('CREATE', 'UPDATE', 'DELETE', 'RECREATE')):
      self.assertIn(f'article {article_id}: operation={operation} result=SUCCESS', stdout)
    for text in ('COMMIT_READY', 'Unresolved pending articles: 0', '[skip ci]', 'deploy の差分', '整合'):
      self.assertIn(text, stdout)
    self.assertEqual(self.fetch.call_count, 4)

  def test_empty_consistent(self):
    self.empty()
    code, stdout, _ = self.check()
    self.assertEqual(code, 0)
    self.assertIn('COMMIT_READY', stdout)
    self.fetch.assert_not_called()

  def test_one_pending(self):
    self.empty()
    ledger = self.ledger()
    ledger['pending_next_state'][IDS[0]] = self.fixture.ledger['pending_next_state'][IDS[0]]
    self.save(ledger)
    (self.deploy / 'articles' / f'{SLUGS[0]}.md').write_text('candidate')
    self.fetch.side_effect = lambda slug: ArticleResponse(404)
    code, stdout, stderr = self.check()
    self.assertEqual(code, 1)
    self.assertIn('result=PENDING', stdout)
    self.assertIn('Unresolved pending articles: 1', stdout)
    self.assertIn('commit/push しない', stdout)
    self.assertIn('one-shot check', stdout)
    self.assertEqual(stderr, '')
    self.fetch.assert_called_once_with(SLUGS[0])

  def test_mixed_error_unknown_partial_success(self):
    def fetch(slug):
      if slug == SLUGS[0]:
        raise OSError('network unavailable')
      return ArticleResponse(404) if slug == SLUGS[1] else self.fixture.fetch(slug)
    self.fetch.side_effect = fetch
    code, stdout, stderr = self.check()
    self.assertEqual(code, 1)
    for text in ('result=ERROR', 'network unavailable', 'result=UNKNOWN', 'result=SUCCESS', 'NOT_READY'):
      self.assertIn(text, stdout)
    self.assertEqual(stderr, '')
    self.assertEqual(set(self.ledger()['pending_next_state']), set(IDS[:2]))
    self.assertEqual(self.fetch.call_count, 4)

  def test_collision_cleanup_ready(self):
    self.fetch.side_effect = lambda slug: response(slug, 99) if slug in (SLUGS[0], SLUGS[3]) else self.fixture.fetch(slug)
    code, stdout, _ = self.check()
    self.assertEqual(code, 0)
    self.assertEqual(stdout.count('result=COLLISION'), 2)
    self.assertFalse((self.deploy / 'articles' / f'{SLUGS[0]}.md').exists())
    self.assertEqual(self.ledger()['pending_next_state'], {})

  def test_no_pending_inconsistent(self):
    self.empty()
    (self.deploy / 'articles' / 'unmanaged.md').write_text('unmanaged')
    code, stdout, _ = self.check()
    self.assertEqual(code, 1)
    self.assertIn('NOT_READY', stdout)
    self.assertIn('Ledger-unmanaged', stdout)
    self.assertIn('Unresolved pending articles: 0', stdout)

  def test_confirmed_artifact_differs_is_not_ready(self):
    core.check_pending(self.deploy, self.fixture.fetch)
    (self.deploy / 'articles' / f'{SLUGS[0]}.md').write_text('different content')
    code, stdout, stderr = self.check()
    self.assertEqual(code, 1)
    self.assertIn('Artifact differs from released snapshot', stdout)
    self.assertEqual(stderr, '')
    self.fetch.assert_not_called()

  def test_invalid_config_even_without_pending(self):
    self.empty()
    for content in (None, 'invalid', '{}', '{"zenn_user":{"id":true}}'):
      with self.subTest(content=content):
        if content is None:
          self.config_path.unlink()
        else:
          self.config_path.write_text(content)
        code, stdout, stderr = self.check()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, '')
        self.assertIn('ERROR', stderr)
    self.runner.assert_not_called()
    self.fetch.assert_not_called()

  def test_missing_malformed_ledger(self):
    for content in (None, 'invalid', '{}', '{"articles":[],"pending_next_state":{}}'):
      with self.subTest(content=content):
        if content is None:
          self.ledger_path.unlink()
        else:
          self.ledger_path.write_text(content)
        code, stdout, stderr = self.check()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, '')
        self.assertIn('ERROR', stderr)
    self.runner.assert_not_called()

  def test_invalid_pending_is_fatal_but_other_articles_progress(self):
    ledger = self.ledger()
    ledger['pending_next_state'][IDS[0]] = {'operation': 'INVALID'}
    self.save(ledger)
    code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertNotIn('SUCCESS', stderr)
    self.assertNotIn('COMMIT_READY', stderr)
    self.assertIn('部分変更', stderr)
    self.assertEqual(set(self.ledger()['pending_next_state']), {IDS[0]})

  def test_confirmed_schema_failure_is_fatal(self):
    self.empty()
    self.save({'articles': {IDS[0]: {}}, 'pending_next_state': {}})
    code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('Invalid confirmed slug', stderr)

  def test_readiness_io_failure_after_success_is_fatal(self):
    with patch('checker.commit_readiness.read_ledger', side_effect=OSError('read failed')):
      code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertNotIn('SUCCESS', stderr)
    self.assertIn('read failed', stderr)
    self.assertEqual(self.ledger()['pending_next_state'], {})

  def test_partial_apply_error_is_ordinary_not_ready(self):
    real = terminal_create.atomic_replace_bytes
    def replace(path, content):
      if path == self.ledger_path:
        raise OSError('disk full after snapshot')
      return real(path, content)
    with patch.object(terminal_create, 'atomic_replace_bytes', side_effect=replace):
      code, stdout, stderr = self.check()
    self.assertEqual(code, 1)
    self.assertIn('Local apply failed: disk full after snapshot', stdout)
    self.assertEqual(stderr, '')
    self.assertEqual(set(self.ledger()['pending_next_state']), {IDS[0]})
    self.assertTrue((self.deploy / '.zenn-authoring' / 'released' / f'{IDS[0]}.md').exists())

  def test_interrupt_after_snapshot(self):
    real = terminal_create.atomic_replace_bytes
    def replace(path, content):
      if path == self.ledger_path:
        raise KeyboardInterrupt()
      return real(path, content)
    with patch.object(terminal_create, 'atomic_replace_bytes', side_effect=replace):
      code, stdout, stderr = self.check()
    self.assertEqual(code, 130)
    self.assertEqual(stdout, '')
    self.assertIn('部分変更', stderr)
    self.assertIn('Git status', stderr)
    self.assertIn(IDS[0], self.ledger()['pending_next_state'])
    self.assertEqual(self.fetch.call_count, 4)

  def test_unexpected_failure_after_partial_apply(self):
    with patch.object(core, 'apply_update_success', side_effect=RuntimeError('private details')):
      code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('部分変更', stderr)
    self.assertNotIn('private details', stderr)
    self.assertNotIn(IDS[0], self.ledger()['pending_next_state'])

  def test_keyboard_interrupt_before_core(self):
    with patch.object(cli, 'load_configured_user_id', side_effect=KeyboardInterrupt()):
      code, stdout, stderr = self.check()
    self.assertEqual(code, 130)
    self.assertEqual(stdout, '')
    self.assertIn('INTERRUPTED', stderr)
    self.runner.assert_not_called()

  def test_wrong_root_branches_and_broken_association(self):
    for root in (self.root / 'missing', self.root / '.git', Path(__file__).resolve().parents[2]):
      code, stdout, _ = invoke(['check', '--article-root', str(root)])
      self.assertEqual(code, 2)
      self.assertEqual(stdout, '')
    for target in (self.root, self.deploy):
      original = 'main' if target == self.root else 'deploy'
      fixture_git(target, 'branch', '-m', 'wrong')
      self.assertEqual(self.check()[0], 2)
      fixture_git(target, 'branch', '-m', original)
    (self.deploy / '.git').write_text('gitdir: ../missing\n')
    self.assertEqual(self.check()[0], 2)
    self.runner.assert_not_called()

  def test_unrelated_deploy_repository(self):
    fixture_git(self.root, 'worktree', 'remove', '_deploy')
    self.deploy.mkdir()
    fixture_git(self.deploy, 'init', '-b', 'deploy')
    (self.deploy / 'file').write_text('unrelated')
    fixture_git(self.deploy, 'add', '.')
    fixture_git(self.deploy, 'commit', '-m', 'unrelated')
    code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('metadata', stderr)
    self.runner.assert_not_called()

  def test_reporting_failure_after_apply(self):
    real = cli.read_ledger
    calls = 0
    def read(root):
      nonlocal calls
      calls += 1
      if calls == 2:
        raise OSError('reporting read failed')
      return real(root)
    with patch.object(cli, 'read_ledger', side_effect=read):
      code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('reporting read failed', stderr)
    self.assertIn('部分変更', stderr)
    self.assertEqual(self.ledger()['pending_next_state'], {})

  def test_ledger_changed_during_observation_is_fatal(self):
    def fetch(slug):
      self.ledger_path.write_bytes(self.ledger_path.read_bytes() + b'\n')
      return self.fixture.fetch(slug)
    self.fetch.side_effect = fetch
    code, stdout, stderr = self.check()
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('Ledger changed', stderr)
    self.assertEqual(len(self.ledger()['pending_next_state']), 4)

  def test_module_entrypoint_help(self):
    result = subprocess.run([sys.executable, '-m', 'checker', 'check', '--help'],
                            capture_output=True, text=True, check=False)
    self.assertEqual(result.returncode, 0)
    self.assertIn('--article-root PATH', result.stdout)
    self.assertEqual(result.stderr, '')


class StructuredFailureTests(unittest.TestCase):
  def test_defaults_preserve_api(self):
    readiness = ReadinessResult(CommitReadiness.NOT_READY)
    self.assertFalse(readiness.fatal_error)
    self.assertFalse(core.CheckResult({}, readiness).fatal_error)

  def test_missing_ledger_is_fatal_in_both_models(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      self.assertTrue(check_commit_readiness(root).fatal_error)
      result = core.check_pending(root, Mock(side_effect=AssertionError('Unexpected HTTP')))
      self.assertTrue(result.fatal_error)
      self.assertTrue(result.readiness.fatal_error)

  def test_core_config_failure_is_structured(self):
    fixture = fixture_data.UnifiedCheckTests()
    fixture.setUp()
    self.addCleanup(fixture.doCleanups)
    (fixture.meta / 'config.json').unlink()
    fetch = Mock(side_effect=AssertionError('Unexpected HTTP'))
    result = core.check_pending(fixture.root, fetch)
    self.assertTrue(result.fatal_error)
    self.assertEqual(result.readiness.result, CommitReadiness.NOT_READY)
    fetch.assert_not_called()
