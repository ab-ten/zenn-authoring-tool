"""手動 UPDATE 確定の信頼境界、隔離 Git repository と中断復旧を検証します。"""

import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from checker import confirm_pending_update as workflow
from checker import cli, terminal_update, check_pending
from checker.create_observation import ArticleResponse, CreateObservation, CreateResult
from checker.commit_readiness import CommitReadiness
import test_checker_cli as fixtures
from test_unified_check import IDS, SLUGS, BASELINE, CURRENT, response
from test_entry_guards import fixture_git, snapshot


class ConfirmPendingUpdateTests(unittest.TestCase):
  def setUp(self):
    self.fixture = fixtures.CheckerCliIntegrationTests()
    self.fixture.setUp()
    self.addCleanup(self.fixture.doCleanups)
    self.root = self.fixture.root
    self.deploy = self.fixture.deploy
    self.ledger_path = self.fixture.ledger_path
    self.snapshot = self.deploy / '.zenn-authoring' / 'released' / f'{IDS[1]}.md'
    self.artifact = self.deploy / 'articles' / f'{SLUGS[1]}.md'
    self.fetch = Mock(return_value=response(SLUGS[1], observation=BASELINE))

  def confirm(self, article_id=IDS[1], **kwargs):
    options = dict(confirmed_deploy_success=True, confirmed_content_match=True,
                   fetch_article=self.fetch)
    options.update(kwargs)
    return workflow.confirm_pending_update(self.root, article_id, **options)

  def invoke(self, *flags, article_id=IDS[1]):
    with patch.object(cli, 'confirm_pending_update', side_effect=lambda *args, **kwargs:
         workflow.confirm_pending_update(*args, **kwargs, fetch_article=self.fetch)):
      return fixtures.invoke(['confirm-pending-update', article_id, '--article-root',
                              str(self.root), *flags])

  def state(self):
    return self.ledger_path.read_bytes(), self.snapshot.read_bytes()

  def assert_refused(self, **kwargs):
    before = self.state()
    with patch.object(workflow, 'apply_update_success') as apply:
      with self.assertRaises((ValueError, OSError, TypeError)):
        self.confirm(**kwargs)
      apply.assert_not_called()
    self.assertEqual(self.state(), before)

  def test_required_keyword_only_confirmations_and_strict_true(self):
    with patch.object(workflow, 'identify_worktrees') as identify:
      for kwargs in ({}, {'confirmed_deploy_success': True}, {'confirmed_content_match': True}):
        with self.assertRaises(TypeError):
          workflow.confirm_pending_update(self.root, IDS[1], **kwargs)
      with self.assertRaises(TypeError):
        workflow.confirm_pending_update(self.root, IDS[1], True, True)
      for value in (False, None, 1, 'true', [], {}):
        for key in ('confirmed_deploy_success', 'confirmed_content_match'):
          with self.subTest(key=key, value=value):
            self.assert_refused(**{key: value})
      identify.assert_not_called()
    self.fetch.assert_not_called()

  def test_success_reuses_classifier_and_terminal_apply_only_for_target(self):
    before = self.fixture.ledger()
    with patch.object(workflow, 'classify_update_response', wraps=workflow.classify_update_response) as classify, \
         patch.object(workflow, 'apply_update_success', wraps=terminal_update.apply_update_success) as apply:
      self.assertEqual(self.confirm(), 2)
      self.assertEqual(classify.call_count, 1)
      apply.assert_called_once_with(self.deploy, IDS[1], BASELINE)
    self.fetch.assert_called_once_with(SLUGS[1])
    after = self.fixture.ledger()
    expected = copy.deepcopy(before)
    expected['articles'][IDS[1]]['successful_release'] = 2
    expected['articles'][IDS[1]]['successful_zenn_observation'] = BASELINE
    del expected['pending_next_state'][IDS[1]]
    self.assertEqual(after, expected)
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    self.assertNotIn('confirmed_content_match', self.ledger_path.read_text())
    self.fixture.runner.assert_not_called()

  def test_pending_classifier_has_no_observation_and_baseline_is_equivalent(self):
    pending = self.fixture.ledger()['pending_next_state'][IDS[1]]
    result = workflow.classify_update_response(self.fetch.return_value, pending, 2)
    self.assertEqual(result.result, CreateResult.PENDING)
    self.assertIsNone(result.observation)
    article = self.fetch.return_value.payload['article']
    self.assertEqual({key: article[key] for key in BASELINE}, pending['baseline_observation'])

  def test_baseline_may_differ_from_previous_successful_observation(self):
    ledger = self.fixture.ledger()
    ledger['articles'][IDS[1]]['successful_zenn_observation'] = CURRENT
    self.fixture.save(ledger)
    self.confirm()
    self.assertEqual(self.fixture.ledger()['articles'][IDS[1]]['successful_zenn_observation'], BASELINE)

  def test_remote_results_refused_with_one_get(self):
    values = [response(SLUGS[1], observation=CURRENT), ArticleResponse(404),
              response(SLUGS[1], owner=99), *[ArticleResponse(s) for s in (403, 429, 500, 302)],
              ArticleResponse(200), ArticleResponse(200, {'article': []}),
              response('unexpected-slug'), response(SLUGS[1], observation={}), None]
    for value in values:
      with self.subTest(value=value):
        self.fetch.reset_mock()
        self.fetch.return_value = value
        self.assert_refused()
        self.fetch.assert_called_once_with(SLUGS[1])
    self.fetch.side_effect = OSError('transport failed')
    self.assert_refused()

  def test_unexpected_collision_refused(self):
    with patch.object(workflow, 'classify_update_response',
                      return_value=CreateObservation(CreateResult.COLLISION)):
      self.assert_refused()

  def test_invalid_article_and_pending(self):
    for article_id in ('invalid', '../unsafe', IDS[4], IDS[0], IDS[2], IDS[3]):
      self.assert_refused(article_id=article_id)
    original = self.fixture.ledger()
    cases = [('pending_next_state', 'operation', 'CREATE'),
             ('pending_next_state', 'state', 'erased'),
             ('pending_next_state', 'release', 1), ('pending_next_state', 'release', 3),
             ('pending_next_state', 'release', True), ('pending_next_state', 'slug', '../unsafe'),
             ('pending_next_state', 'slug', 'different-valid-slug'),
             ('pending_next_state', 'baseline_observation', {}),
             ('articles', 'state', 'erased'), ('articles', 'successful_release', 0),
             ('articles', 'successful_zenn_observation', {})]
    for section, key, value in cases:
      with self.subTest(section=section, key=key, value=value):
        ledger = copy.deepcopy(original)
        ledger[section][IDS[1]][key] = value
        self.fixture.save(ledger)
        self.assert_refused()
    self.fetch.assert_not_called()

  def test_slug_overlap_refused(self):
    ledger = self.fixture.ledger()
    ledger['pending_next_state'][IDS[0]]['slug'] = SLUGS[1]
    self.fixture.save(ledger)
    self.assert_refused()
    self.fetch.assert_not_called()

  def test_missing_and_malformed_ledger(self):
    for content in (None, 'invalid', '{}', '{"articles":[],"pending_next_state":{}}'):
      if content is None:
        self.ledger_path.unlink()
      else:
        self.ledger_path.write_text(content)
      before_snapshot = self.snapshot.read_bytes()
      with patch.object(workflow, 'apply_update_success') as apply:
        with self.assertRaises((ValueError, OSError)):
          self.confirm()
        apply.assert_not_called()
      self.assertEqual(self.snapshot.read_bytes(), before_snapshot)
    self.fetch.assert_not_called()

  def test_config_and_identity_errors(self):
    config = self.fixture.config_path
    for value in (None, '{}', 'invalid', '{"zenn_user":{"id":true}}'):
      if value is None:
        config.unlink()
      else:
        config.write_text(value)
      self.assert_refused()
    config.write_text('{"zenn_user":{"id":2}}')
    for root in (self.root / 'missing', self.deploy, Path(__file__).resolve().parents[2]):
      with self.assertRaises(ValueError):
        workflow.confirm_pending_update(root, IDS[1], confirmed_deploy_success=True,
                                        confirmed_content_match=True, fetch_article=self.fetch)
    fixture_git(self.deploy, 'branch', '-m', 'wrong')
    self.assert_refused()
    fixture_git(self.deploy, 'branch', '-m', 'deploy')
    (self.deploy / '.git').write_text('gitdir: ../missing\n')
    self.assert_refused()
    self.fetch.assert_not_called()

  def test_unsafe_files_and_missing_artifact(self):
    for path in (self.artifact, self.snapshot, self.ledger_path, self.fixture.config_path):
      content = path.read_bytes()
      for kind in ('symlink', 'dangling', 'fifo', 'directory'):
        with self.subTest(path=path.name, kind=kind):
          external = self.deploy / 'external'
          external.write_bytes(content)
          path.unlink()
          if kind in ('symlink', 'dangling'):
            path.symlink_to(external if kind == 'symlink' else self.deploy / 'absent')
          elif kind == 'fifo':
            os.mkfifo(path)
          else:
            path.mkdir()
          with patch.object(workflow, 'apply_update_success') as apply:
            with self.assertRaises((ValueError, OSError)):
              self.confirm()
            apply.assert_not_called()
          self.assertEqual(external.read_bytes(), content)
          path.rmdir() if kind == 'directory' else path.unlink()
          path.write_bytes(content)
          external.unlink()
    self.artifact.unlink()
    self.assert_refused()
    self.fetch.assert_not_called()

  def test_unsafe_parents(self):
    for parent in (self.artifact.parent, self.snapshot.parent):
      backup = self.deploy / 'backup'
      parent.rename(backup)
      parent.symlink_to(backup, target_is_directory=True)
      self.assert_refused()
      parent.unlink()
      backup.rename(parent)
    self.fetch.assert_not_called()

  def test_ledger_change_during_http(self):
    def fetch(slug):
      self.ledger_path.write_bytes(self.ledger_path.read_bytes() + b'\n')
      return response(slug, observation=BASELINE)
    self.fetch.side_effect = fetch
    before_snapshot = self.snapshot.read_bytes()
    with patch.object(workflow, 'apply_update_success') as apply:
      with self.assertRaisesRegex(ValueError, 'Ledger changed'):
        self.confirm()
      apply.assert_not_called()
    self.assertEqual(self.snapshot.read_bytes(), before_snapshot)
    self.assertIn(IDS[1], self.fixture.ledger()['pending_next_state'])

  def test_atomic_order_recovery_and_no_double_apply(self):
    before = self.ledger_path.read_bytes()
    real = terminal_update.atomic_replace_bytes
    calls = []
    def fail(path, content):
      calls.append(path)
      if path == self.ledger_path:
        self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
        raise OSError('ledger replacement failed')
      return real(path, content)
    with patch.object(terminal_update, 'atomic_replace_bytes', side_effect=fail):
      with self.assertRaises(OSError):
        self.confirm()
    self.assertEqual(calls, [self.snapshot, self.ledger_path])
    self.assertEqual(self.ledger_path.read_bytes(), before)
    with patch('checker.atomic_write.os.replace', wraps=os.replace) as replace:
      self.confirm()
      self.assertEqual(replace.call_count, 1)
      self.assertEqual(Path(replace.call_args.args[1]), self.ledger_path)
    self.fetch.reset_mock()
    self.assert_refused()
    self.fetch.assert_not_called()

  def test_ledger_replaced_by_symlink_during_observation(self):
    external = self.deploy / 'external-ledger.json'
    external.write_bytes(self.ledger_path.read_bytes())
    before = external.read_bytes(), self.snapshot.read_bytes()
    def fetch(slug):
      self.ledger_path.unlink()
      self.ledger_path.symlink_to(external)
      return response(slug, observation=BASELINE)
    self.fetch.side_effect = fetch
    with patch.object(workflow, 'apply_update_success') as apply:
      with self.assertRaisesRegex(ValueError, 'symlink'):
        self.confirm()
      apply.assert_not_called()
    self.assertEqual((external.read_bytes(), self.snapshot.read_bytes()), before)

  def test_cli_ledger_failure_preserves_git_and_can_retry(self):
    before = self.ledger_path.read_bytes()
    metadata = snapshot(self.root / '.git')
    real = terminal_update.atomic_replace_bytes
    def fail(path, content):
      if path == self.ledger_path:
        raise OSError('ledger failure')
      return real(path, content)
    with patch.object(terminal_update, 'atomic_replace_bytes', side_effect=fail):
      code, stdout, stderr = self.invoke('--confirmed-deploy-success', '--confirmed-content-match')
    self.assertEqual((code, stdout), (2, ''))
    self.assertIn('ledger failure', stderr)
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(snapshot(self.root / '.git'), metadata)
    self.assertEqual(self.invoke('--confirmed-deploy-success', '--confirmed-content-match')[0], 0)

  def test_snapshot_replacement_failure(self):
    with patch('checker.atomic_write.os.replace', side_effect=OSError('snapshot failure')):
      self.assert_refused_with_real_apply()

  def assert_refused_with_real_apply(self):
    before = self.state()
    with self.assertRaises(OSError):
      self.confirm()
    self.assertEqual(self.state(), before)

  def test_cli_success_and_followup_global_check(self):
    metadata = snapshot(self.root / '.git')
    code, stdout, stderr = self.invoke('--confirmed-deploy-success', '--confirmed-content-match')
    self.assertEqual((code, stderr), (0, ''))
    for text in (IDS[1], '手動 SUCCESS', 'Successful release', 'Pending を解消',
                 'Released snapshot', 'COMMIT_READY を保証しません', 'python -m checker check',
                 'ユーザーによる確認'):
      self.assertIn(text, stdout)
    self.assertEqual(snapshot(self.root / '.git'), metadata)
    self.fixture.runner.assert_not_called()
    outcome = check_pending.check_pending(self.deploy, lambda slug: ArticleResponse(404))
    self.assertEqual(outcome.readiness.result, CommitReadiness.NOT_READY)
    outcome = check_pending.check_pending(self.deploy, self.fixture.fixture.fetch)
    self.assertEqual(outcome.readiness.result, CommitReadiness.COMMIT_READY)

  def test_cli_remote_rejection_guidance(self):
    for remote in (response(SLUGS[1]), ArticleResponse(404), ArticleResponse(500)):
      self.fetch.return_value = remote
      before = self.state()
      code, stdout, stderr = self.invoke('--confirmed-deploy-success', '--confirmed-content-match')
      self.assertEqual((code, stdout), (2, ''))
      self.assertIn('python -m checker check', stderr)
      self.assertEqual(self.state(), before)

  def test_cli_interrupt_after_snapshot_and_retry(self):
    before = self.ledger_path.read_bytes()
    real = terminal_update.atomic_replace_bytes
    def interrupt(path, content):
      if path == self.ledger_path:
        raise KeyboardInterrupt()
      return real(path, content)
    metadata = snapshot(self.root / '.git')
    with patch.object(terminal_update, 'atomic_replace_bytes', side_effect=interrupt):
      code, stdout, stderr = self.invoke('--confirmed-deploy-success', '--confirmed-content-match')
    self.assertEqual((code, stdout), (130, ''))
    self.assertIn('部分変更', stderr)
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(self.snapshot.read_bytes(), self.artifact.read_bytes())
    self.assertEqual(snapshot(self.root / '.git'), metadata)
    self.assertEqual(self.invoke('--confirmed-deploy-success', '--confirmed-content-match')[0], 0)

  def test_cli_failures_and_interrupt_before_http(self):
    self.assertEqual(self.invoke('--confirmed-deploy-success', '--confirmed-content-match',
                                 article_id='invalid')[0], 2)
    self.fetch.assert_not_called()
    for exc, code in ((OSError('transport'), 2), (RuntimeError('private'), 2), (KeyboardInterrupt(), 130)):
      self.fetch.side_effect = exc
      before = self.state()
      result = self.invoke('--confirmed-deploy-success', '--confirmed-content-match')
      self.assertEqual(result[0], code)
      self.assertNotIn('private', result[2])
      self.assertEqual(self.state(), before)


class ConfirmationArgumentTests(unittest.TestCase):
  def test_help_missing_arguments_unknown_flags_and_no_cwd_fallback(self):
    command = 'confirm-pending-update'
    base = [command, IDS[1], '--article-root', '/explicit']
    both = ['--confirmed-deploy-success', '--confirmed-content-match']
    errors = [[command], [command, IDS[1], *both], base,
              [*base, both[0]], [*base, both[1]],
              [command, '--article-root', '/explicit', *both],
              [*base, *both, '--yes'], [*base, *both, '--unknown'],
              [command, IDS[1], '--article-root', '', *both]]
    with patch.object(cli, 'confirm_pending_update') as run, \
         patch.object(Path, 'cwd', side_effect=AssertionError('CWD fallback')):
      for args in errors:
        code, _, stderr = fixtures.invoke(args)
        self.assertEqual(code, 2, args)
        self.assertIn('ユーザー責任', stderr)
      code, stdout, _ = fixtures.invoke([command, '--help'])
      self.assertEqual(code, 0)
      for text in (*both, 'ARTICLE_ID', '--article-root PATH', 'ユーザー責任', 'Checker'):
        self.assertIn(text, stdout)
      self.assertIn(command, fixtures.invoke(['--help'])[1])
      run.assert_not_called()
