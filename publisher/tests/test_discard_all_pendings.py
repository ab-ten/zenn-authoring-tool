"""隔離 Git fixture と mock HTTP で全 pending 破棄を検証します。"""

import copy
from dataclasses import replace
import json
import stat
import unittest
from unittest.mock import Mock, patch

from checker import discard_all_pendings as workflow
from checker import cli, terminal_create, terminal_update, terminal_delete, terminal_recreate
from checker.create_observation import ArticleResponse
from checker.discard_preflight import PreflightResult, LocalValidation
from checker.errors import LocalApplyError
from checker.terminal_collision import apply_collision
import test_discard_preflight as preflight_fixture
from test_checker_cli import invoke
from test_entry_guards import fixture_git, snapshot
from test_unified_check import IDS, SLUGS, BASELINE, CURRENT, response
from converter import git_inspection


FLAGS = ['--confirmed-deploy-terminal', '--confirmed-remote-safe', '--confirmed-abandon-all']


class DiscardWorkflowTests(unittest.TestCase):
  setUp = preflight_fixture.DiscardPreflightTests.setUp
  artifact = preflight_fixture.DiscardPreflightTests.artifact
  released = preflight_fixture.DiscardPreflightTests.released
  ledger = preflight_fixture.DiscardPreflightTests.ledger
  save = preflight_fixture.DiscardPreflightTests.save

  def commit(self, message):
    fixture_git(self.deploy, 'add', '.')
    fixture_git(self.deploy, 'commit', '--allow-empty', '-m', message)

  def safe_response(self, slug):
    if slug in (SLUGS[0], SLUGS[3]):
      return ArticleResponse(404)
    return response(slug, observation=BASELINE)

  def discard(self, fetch=None, **kwargs):
    return workflow.discard_all_pendings(
      self.root, fetch_article=fetch or self.safe_response,
      **dict(dict.fromkeys(('confirmed_deploy_terminal', 'confirmed_remote_safe',
                          'confirmed_abandon_all'), True), **kwargs))

  def assert_refused(self, fetch=None):
    before = self.path.read_bytes()
    with patch.object(workflow, 'atomic_replace_bytes') as write:
      with self.assertRaises((LocalApplyError, OSError, RuntimeError)):
        self.discard(fetch)
    write.assert_not_called()
    self.assertEqual(self.path.read_bytes(), before)

  def test_single_replacement_preserves_every_other_file_and_mode(self):
    before = snapshot(self.root)
    ledger = self.ledger()
    self.path.chmod(0o640)
    real = workflow.atomic_replace_bytes
    with patch.object(workflow, 'atomic_replace_bytes', wraps=real) as write, \
         patch.object(git_inspection, 'git', wraps=git_inspection.git) as git:
      result = self.discard()
    write.assert_called_once()
    expected = copy.deepcopy(ledger)
    expected['pending_next_state'] = {}
    self.assertEqual(self.ledger(), expected)
    self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
    after = snapshot(self.root)
    key = self.path.relative_to(self.root).as_posix()
    self.assertNotEqual(before.pop(key), after.pop(key))
    self.assertEqual(before, after)
    self.assertEqual(result.recovery_plan.source_commit, self.parent)
    self.assertEqual(len(result.diagnostics), 4)
    allowed = {'rev-parse', 'symbolic-ref', 'rev-list', 'ls-tree', 'ls-files', 'status',
               'cat-file', 'for-each-ref'}
    self.assertTrue(all(call.args[1] in allowed for call in git.call_args_list))
    self.assert_refused()

  def test_all_confirmations_require_literal_true_before_inspection(self):
    for name in ('confirmed_deploy_terminal', 'confirmed_remote_safe', 'confirmed_abandon_all'):
      for value in (False, None, 1, 'yes'):
        with self.subTest(name=name, value=value), \
             patch.object(workflow, 'preflight_discard') as preflight:
          with self.assertRaises(LocalApplyError):
            self.discard(**{name: value})
          preflight.assert_not_called()

  def test_remote_rejection_matrix_all_pending_observed_no_write(self):
    for index in range(4):
      cases = [ArticleResponse(code) for code in (401, 403, 429, 500, 302, 204)]
      cases += [ArticleResponse(200, None), ArticleResponse(200, {'article': {}}),
                response(SLUGS[index], owner=True), response(SLUGS[index], owner='2'),
                response(SLUGS[index], owner=0), response(SLUGS[index], owner=None),
                response('wrong-slug'), OSError('transport'), ValueError('invalid JSON'),
                RuntimeError('adapter failure'), object()]
      if index in (0, 3):
        cases += [response(SLUGS[index])]
      elif index == 1:
        cases += [response(SLUGS[index]), ArticleResponse(404), response(SLUGS[index], owner=99),
                  response(SLUGS[index], observation={})]
      else:
        cases += [ArticleResponse(404), response(SLUGS[index], owner=99)]
      for case in cases:
        with self.subTest(index=index, case=case):
          def fetch(slug):
            if slug != SLUGS[index]:
              return self.safe_response(slug)
            if isinstance(case, Exception):
              raise case
            return case
          adapter = Mock(side_effect=fetch)
          self.assert_refused(adapter)
          self.assertEqual(adapter.call_count, 4)

  def test_create_recreate_other_owner_allowed_delete_needs_no_timestamps(self):
    def fetch(slug):
      if slug in (SLUGS[0], SLUGS[3]):
        return response(slug, owner=99, observation={})
      if slug == SLUGS[2]:
        return response(slug, observation={})
      return self.safe_response(slug)
    self.discard(fetch)
    self.assertEqual(self.ledger()['pending_next_state'], {})

  def test_override_baseline_can_differ_from_previous_success(self):
    fixture_git(self.deploy, 'reset', '--hard', self.parent)
    ledger = copy.deepcopy(self.pending_ledger)
    ledger['pending_next_state'][IDS[1]]['baseline_observation'] = CURRENT
    self.save(ledger)
    for index in (0, 1, 3):
      self.artifact(index).write_bytes(b'new candidate')
    self.artifact(2).unlink()
    self.commit('override baseline pending')
    self.discard(lambda slug: response(slug, observation=CURRENT) if slug == SLUGS[1]
                 else self.safe_response(slug))
    self.assertEqual(self.ledger()['articles'], ledger['articles'])

  def test_partial_success_restore_only_remaining_pending(self):
    appliers = (terminal_create.apply_create_success, terminal_update.apply_update_success,
                terminal_delete.apply_delete_success, terminal_recreate.apply_recreate_success)
    for index, apply in enumerate(appliers):
      with self.subTest(index=index):
        self.setUp()
        if index == 2:
          apply(self.deploy, IDS[index])
        else:
          apply(self.deploy, IDS[index], CURRENT)
        confirmed = copy.deepcopy(self.ledger()['articles'])
        snapshots = snapshot(self.deploy / '.zenn-authoring/released')
        outcome = self.discard()
        self.assertNotIn(f'articles/{SLUGS[index]}.md', outcome.recovery_plan.article_paths)
        self.assertEqual(self.ledger()['articles'], confirmed)
        self.assertEqual(snapshot(self.deploy / '.zenn-authoring/released'), snapshots)

  def test_partial_collision_restore_only_remaining_pending(self):
    for index in (0, 3):
      apply_collision(self.deploy, IDS[index])
    result = self.discard()
    self.assertEqual(result.recovery_plan.article_paths,
                     tuple(f'articles/{slug}.md' for slug in SLUGS[1:3]))

  def test_local_failure_matrix_no_http_or_replacement(self):
    for kind in ('empty', 'artifact', 'snapshot', 'staged', 'unsafe', 'head', 'owner'):
      with self.subTest(kind=kind):
        self.setUp()
        if kind == 'empty':
          ledger = self.ledger()
          ledger['pending_next_state'] = {}
          self.save(ledger)
        elif kind == 'artifact':
          self.artifact(0).write_bytes(b'edited')
        elif kind == 'snapshot':
          self.released(0).write_bytes(b'orphan')
        elif kind == 'staged':
          self.artifact(0).write_bytes(b'edited')
          fixture_git(self.deploy, 'add', 'articles')
        elif kind == 'unsafe':
          fixture_git(self.deploy, 'update-index', '--skip-worktree', 'articles/' + SLUGS[0] + '.md')
        elif kind == 'head':
          self.commit('later commit')
        else:
          config = self.deploy / '.zenn-authoring/config.json'
          config.write_text('{"zenn_user":{"id":true}}')
        fetch = Mock(side_effect=self.safe_response)
        self.assert_refused(fetch)
        fetch.assert_not_called()

  def test_changes_during_remote_refuse_clear(self):
    for kind in ('ledger', 'head', 'config', 'artifact', 'snapshot'):
      with self.subTest(kind=kind):
        self.setUp()
        def fetch(slug):
          if slug == SLUGS[0]:
            if kind == 'ledger':
              self.path.write_bytes(self.path.read_bytes() + b' ')
            elif kind == 'head':
              self.commit('later deploy')
            elif kind == 'config':
              (self.deploy / '.zenn-authoring/config.json').write_text('{"zenn_user":{"id":99}}')
            elif kind == 'artifact':
              self.artifact(0).write_bytes(b'concurrent')
            else:
              self.released(1).write_bytes(b'concurrent')
          return self.safe_response(slug)
        with patch.object(workflow, 'atomic_replace_bytes') as write:
          with self.assertRaises(LocalApplyError):
            self.discard(fetch)
        write.assert_not_called()
        self.assertTrue(self.ledger()['pending_next_state'])

  def test_second_preflight_failure_or_plan_pending_changes(self):
    first = workflow.preflight_discard(self.root)
    changes = [PreflightResult(LocalValidation.LOCAL_VALIDATION_REJECTED, diagnostics=('failed',)),
               replace(first, pending=first.pending[:-1])]
    for key, value in (('expected_head', 'changed'), ('source_commit', 'changed'),
                       ('ledger_sha256', 'changed'), ('article_paths', ())):
      changes.append(replace(first, recovery_plan=replace(first.recovery_plan, **{key: value})))
    for second in changes:
      with self.subTest(second=second), \
           patch.object(workflow, 'preflight_discard', side_effect=[first, second]):
        self.assert_refused()

  def test_final_ledger_head_config_checks(self):
    for kind in ('ledger', 'head', 'config'):
      with self.subTest(kind=kind):
        self.setUp()
        real = workflow.ledger_bytes
        def serialize(ledger):
          if kind == 'ledger':
            self.path.write_bytes(self.path.read_bytes() + b' ')
          elif kind == 'head':
            self.commit('changed at replacement boundary')
          else:
            (self.deploy / '.zenn-authoring/config.json').write_text('{"zenn_user":{"id":99}}')
          return real(ledger)
        with patch.object(workflow, 'ledger_bytes', side_effect=serialize), \
             patch.object(workflow, 'atomic_replace_bytes') as write:
          with self.assertRaises(LocalApplyError):
            self.discard()
        write.assert_not_called()
        self.assertTrue(self.ledger()['pending_next_state'])

  def test_pre_replacement_and_replacement_failures_preserve_pending(self):
    for target in ('ledger_bytes', 'atomic_replace_bytes'):
      with patch.object(workflow, target, side_effect=OSError('injected')):
        before = self.path.read_bytes()
        with self.assertRaises(OSError):
          self.discard()
        self.assertEqual(self.path.read_bytes(), before)
    with patch('checker.atomic_write.os.replace', side_effect=OSError('replacement failure')):
      before = self.path.read_bytes()
      with self.assertRaises(OSError):
        self.discard()
      self.assertEqual(self.path.read_bytes(), before)
      self.assertEqual(list(self.path.parent.glob('.ledger.json.*.tmp')), [])
    with patch.object(workflow, 'atomic_replace_bytes', return_value=False):
      with self.assertRaises(LocalApplyError):
        self.discard()

  def test_post_replacement_exception_or_mismatch_never_returns_success(self):
    for kind in ('exception', 'mismatch', 'head', 'config'):
      with self.subTest(kind=kind):
        self.setUp()
        real = workflow.atomic_replace_bytes
        def write(*args, **kwargs):
          real(*args, **kwargs)
          if kind == 'exception':
            raise RuntimeError('after replace')
          if kind == 'mismatch':
            self.path.write_bytes(b'{}')
          elif kind == 'head':
            self.commit('concurrent after replace')
          else:
            (self.deploy / '.zenn-authoring/config.json').write_text('{"zenn_user":{"id":99}}')
          return True
        with patch.object(workflow, 'atomic_replace_bytes', side_effect=write):
          with self.assertRaises((LocalApplyError, RuntimeError)):
            self.discard()
        self.assertFalse(self.ledger().get('pending_next_state'))

  def test_cli_success_and_recovery_guidance(self):
    argv = ['discard-all-pendings', '--article-root', str(self.root), *FLAGS]
    with patch.object(cli, 'discard_all_pendings', side_effect=lambda root, **kw:
                      workflow.discard_all_pendings(root, fetch_article=self.safe_response, **kw)):
      code, out, err = invoke(argv)
      self.assertEqual(code, 0, err)
      self.assertIn('PENDING_CLEARED:', out)
      self.assertIn('RESTORE_REQUIRED:', out)
      self.assertIn('git restore --source=' + self.parent + ' --worktree --', out)
      for slug in SLUGS[:4]:
        self.assertIn('articles/' + slug + '.md', out)
      self.assertNotIn('Global readiness:', out)
      self.assertNotIn('SUCCESS', out)
      self.assertIn('通常 check が COMMIT_READY の場合のみ', out)
      self.assertIn('再実行せず', out)
      code, out, err = invoke(argv)
      self.assertEqual(code, 2)
      self.assertNotIn('PENDING_CLEARED', out + err)
      self.assertIn('pending が空なら', err)

  def test_cli_failure_and_interrupt_after_replacement(self):
    for exception, expected in ((OSError('failure'), 2), (RuntimeError('failure'), 2),
                                (KeyboardInterrupt(), 130)):
      with self.subTest(exception=exception):
        self.setUp()
        real = workflow.atomic_replace_bytes
        def write(*args, **kwargs):
          real(*args, **kwargs)
          raise exception
        with patch.object(workflow, 'atomic_replace_bytes', side_effect=write), \
             patch.object(cli, 'discard_all_pendings', side_effect=lambda root, **kw:
                          workflow.discard_all_pendings(root, fetch_article=self.safe_response, **kw)):
          code, out, err = invoke(['discard-all-pendings', '--article-root', str(self.root), *FLAGS])
        self.assertEqual(code, expected)
        self.assertNotIn('PENDING_CLEARED', out + err)
        self.assertNotIn('SUCCESS', out + err)
        self.assertNotIn('COMMIT_READY', out + err)
        self.assertIn('既に削除されている可能性', err)
        self.assertEqual(self.ledger()['pending_next_state'], {})


class DiscardArgumentTests(unittest.TestCase):
  def test_arguments_and_help_read_only(self):
    base = ['discard-all-pendings', '--article-root', '/explicit']
    cases = [(['discard-all-pendings', '--help'], 0), (['discard-all-pendings', *FLAGS], 2),
             (base, 2), (base + FLAGS + ['--unknown'], 2),
             (['discard-all-pendings', '--article-ro', '/explicit', *FLAGS], 2),
             (base + FLAGS[1:] + ['--confirmed-deploy-term'], 2)]
    cases.extend((base + [flag for flag in FLAGS if flag != missing], 2) for missing in FLAGS)
    with patch.object(cli, 'discard_all_pendings') as run, \
         patch.object(cli, 'identify_worktrees') as guard:
      for args, expected in cases:
        with self.subTest(args=args):
          code, out, err = invoke(args)
          self.assertEqual(code, expected)
          self.assertIn('usage:', out if expected == 0 else err)
          if expected == 2:
            self.assertIn('Article API', err)
      run.assert_not_called()
      guard.assert_not_called()
