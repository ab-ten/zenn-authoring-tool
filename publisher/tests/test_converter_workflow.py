"""隔離 Git worktree で workflow の接続と書き込み境界を検証します。"""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from checker import atomic_write, unified_preflight
from checker.create_observation import ArticleResponse
from checker.create_preflight import PreflightResult, PreflightStatus
from checker.errors import LocalApplyError
from converter import workflow, apply_integration, preflight_integration, planning
from converter.models import PublishPlan
from converter.preflight_integration import PreflightOutcome
from converter.source_parser import parse_source
from test_article_integration import ID, OTHER, SLUG, NOW, OBSERVATION, source
from test_entry_guards import fixture_git, snapshot
from test_update_preflight import response
from test_create_preflight import response as other_response


class WorkflowTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name) / 'article repo'
    self.root.mkdir()
    fixture_git(self.root, 'init', '-b', 'main')
    fixture_git(self.root, 'config', 'worktree.useRelativePaths', 'true')
    (self.root / 'articles').mkdir()
    (self.root / 'images').mkdir()
    (self.root / 'images/asset.png').write_bytes(b'image')
    (self.root / '.gitignore').write_text('/_deploy/\n')
    self.commit(self.root)
    fixture_git(self.root, 'worktree', 'add', '--relative-paths', '-b', 'deploy', '_deploy')
    self.deploy = self.root / '_deploy'
    self.management = self.deploy / '.zenn-authoring'
    (self.management / 'released').mkdir(parents=True)
    self.ledger_path = self.management / 'ledger.json'
    self.ledger = {'articles': {}, 'pending_next_state': {}}
    self.save_ledger()
    (self.management / 'config.json').write_text(json.dumps({'zenn_user': {'id': 2}}))
    self.commit(self.deploy)
    for target in ('urllib.request.urlopen', 'checker.zenn_http.urlopen'):
      mock = self.enterContext(patch(target, side_effect=AssertionError('Live HTTP forbidden')))
      self.addCleanup(mock.assert_not_called)

  def commit(self, root):
    fixture_git(root, 'add', '-A')
    fixture_git(root, 'commit', '--allow-empty', '-m', 'fixture')

  def save_ledger(self):
    self.ledger_path.write_text(json.dumps(self.ledger))

  def write_source(self, markdown=None, name='human filename.md', commit=True):
    path = self.root / 'articles' / name
    path.write_text(source() if markdown is None else markdown)
    if commit:
      self.commit(self.root)
    return path

  def confirmed(self, state='active'):
    content = parse_source(source(release=3)).record.candidate_content.encode()
    self.ledger['articles'][ID] = {
      'state': state, 'slug': SLUG, 'successful_release': 3,
      'successful_zenn_observation': dict(OBSERVATION)}
    (self.management / 'released' / f'{ID}.md').write_bytes(content)
    if state == 'active':
      (self.deploy / 'articles').mkdir(exist_ok=True)
      (self.deploy / 'articles' / f'{SLUG}.md').write_bytes(content)
    self.save_ledger()
    self.commit(self.deploy)

  def images(self):
    (self.deploy / 'images/old.png').write_bytes(b'old')
    self.commit(self.deploy)
    (self.root / 'images/asset.png').write_bytes(b'updated')
    (self.root / 'images/new.png').write_bytes(b'new')
    self.commit(self.root)

  def run_workflow(self, responses=(), status=PreflightStatus.OK, requested=None):
    before_sources = snapshot(self.root / 'articles')
    before_images = snapshot(self.root / 'images')
    before_released = snapshot(self.management / 'released')
    confirmed = json.loads(self.ledger_path.read_bytes())['articles']
    before_deploy = snapshot(self.deploy)
    fetch = Mock(side_effect=list(responses))
    captured, writes = {}, []
    real_preflight = workflow.preflight_publish
    real_write = apply_integration.atomic_replace_bytes
    real_remote = unified_preflight.preflight_plan
    def remote(root, plan, injected):
      captured['approved_plan'] = plan
      return real_remote(root, plan, injected)
    def preflight(*args, **kwargs):
      value = real_preflight(*args, **kwargs)
      captured['outcome'] = value
      captured['plan_repr'] = repr(value.plan)
      return value
    def write(path, content, **kwargs):
      writes.append(path)
      if path == self.ledger_path:
        plan = captured['outcome'].plan
        for entry in plan.articles:
          target = self.deploy / 'articles' / f'{entry.slug}.md'
          if entry.operation == 'DELETE':
            self.assertFalse(target.exists())
          else:
            self.assertEqual(target.read_bytes(), entry.candidate_content.encode())
        for entry in plan.images:
          target = self.deploy / entry.path
          if entry.operation == 'DELETE':
            self.assertFalse(target.exists())
          else:
            self.assertEqual(target.read_bytes(), entry.content)
      return real_write(path, content, **kwargs)
    real_apply = workflow.apply_publish
    def apply(outcome):
      self.assertIs(outcome, captured['outcome'])
      with ExitStack() as stack:
        guards = [stack.enter_context(patch(target, side_effect=AssertionError('Repeated work')))
          for target in ('converter.preflight_integration.plan_repository',
                         'converter.planning._classify', 'converter.image_planning._mirror_diff',
                         'checker.unified_preflight.preflight_plan',
                         'converter.git_inspection.git')]
        observations = fetch.call_count
        real_apply(outcome)
        self.assertEqual(fetch.call_count, observations)
        for guard in guards:
          guard.assert_not_called()
    with patch.object(workflow, 'preflight_publish', side_effect=preflight) as pre, patch.object(
        workflow, 'apply_publish', side_effect=apply) as app, patch.object(
        preflight_integration, 'plan_repository', wraps=preflight_integration.plan_repository) as plan, patch.object(
        planning, '_classify', wraps=planning._classify) as classify, patch.object(
        unified_preflight, 'preflight_plan', side_effect=remote) as checker, patch.object(
        apply_integration, 'atomic_replace_bytes', side_effect=write):
      outcome = workflow.run_convert(self.root, now=NOW, fetch_article=fetch)
    pre.assert_called_once_with(self.root, now=NOW, fetch_article=fetch)
    plan.assert_called_once()
    checker.assert_called_once()
    self.assertIs(outcome.plan, captured['approved_plan'])
    self.assertEqual(classify.call_count, len(list((self.root / 'articles').glob('*.md'))))
    self.assertIs(outcome, captured['outcome'])
    self.assertEqual(repr(outcome.plan), captured['plan_repr'])
    self.assertEqual(outcome.preflight.result, status)
    if status is PreflightStatus.OK:
      app.assert_called_once_with(outcome)
      if outcome.plan.articles:
        self.assertEqual(writes[-1], self.ledger_path)
    else:
      app.assert_not_called()
      self.assertEqual(writes, [])
      self.assertEqual(snapshot(self.deploy), before_deploy)
    self.assertEqual(snapshot(self.root / 'articles'), before_sources)
    self.assertEqual(snapshot(self.root / 'images'), before_images)
    self.assertEqual(snapshot(self.management / 'released'), before_released)
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['articles'], confirmed)
    slugs = requested if requested is not None else [entry.slug for entry in outcome.plan.articles]
    self.assertEqual(fetch.call_args_list, [call(slug) for slug in slugs])
    return outcome

  def test_create_article_only(self):
    self.write_source()
    outcome = self.run_workflow([ArticleResponse(404)])
    self.assertEqual(outcome.plan.articles[0].operation, 'CREATE')
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID],
      {'operation': 'CREATE', 'slug': SLUG, 'state': 'active', 'release': 1})
    self.assertEqual(outcome.plan.images, ())

  def test_update_baseline(self):
    self.confirmed()
    current = {**OBSERVATION, 'body_updated_at': 'reviewed drift'}
    markdown = source(release=4, body='Updated\n').replace('<!-- #release:4 -->',
      '<!-- #release:4 -->\n<!-- #observation-override:' + json.dumps(current) + ' -->')
    self.write_source(markdown)
    outcome = self.run_workflow([response(current, slug=SLUG)])
    pending = json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID]
    self.assertEqual(pending['operation'], 'UPDATE')
    self.assertEqual(pending['baseline_observation'], current)
    self.assertEqual(outcome.preflight.articles[0].baseline_observation, current)
    self.assertNotIn('observation_override', pending)

  def test_delete(self):
    self.confirmed()
    self.write_source(source(state='erased', release=3))
    outcome = self.run_workflow([response(OBSERVATION, slug=SLUG)])
    self.assertEqual(outcome.plan.articles[0].operation, 'DELETE')
    self.assertFalse((self.deploy / 'articles' / f'{SLUG}.md').exists())
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID]['release'], 3)

  def test_recreate(self):
    self.confirmed('erased')
    self.write_source(source(release=3))
    outcome = self.run_workflow([ArticleResponse(404)])
    self.assertEqual(outcome.plan.articles[0].operation, 'RECREATE')
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID]['release'], 3)

  def test_recreate_new_slug_observes_old_and_new_with_one_preflight(self):
    self.confirmed('erased')
    new_slug = 'recreated-article-slug'
    self.write_source(source(slug=new_slug, release=4, body='Recreated\n'))
    outcome = self.run_workflow([other_response(slug=SLUG), ArticleResponse(404)],
      requested=[SLUG, new_slug])
    entry, = outcome.plan.articles
    self.assertEqual((entry.operation, entry.slug, entry.release), ('RECREATE', new_slug, 4))
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID]['slug'], new_slug)

  def test_image_only(self):
    self.confirmed()
    self.write_source(source(release=3))
    self.images()
    ledger = self.ledger_path.read_bytes()
    outcome = self.run_workflow()
    self.assertEqual(outcome.plan.articles, ())
    self.assertEqual({entry.operation for entry in outcome.plan.images}, {'ADD', 'UPDATE', 'DELETE'})
    self.assertEqual(self.ledger_path.read_bytes(), ledger)

  def test_mixed_articles_and_combined_images(self):
    self.write_source()
    self.write_source(source(article_id=OTHER, slug='second-article-slug'), name='second.md')
    self.images()
    outcome = self.run_workflow([ArticleResponse(404), ArticleResponse(404)])
    self.assertEqual(len(outcome.plan.articles), 2)
    self.assertEqual({entry.operation for entry in outcome.plan.images}, {'ADD', 'UPDATE', 'DELETE'})
    self.assertEqual(set(json.loads(self.ledger_path.read_bytes())['pending_next_state']), {ID, OTHER})

  def test_noop_has_no_writes(self):
    self.confirmed()
    self.write_source(source(release=3))
    before = snapshot(self.deploy)
    with self.no_writes():
      outcome = self.run_workflow()
    self.assertEqual(outcome.plan, PublishPlan())
    self.assertEqual(snapshot(self.deploy), before)

  def no_writes(self):
    stack = ExitStack()
    for target in ('checker.atomic_write.atomic_replace_bytes', 'checker.atomic_write.os.replace',
                   'pathlib.Path.mkdir', 'pathlib.Path.unlink', 'pathlib.Path.rmdir',
                   'pathlib.Path.write_bytes', 'pathlib.Path.write_text'):
      stack.enter_context(patch(target, side_effect=AssertionError('Write forbidden')))
    return stack

  def test_create_collision_blocks_mixed_plan(self):
    self.write_source()
    self.images()
    with self.no_writes():
      self.run_workflow([other_response(slug=SLUG)], PreflightStatus.COLLISION)

  def test_recreate_collision_blocks_mixed_plan(self):
    self.confirmed('erased')
    self.write_source(source(release=3))
    self.images()
    with self.no_writes():
      self.run_workflow([other_response(slug=SLUG)], PreflightStatus.COLLISION)

  def test_overall_error_blocks_successful_article_and_images(self):
    self.write_source()
    self.write_source(source(article_id=OTHER, slug='second-article-slug'), name='second.md')
    self.images()
    with self.no_writes():
      self.run_workflow([ArticleResponse(404), ArticleResponse(503)], PreflightStatus.ERROR)

  def local_failure(self, pattern):
    before = snapshot(self.root)
    fetch = Mock()
    with self.no_writes(), patch.object(unified_preflight, 'preflight_plan') as remote, patch.object(
        workflow, 'apply_publish') as app:
      with self.assertRaisesRegex(ValueError, pattern):
        workflow.run_convert(self.root, now=NOW, fetch_article=fetch)
    remote.assert_not_called()
    app.assert_not_called()
    fetch.assert_not_called()
    self.assertEqual(snapshot(self.root), before)

  def test_local_validation_prevents_preflight_apply(self):
    self.write_source(source(release=2))
    self.local_failure('release')

  def test_dirty_deploy_prevents_planning(self):
    (self.deploy / 'dirty').write_bytes(b'dirty')
    with patch('converter.article_integration.plan_articles') as plan:
      self.local_failure('Dirty deploy')
    plan.assert_not_called()

  def test_pending_blocks_image_only_before_planning(self):
    self.images()
    self.ledger['pending_next_state'][ID] = {'operation': 'CREATE'}
    self.save_ledger()
    self.commit(self.deploy)
    with patch('converter.article_integration.plan_articles') as plan, patch(
        'converter.repository_planning.plan_images') as images:
      self.local_failure('pending_next_state')
    plan.assert_not_called()
    images.assert_not_called()

  def test_invalid_dirty_images(self):
    (self.root / 'images/asset.png').write_bytes(b'dirty')
    self.local_failure('Image')

  def test_invalid_untracked_images(self):
    (self.root / 'images/bad.svg').write_bytes(b'invalid')
    self.local_failure('Image')

  def test_invalid_committed_image(self):
    (self.root / 'images/bad.svg').write_bytes(b'invalid')
    self.commit(self.root)
    self.local_failure('extension')

  def test_invalid_dirty_hold_still_fully_validated(self):
    self.write_source(source(state='hold').replace('title: Test', '[invalid YAML'), commit=False)
    self.local_failure('.')

  def test_explicit_root_required(self):
    with self.assertRaises(TypeError):
      workflow.run_convert()
    with self.assertRaises(ValueError):
      workflow.run_convert(None, fetch_article=Mock())

  def test_apply_preparation_failure_has_no_writes(self):
    self.write_source()
    before = snapshot(self.root)
    real = workflow.preflight_publish
    def invalid_target(*args, **kwargs):
      outcome = real(*args, **kwargs)
      return PreflightOutcome(outcome.deploy_root, outcome.plan,
        PreflightResult(PreflightStatus.OK))
    with self.no_writes(), patch.object(workflow, 'preflight_publish', side_effect=invalid_target):
      with self.assertRaisesRegex(LocalApplyError, 'one-to-one'):
        workflow.run_convert(self.root, now=NOW, fetch_article=Mock(return_value=ArticleResponse(404)))
    self.assertEqual(snapshot(self.root), before)

  def partial_failure(self, boundary):
    self.write_source()
    self.images()
    old_ledger = self.ledger_path.read_bytes()
    sources = snapshot(self.root / 'articles')
    released = snapshot(self.management / 'released')
    real = apply_integration.atomic_replace_bytes
    attempts = []
    target = {'first': self.deploy / 'articles' / f'{SLUG}.md',
              'image': self.deploy / 'images/asset.png', 'ledger': self.ledger_path}[boundary]
    def write(path, content, **kwargs):
      attempts.append(path)
      if path == target:
        raise OSError('injected failure')
      return real(path, content, **kwargs)
    with patch.object(apply_integration, 'atomic_replace_bytes', side_effect=write):
      with self.assertRaisesRegex(LocalApplyError, 'injected failure'):
        workflow.run_convert(self.root, now=NOW, fetch_article=Mock(return_value=ArticleResponse(404)))
    self.assertEqual(attempts[-1], target)
    self.assertEqual(self.ledger_path.read_bytes(), old_ledger)
    self.assertEqual(snapshot(self.root / 'articles'), sources)
    self.assertEqual(snapshot(self.management / 'released'), released)
    artifact = self.deploy / 'articles' / f'{SLUG}.md'
    if boundary == 'first':
      self.assertFalse(artifact.exists())
      self.assertTrue((self.deploy / 'images/old.png').exists())
    else:
      self.assertEqual(artifact.read_bytes(), parse_source(source()).record.candidate_content.encode())
      self.assertFalse((self.deploy / 'images/old.png').exists())
      self.assertTrue(fixture_git(self.deploy, '--no-optional-locks', 'status', '--porcelain'))
      if boundary == 'image':
        self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'image')
        self.assertFalse((self.deploy / 'images/new.png').exists())
      else:
        self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'updated')
        self.assertEqual((self.deploy / 'images/new.png').read_bytes(), b'new')

  def test_first_apply_failure_stops_images(self):
    self.partial_failure('first')

  def test_partial_apply_failure_keeps_changes_without_rollback(self):
    self.partial_failure('image')

  def test_pending_commit_failure_keeps_all_artifacts(self):
    self.partial_failure('ledger')

  def test_exception_after_pending_commit_still_propagates(self):
    self.write_source()
    real = apply_integration.atomic_replace_bytes
    def write(path, content, **kwargs):
      real(path, content, **kwargs)
      if path == self.ledger_path:
        raise OSError('after commit')
    with patch.object(apply_integration, 'atomic_replace_bytes', side_effect=write):
      with self.assertRaisesRegex(LocalApplyError, 'after commit'):
        workflow.run_convert(self.root, now=NOW, fetch_article=Mock(return_value=ArticleResponse(404)))
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID]['operation'], 'CREATE')

  def test_contract_original_outcome_for_all_statuses(self):
    for status in PreflightStatus:
      with self.subTest(status=status):
        outcome = PreflightOutcome(self.deploy, PublishPlan(), PreflightResult(status))
        fetch = Mock()
        with patch.object(workflow, 'preflight_publish', return_value=outcome) as pre, patch.object(
            workflow, 'apply_publish') as app:
          self.assertIs(workflow.run_convert(self.root, now=NOW, fetch_article=fetch), outcome)
        pre.assert_called_once_with(self.root, now=NOW, fetch_article=fetch)
        if status is PreflightStatus.OK:
          self.assertIs(app.call_args.args[0], outcome)
          app.assert_called_once()
        else:
          app.assert_not_called()

  def test_unexpected_preflight_exception_propagates(self):
    error = RuntimeError('unexpected preflight')
    with patch.object(workflow, 'preflight_publish', side_effect=error) as pre, patch.object(
        workflow, 'apply_publish') as app:
      with self.assertRaises(RuntimeError) as caught:
        workflow.run_convert(self.root)
    self.assertIs(caught.exception, error)
    pre.assert_called_once()
    app.assert_not_called()

  def test_unexpected_apply_exception_propagates_unchanged(self):
    outcome = PreflightOutcome(self.deploy, PublishPlan(), PreflightResult(PreflightStatus.OK))
    error = RuntimeError('unexpected apply')
    with patch.object(workflow, 'preflight_publish', return_value=outcome) as pre, patch.object(
        workflow, 'apply_publish', side_effect=error) as app:
      with self.assertRaises(RuntimeError) as caught:
        workflow.run_convert(self.root)
    self.assertIs(caught.exception, error)
    pre.assert_called_once()
    app.assert_called_once_with(outcome)

  def test_atomic_pending_replace_failure_preserves_old_ledger(self):
    self.write_source()
    self.images()
    before = self.ledger_path.read_bytes()
    real_replace = atomic_write.os.replace
    def replace_file(src, dest):
      if dest == self.ledger_path:
        self.assertEqual(Path(src).parent, self.management)
        self.assertTrue((self.deploy / 'articles' / f'{SLUG}.md').exists())
        self.assertEqual((self.deploy / 'images/new.png').read_bytes(), b'new')
        raise OSError('atomic ledger replacement')
      return real_replace(src, dest)
    with patch.object(atomic_write.os, 'replace', side_effect=replace_file):
      with self.assertRaisesRegex(LocalApplyError, 'atomic ledger replacement'):
        workflow.run_convert(self.root, now=NOW, fetch_article=Mock(return_value=ArticleResponse(404)))
    self.assertEqual(self.ledger_path.read_bytes(), before)
    self.assertEqual(list(self.management.glob('*.tmp')), [])
