"""workflow をモックし、CLI の引数・診断・終了コードを検証します。"""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from checker.create_preflight import ArticlePreflight, PreflightResult, PreflightStatus
from checker.delete_preflight import DeleteArticlePreflight
from checker.errors import LocalApplyError
from checker.update_preflight import UpdateArticlePreflight
from converter import cli
from converter.git_inspection import GuardError
from converter.models import ArticlePlanEntry, ImagePlanEntry, PublishPlan
from converter.preflight_integration import PreflightOutcome


ID = '6a5c05e8-401a-4e2b-bc92-2c9aa61be305'
SLUG = 'cli-article-slug'
ROOT = Path('/explicit/article repository')
DEPLOY = ROOT / '_deploy'


def outcome(articles=(), images=(), status=PreflightStatus.OK, results=(), diagnostics=()):
  return PreflightOutcome(DEPLOY, PublishPlan(articles, images),
    PreflightResult(status, results, diagnostics))


def article(operation='CREATE', article_id=ID, slug=SLUG):
  return ArticlePlanEntry(article_id, operation, slug,
    'erased' if operation == 'DELETE' else 'active', 1,
    None if operation == 'DELETE' else 'candidate')


class ConverterCliTests(unittest.TestCase):
  def setUp(self):
    self.runner = self.enterContext(patch('converter.cli.workflow.run_convert',
      return_value=outcome()))
    for target in ('converter.workflow.preflight_publish', 'converter.workflow.apply_publish',
                   'converter.preflight_integration.plan_repository',
                   'converter.repository_planning.plan_publish',
                   'converter.git_inspection.git', 'subprocess.run', 'subprocess.Popen',
                   'checker.zenn_http.fetch_article', 'checker.zenn_http.urlopen',
                   'urllib.request.urlopen'):
      mock = self.enterContext(patch(target, side_effect=AssertionError('Unexpected CLI side effect')))
      self.addCleanup(mock.assert_not_called)

  def invoke(self, argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
      try:
        code = cli.main(argv)
      except SystemExit as exc:
        code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()

  def convert(self):
    return self.invoke(['convert', '--article-root', str(ROOT)])

  def assert_failure_output(self, code, stdout, stderr, expected=cli.ExitCode.ERROR):
    self.assertEqual(code, expected)
    self.assertEqual(stdout, '')
    self.assertNotIn('APPLIED', stderr)
    self.assertNotIn('SUCCESS', stderr)
    self.assertNotIn('ローカル変換が完了', stderr)

  def assert_recovery(self, stderr):
    for text in ('部分変更', 'commit/push しない', 'ホスト', 'Git status',
                 'staged/unstaged diff', 'untracked paths', 'pending_next_state',
                 '次の変換前に手動で復旧'):
      self.assertIn(text, stderr)
    self.assertNotIn('計画は適用していません', stderr)

  def test_help(self):
    code, stdout, stderr = self.invoke(['--help'])
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertIn('python -m converter', stdout)
    self.assertIn('convert', stdout)
    self.assertEqual(stderr, '')
    self.runner.assert_not_called()

  def test_convert_help(self):
    code, stdout, stderr = self.invoke(['convert', '--help'])
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertIn('--article-root PATH', stdout)
    self.assertIn('必須', stdout)
    self.assertEqual(stderr, '')
    self.runner.assert_not_called()

  def test_missing_subcommand(self):
    code, stdout, stderr = self.invoke([])
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('command', stderr)
    self.runner.assert_not_called()

  def test_missing_mandatory_root_does_not_select_cwd(self):
    with patch.object(Path, 'cwd', side_effect=AssertionError('Implicit repository')) as cwd:
      code, stdout, stderr = self.invoke(['convert'])
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('--article-root', stderr)
    cwd.assert_not_called()
    self.runner.assert_not_called()

  def test_invalid_arguments(self):
    cases = [
      ['unknown'], ['--article-root', str(ROOT)],
      ['convert', '--article-root'], ['convert', str(ROOT)],
      ['convert', '--article-root', str(ROOT), '--force'],
      ['convert', '--article-root', str(ROOT), '--skip-preflight'],
      ['convert', '--article-root', str(ROOT), '--push'],
      ['convert', '--article-root', str(ROOT), '--automatic-retry'],
      ['convert', '--article-ro', str(ROOT)],
      ['convert', '--article-root', ''],
    ]
    for args in cases:
      with self.subTest(args=args):
        code, stdout, stderr = self.invoke(args)
        self.assert_failure_output(code, stdout, stderr)
        self.assertIn('usage:', stderr)
    self.runner.assert_not_called()

  def test_explicit_root_forwarded_once_without_other_arguments(self):
    code, _, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertEqual(stderr, '')
    self.runner.assert_called_once_with(ROOT)

  def test_explicit_relative_root_is_forwarded(self):
    self.invoke(['convert', '--article-root', '../article repo'])
    self.runner.assert_called_once_with(Path('../article repo'))

  def test_repository_validity_is_delegated(self):
    self.runner.side_effect = GuardError('Wrong authoring branch')
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('Wrong authoring branch', stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_none_argv_uses_process_arguments(self):
    with patch.object(sys, 'argv', ['converter', 'convert', '--article-root', str(ROOT)]):
      code, _, _ = self.invoke(None)
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.runner.assert_called_once_with(ROOT)

  def test_success_counts_approved_operations_without_mutation(self):
    articles = tuple(article(op, f'id-{index}', f'slug-{index}')
      for index, op in enumerate(('CREATE', 'CREATE', 'UPDATE', 'DELETE', 'RECREATE')))
    images = (ImagePlanEntry('ADD', 'images/new.png', b'new'),
              ImagePlanEntry('UPDATE', 'images/asset.png', b'updated'),
              ImagePlanEntry('DELETE', 'images/old.png'))
    original = outcome(articles, images)
    self.runner.return_value = original
    before = repr(original)
    code, stdout, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertIn('articles: CREATE=2 UPDATE=1 DELETE=1 RECREATE=1', stdout)
    self.assertIn('images: ADD=1 UPDATE=1 DELETE=1', stdout)
    self.assertIn('pending article transitions: あり', stdout)
    for text in ('Zenn の公開結果は未確認', 'deploy の差分', 'ホストから commit/push',
                 'state-checker check', 'Zenn Dashboard'):
      self.assertIn(text, stdout)
    self.assertNotIn('SUCCESS', stdout)
    self.assertEqual(stderr, '')
    self.assertEqual(repr(original), before)
    self.assertIs(original.plan.articles, articles)
    self.assertIs(original.plan.images, images)
    self.runner.assert_called_once_with(ROOT)

  def test_article_only_summary_has_zero_image_counts(self):
    self.runner.return_value = outcome((article(),))
    code, stdout, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertIn('images: ADD=0 UPDATE=0 DELETE=0', stdout)
    self.assertIn('pending article transitions: あり', stdout)
    self.assertIn('state-checker check', stdout)
    self.assertEqual(stderr, '')

  def test_no_changes_without_commit_recommendation(self):
    code, stdout, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertIn('NO_CHANGES', stdout)
    self.assertIn('articles: CREATE=0 UPDATE=0 DELETE=0 RECREATE=0', stdout)
    self.assertIn('images: ADD=0 UPDATE=0 DELETE=0', stdout)
    self.assertIn('pending article transitions: なし', stdout)
    for text in ('commit', 'push', 'state-checker check', 'APPLIED', 'SUCCESS'):
      self.assertNotIn(text, stdout)
    self.assertEqual(stderr, '')

  def test_image_only_summary_has_no_article_pending(self):
    self.runner.return_value = outcome(images=(ImagePlanEntry('ADD', 'images/new.png', b'new'),))
    code, stdout, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertIn('images: ADD=1 UPDATE=0 DELETE=0', stdout)
    self.assertIn('pending article transitions: なし', stdout)
    self.assertIn('記事の pending state は生成していません', stdout)
    self.assertIn('記事と別に', stdout)
    self.assertIn('Zenn Dashboard', stdout)
    self.assertNotIn('state-checker check', stdout)
    self.assertNotIn('NO_CHANGES', stdout)
    self.assertEqual(stderr, '')

  def test_collision_displays_existing_diagnostics(self):
    item = ArticlePreflight(ID, SLUG, PreflightStatus.COLLISION, 'Slug owned by another user')
    self.runner.return_value = outcome((article('RECREATE'),), status=PreflightStatus.COLLISION,
      results=(item,), diagnostics=('global collision diagnostic',))
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr, cli.ExitCode.COLLISION)
    for text in ('Preflight: COLLISION', f'RECREATE {SLUG}', item.detail,
                 'global collision diagnostic', '計画は適用していません'):
      self.assertIn(text, stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_preflight_error_uses_plan_context_for_subset_results(self):
    first = article('CREATE', 'first-id', 'first-article-slug')
    item = ArticlePreflight(ID, SLUG, PreflightStatus.ERROR, 'Invalid remote schema')
    self.runner.return_value = outcome((first, article('UPDATE')), status=PreflightStatus.ERROR,
      results=(item,), diagnostics=('global config diagnostic',))
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    for text in ('Preflight: ERROR', f'UPDATE {SLUG}', item.detail,
                 'global config diagnostic', '計画は適用していません'):
      self.assertIn(text, stderr)
    self.assertNotIn(f'CREATE {SLUG}', stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_update_drift_observations_and_override_json_are_preserved(self):
    old = {'body_updated_at': 'old', 'source_repo_updated_at': 'old', 'published_at': 'date'}
    current = {**old, 'body_updated_at': 'new'}
    override_json = json.dumps(current)
    item = UpdateArticlePreflight(ID, SLUG, PreflightStatus.ERROR, 'Remote drift',
      ledger_observation=old, current_observation=current,
      override_observation=old, observation_override_json=override_json)
    self.runner.return_value = outcome((article('UPDATE'),), status=PreflightStatus.ERROR, results=(item,))
    original = repr(self.runner.return_value)
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    for text in ('ledger_observation: ' + json.dumps(old),
                 'current_observation: ' + json.dumps(current),
                 'override_observation: ' + json.dumps(old),
                 '#observation-override: ' + override_json):
      self.assertIn(text, stderr)
    self.assertEqual(repr(self.runner.return_value), original)

  def test_preflight_warnings_are_displayed(self):
    item = DeleteArticlePreflight(ID, SLUG, PreflightStatus.OK, warnings=('Already deleted',))
    self.runner.return_value = outcome((article('DELETE'),), status=PreflightStatus.ERROR, results=(item,))
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('warning: Already deleted', stderr)

  def assert_ok_warning(self, articles, results, warning):
    self.runner.return_value = outcome(articles)
    baseline_code, baseline_stdout, baseline_stderr = self.convert()
    self.assertEqual(baseline_code, cli.ExitCode.COMPLETED)
    self.assertEqual(baseline_stderr, '')
    self.runner.reset_mock()
    self.runner.return_value = outcome(articles, results=results)
    code, stdout, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertEqual(stdout, baseline_stdout)
    self.assertEqual(stderr, f'DELETE {SLUG}: warning: {warning}\n')
    self.runner.assert_called_once_with(ROOT)

  def test_ok_delete_already_absent_warning(self):
    warning = 'Publication already absent (HTTP 404)'
    item = DeleteArticlePreflight(ID, SLUG, PreflightStatus.OK,
      detail='DELETE may continue', warnings=(warning,))
    self.assert_ok_warning((article('DELETE'),), (item,), warning)

  def test_ok_delete_reused_slug_warning(self):
    warning = 'Old slug already reused by another user'
    observation = {'body_updated_at': 'remote', 'source_repo_updated_at': 'remote', 'published_at': 'date'}
    item = DeleteArticlePreflight(ID, SLUG, PreflightStatus.OK,
      detail='DELETE may continue', warnings=(warning,), current_observation=observation)
    self.assert_ok_warning((article('DELETE'),), (item,), warning)

  def test_ok_mixed_operations_show_only_delete_warning(self):
    warning = 'Publication already absent'
    create = article('CREATE', 'create-id', 'created-article-slug')
    update = article('UPDATE', 'update-id', 'updated-article-slug')
    observation = {'body_updated_at': 'remote', 'source_repo_updated_at': 'remote', 'published_at': 'date'}
    results = (
      ArticlePreflight(create.article_id, create.slug, PreflightStatus.OK, 'CREATE detail'),
      UpdateArticlePreflight(update.article_id, update.slug, PreflightStatus.OK,
        'UPDATE detail', baseline_observation=observation, current_observation=observation),
      DeleteArticlePreflight(ID, SLUG, PreflightStatus.OK, warnings=(warning,)),
    )
    self.assert_ok_warning((create, update, article('DELETE')), results, warning)

  def test_ok_without_warnings_has_no_extra_diagnostics(self):
    observation = {'body_updated_at': 'remote', 'source_repo_updated_at': 'remote', 'published_at': 'date'}
    item = DeleteArticlePreflight(ID, SLUG, PreflightStatus.OK,
      'DELETE detail', current_observation=observation)
    self.runner.return_value = outcome((article('DELETE'),), results=(item,))
    code, stdout, stderr = self.convert()
    self.assertEqual(code, cli.ExitCode.COMPLETED)
    self.assertEqual(stderr, '')
    self.assertIn('articles: CREATE=0 UPDATE=0 DELETE=1 RECREATE=0', stdout)
    for text in ('Preflight:', 'DELETE detail', 'current_observation', 'warning:'):
      self.assertNotIn(text, stdout)
    self.runner.assert_called_once_with(ROOT)

  def test_global_error_without_article_results(self):
    self.runner.return_value = outcome((article(),), status=PreflightStatus.ERROR,
      diagnostics=('Missing owner configuration',))
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('Missing owner configuration', stderr)

  def test_unmatched_article_diagnostic_retains_id_and_slug(self):
    item = ArticlePreflight('unmatched-id', 'unmatched-slug', PreflightStatus.ERROR, 'Unmatched diagnostic')
    self.runner.return_value = outcome(status=PreflightStatus.ERROR, results=(item,))
    _, _, stderr = self.convert()
    self.assertIn('article unmatched-id unmatched-slug', stderr)
    self.assertIn(item.detail, stderr)

  def test_expected_entry_guard_failure_is_concise(self):
    self.runner.side_effect = GuardError('Dirty deploy worktree: untracked article')
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertEqual(stderr, 'ERROR: GuardError: Dirty deploy worktree: untracked article\n')
    self.runner.assert_called_once_with(ROOT)

  def test_validation_error_has_original_message_and_no_retry(self):
    self.runner.side_effect = ValueError('Invalid source: release must be 1')
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('ERROR: ValueError: Invalid source: release must be 1', stderr)
    self.assertNotIn('Traceback', stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_apply_error_is_not_mistaken_for_validation_error(self):
    self.runner.side_effect = LocalApplyError('Image UPDATE failed; 2 preceding operations completed: disk full')
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('Image UPDATE failed; 2 preceding operations completed: disk full', stderr)
    self.assert_recovery(stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_unexpected_exception_retains_traceback_and_cause(self):
    try:
      try:
        raise OSError('original disk failure')
      except OSError as cause:
        raise RuntimeError('unexpected execution failure') from cause
    except RuntimeError as error:
      self.runner.side_effect = error
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('Traceback', stderr)
    self.assertIn('original disk failure', stderr)
    self.assertIn('RuntimeError: unexpected execution failure', stderr)
    self.assert_recovery(stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_generic_value_error_does_not_claim_that_writes_never_started(self):
    self.runner.side_effect = ValueError('unclassified failure')
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assert_recovery(stderr)

  def test_keyboard_interrupt_returns_130_and_manual_recovery(self):
    self.runner.side_effect = KeyboardInterrupt()
    code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr, cli.ExitCode.INTERRUPTED)
    self.assertIn('INTERRUPTED', stderr)
    self.assert_recovery(stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_reporting_failure_after_workflow_is_conservative(self):
    with patch.object(cli, '_summary', side_effect=RuntimeError('summary interrupted')):
      code, stdout, stderr = self.convert()
    self.assert_failure_output(code, stdout, stderr)
    self.assertIn('summary interrupted', stderr)
    self.assert_recovery(stderr)
    self.runner.assert_called_once_with(ROOT)

  def test_lint_help_and_required_arguments(self):
    code, stdout, stderr = self.invoke(['lint', '--help'])
    self.assertEqual(code, 0)
    for option in ('--article-root', '--all', '--article'):
      self.assertIn(option, stdout)
    self.assertEqual(stderr, '')
    for argv in (['lint', '--all'], ['lint', '--article-root', str(ROOT)],
                 ['lint', '--article-root', str(ROOT), '--all', '--article', 'articles/a.md'],
                 ['lint', '--article-root', '', '--all']):
      with self.subTest(argv=argv):
        code, _, stderr = self.invoke(argv)
        self.assertEqual(code, 2)
        self.assertNotIn('部分変更', stderr)
    self.runner.assert_not_called()

  def test_lint_all_summary_and_exit_zero(self):
    from converter.lint import LintResult
    with patch.object(cli, 'lint_sources', return_value=LintResult('all', 3, ())) as lint:
      code, stdout, stderr = self.invoke(['lint', '--article-root', str(ROOT), '--all'])
    self.assertEqual(code, 0)
    self.assertIn('lint: scope=all articles=3 errors=0', stdout)
    self.assertIn('公開準備の判定ではありません', stdout)
    self.assertEqual(stderr, '')
    for text in ('SUCCESS', 'COMMIT_READY', 'commit/push', 'ローカル変換が完了'):
      self.assertNotIn(text, stdout)
    lint.assert_called_once_with(ROOT, all_articles=True, articles=())
    self.runner.assert_not_called()

  def test_lint_repeated_targets_and_validation_exit_one(self):
    from converter.lint import LintDiagnostic, LintResult
    diagnostic = LintDiagnostic('articles/a.md', 'error', 'INVALID_UUID', 'Invalid source UUID')
    with patch.object(cli, 'lint_sources', return_value=LintResult('selected', 2, (diagnostic,))) as lint:
      code, stdout, stderr = self.invoke(['lint', '--article-root', str(ROOT),
        '--article', 'articles/a.md', '--article', 'articles/nested/b.md'])
    self.assertEqual(code, 1)
    self.assertIn('scope=selected articles=2 errors=1', stdout)
    self.assertEqual(stderr, 'articles/a.md: error [INVALID_UUID]: Invalid source UUID\n')
    lint.assert_called_once_with(ROOT, all_articles=False,
      articles=('articles/a.md', 'articles/nested/b.md'))
    self.runner.assert_not_called()

  def test_lint_failures_and_interrupt_never_print_deploy_recovery(self):
    from converter.lint import LintError
    for error, expected in ((LintError('unsafe target'), 2),
                            (ValueError('unexpected value'), 2),
                            (LocalApplyError('unexpected local validation error'), 2),
                            (RuntimeError('unexpected failure'), 2),
                            (KeyboardInterrupt(), 130)):
      with self.subTest(error=error), patch.object(cli, 'lint_sources', side_effect=error):
        code, stdout, stderr = self.invoke(['lint', '--article-root', str(ROOT), '--all'])
      self.assertEqual(code, expected)
      self.assertEqual(stdout, '')
      self.assertTrue(stderr)
      for text in ('部分変更', 'commit/push', 'Git status', 'pending_next_state',
                   '手動で復旧', 'SUCCESS', 'COMMIT_READY'):
        self.assertNotIn(text, stderr)
    self.runner.assert_not_called()

  def test_lint_reporting_failure_does_not_print_recovery(self):
    from converter.lint import LintResult
    with patch.object(cli, 'lint_sources', return_value=LintResult('all', 0, ())), patch(
        'converter.cli.print', side_effect=RuntimeError('output failed')):
      code, stdout, stderr = self.invoke(['lint', '--article-root', str(ROOT), '--all'])
    self.assertEqual(code, 2)
    self.assertEqual(stdout, '')
    self.assertIn('output failed', stderr)
    self.assertNotIn('部分変更', stderr)
    self.runner.assert_not_called()

  def test_lint_real_api_validation_and_target_errors(self):
    from converter.git_inspection import Worktrees
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      (root / 'articles').mkdir()
      (root / 'articles/invalid.md').write_text('Invalid Markdown', encoding='utf-8')
      with patch('converter.lint.identify_worktrees',
                 return_value=Worktrees(root, root / '_deploy')):
        prefix = ['lint', '--article-root', str(root)]
        code, stdout, stderr = self.invoke(prefix + ['--all'])
        self.assertEqual(code, 1)
        self.assertIn('articles=1 errors=1', stdout)
        self.assertIn('articles/invalid.md: error [SOURCE_PARSE]', stderr)
        for target in ('articles/missing.md', 'articles/../invalid.md'):
          code, stdout, stderr = self.invoke(prefix + ['--article', target])
          self.assertEqual(code, 2)
          self.assertEqual(stdout, '')
          self.assertNotIn('部分変更', stderr)
        (root / 'articles/link.md').symlink_to(root / 'articles/invalid.md')
        code, _, stderr = self.invoke(prefix + ['--article', 'articles/link.md'])
        self.assertEqual(code, 2)
        self.assertIn('Refusing symlink', stderr)
    self.runner.assert_not_called()
