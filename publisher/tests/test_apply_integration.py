"""隔離 Git repository で逐次 Apply と pending commit 境界を検証します。"""

from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from checker import atomic_write
from checker.create_observation import ArticleResponse
from checker.errors import LocalApplyError
from converter import apply_integration as integration
from converter import apply_preparation as preparation
from converter.entry_guards import inspect_inputs
from converter.git_inspection import GuardError, inspect_status
from converter.image_planning import plan_images
from converter.models import ImagePlanEntry
from converter.preflight_integration import preflight_publish
import test_apply_preparation as fixtures
from test_article_integration import ID, OTHER, SLUG, NOW, OBSERVATION, source
from test_entry_guards import snapshot, fixture_git
from test_unified_preflight import DELETE_SLUG, NEW_SLUG


class ApplyIntegrationTests(unittest.TestCase):
  setUp = fixtures.ApplyPreparationTests.setUp
  commit = fixtures.ApplyPreparationTests.commit
  save_ledger = fixtures.ApplyPreparationTests.save_ledger
  write_source = fixtures.ApplyPreparationTests.write_source
  confirm = fixtures.ApplyPreparationTests.confirm
  outcome = fixtures.ApplyPreparationTests.outcome
  create = fixtures.ApplyPreparationTests.create
  mixed = fixtures.ApplyPreparationTests.mixed

  def preserved(self):
    return (snapshot(self.root / 'articles'), snapshot(self.root / 'images'),
            snapshot(self.management / 'released'), dict(self.ledger['articles']))

  def assert_preserved(self, before):
    self.assertEqual(self.preserved(), before)
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['articles'], before[3])
    self.http_mock.assert_not_called()
    self.zenn_http_mock.assert_not_called()

  def apply(self, outcome):
    before = self.preserved()
    original = repr(outcome)
    real = preparation.prepare_apply
    captured = []
    def prepare(value):
      self.assertIs(value, outcome)
      prepared = real(value)
      captured.append(prepared)
      return prepared
    with ExitStack() as stack:
      mocks = [stack.enter_context(patch(target, side_effect=AssertionError('Unexpected work')))
               for target in ('converter.source_parser.parse_source', 'converter.planning._classify',
                              'converter.image_planning._mirror_diff',
                              'checker.unified_preflight.preflight_plan',
                              'checker.zenn_http.fetch_article', 'converter.git_inspection.git')]
      prepare_mock = stack.enter_context(patch.object(integration, 'prepare_apply', side_effect=prepare))
      apply_mock = stack.enter_context(patch.object(integration, '_apply_prepared',
                                                    wraps=integration._apply_prepared))
      integration.apply_publish(outcome)
    prepare_mock.assert_called_once_with(outcome)
    apply_mock.assert_called_once_with(captured[0])
    for mock in mocks:
      mock.assert_not_called()
    self.assertIs(captured[0].outcome, outcome)
    for entry, target in zip(outcome.plan.articles, captured[0].article_targets, strict=True):
      self.assertIs(target.entry, entry)
    targets = captured[0].image_deletes + captured[0].image_writes
    for entry in outcome.plan.images:
      self.assertTrue(any(target.entry is entry for target in targets))
    self.assertEqual(repr(outcome), original)
    self.assert_preserved(before)
    return captured[0]

  def test_create_exact_utf8_and_final_ledger_write(self):
    entry = self.create(candidate_content='---\ntitle: 日本語\n---\nBody\r\n')
    outcome = self.outcome((entry,))
    calls = []
    real = integration.atomic_replace_bytes
    def write(path, content, **kwargs):
      calls.append((path, content))
      if path == self.ledger_path:
        self.assertEqual((self.deploy / 'articles' / f'{SLUG}.md').read_bytes(),
                         entry.candidate_content.encode('utf-8'))
      return real(path, content, **kwargs)
    with patch.object(integration, 'atomic_replace_bytes', side_effect=write):
      prepared = self.apply(outcome)
    self.assertEqual(calls, [(prepared.article_targets[0].path, entry.candidate_content.encode()),
                            (self.ledger_path, prepared.pending_ledger_bytes)])
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID],
                     {'operation': 'CREATE', 'slug': SLUG, 'state': 'active', 'release': 1})

  def test_mixed_articles_exact_contents_snapshots_and_baseline(self):
    outcome = self.mixed()
    before = snapshot(self.management / 'released')
    prepared = self.apply(outcome)
    for target in prepared.article_targets:
      if target.entry.operation == 'DELETE':
        self.assertFalse(target.path.exists())
      else:
        self.assertEqual(target.path.read_bytes(), target.entry.candidate_content.encode())
    self.assertEqual(snapshot(self.management / 'released'), before)
    self.assertTrue((self.deploy / 'articles' / f'{NEW_SLUG}.md').is_file())
    ledger = json.loads(self.ledger_path.read_bytes())
    self.assertEqual(ledger['pending_next_state'][OTHER]['baseline_observation'], OBSERVATION)
    self.assertNotIn('observation_override', ledger['pending_next_state'][OTHER])
    self.assertEqual(self.ledger_path.read_bytes(), prepared.pending_ledger_bytes)

  def test_real_preflight_source_filename_and_comments_are_not_materialized(self):
    path = self.write_source()
    (self.management / 'config.json').write_text(json.dumps({'zenn_user': {'id': 12345}}))
    self.commit(self.deploy)
    outcome = preflight_publish(self.root, now=NOW, fetch_article=lambda slug: ArticleResponse(404))
    source_bytes = path.read_bytes()
    self.apply(outcome)
    artifact = self.deploy / 'articles' / f'{SLUG}.md'
    self.assertEqual(artifact.read_bytes(), outcome.plan.articles[0].candidate_content.encode())
    self.assertNotIn(b'<!-- #', artifact.read_bytes())
    self.assertEqual(path.read_bytes(), source_bytes)
    self.assertFalse((artifact.parent / path.name).exists())

  def test_nested_add_and_unusual_filename(self):
    entry = ImagePlanEntry('ADD', 'images/nested/a b\nc\t日本語:#?.png', b'\x00\xfffixed')
    old = self.ledger_path.read_bytes()
    self.apply(self.outcome(images=(entry,)))
    self.assertEqual((self.deploy / entry.path).read_bytes(), entry.content)
    self.assertEqual(self.ledger_path.read_bytes(), old)

  def test_same_size_binary_update_preserves_executable_mode(self):
    path = self.deploy / 'images/asset.png'
    path.chmod(0o755)
    self.commit(self.deploy)
    entry = ImagePlanEntry('UPDATE', 'images/asset.png', b'\x00\xff\x01\xfe\x02')
    self.apply(self.outcome(images=(entry,)))
    self.assertEqual(path.read_bytes(), entry.content)
    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
    diff = fixture_git(self.deploy, 'diff', '--summary')
    self.assertNotIn(b'mode change', diff)

  def test_mixed_images_and_unrelated_files(self):
    (self.deploy / 'images/old.png').write_bytes(b'old')
    (self.deploy / 'unrelated').write_bytes(b'untouched')
    self.commit(self.deploy)
    old = self.ledger_path.read_bytes()
    entries = (ImagePlanEntry('ADD', 'images/new.png', b'new'),
               ImagePlanEntry('UPDATE', 'images/asset.png', b'other'),
               ImagePlanEntry('DELETE', 'images/old.png'))
    self.apply(self.outcome(images=entries))
    self.assertEqual((self.deploy / 'images/new.png').read_bytes(), b'new')
    self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'other')
    self.assertFalse((self.deploy / 'images/old.png').exists())
    self.assertEqual((self.deploy / 'unrelated').read_bytes(), b'untouched')
    self.assertEqual(self.ledger_path.read_bytes(), old)

  def test_file_to_directory_transition(self):
    entries = (ImagePlanEntry('ADD', 'images/asset.png/deep/new.png', b'fixed'),
               ImagePlanEntry('DELETE', 'images/asset.png'))
    self.apply(self.outcome(images=entries))
    self.assertEqual((self.deploy / entries[0].path).read_bytes(), b'fixed')

  def transition(self):
    base = self.deploy / 'images/replacement.png'
    (base / 'nested/empty').mkdir(parents=True)
    (base / 'nested/old.png').write_bytes(b'old')
    return self.outcome(images=(ImagePlanEntry('ADD', 'images/replacement.png', b'fixed'),
      ImagePlanEntry('DELETE', 'images/replacement.png/nested/old.png')))

  def test_directory_to_file_removes_deepest_first(self):
    outcome = self.transition()
    removed = []
    real = Path.rmdir
    def remove(path):
      removed.append(path)
      return real(path)
    with patch.object(Path, 'rmdir', remove):
      prepared = self.apply(outcome)
    self.assertEqual(removed, list(prepared.image_directories_to_remove))
    self.assertEqual((self.deploy / 'images/replacement.png').read_bytes(), b'fixed')

  def test_noop_changes_no_files_or_modes_or_times(self):
    before = fixtures.ApplyPreparationTests.tree(self)
    with patch.object(integration, 'atomic_replace_bytes') as write:
      self.apply(self.outcome())
    write.assert_not_called()
    self.assertEqual(fixtures.ApplyPreparationTests.tree(self), before)

  def test_original_image_bytes_without_source_reread(self):
    entry = ImagePlanEntry('UPDATE', 'images/asset.png', b'fixed')
    (self.root / 'images/asset.png').write_bytes(b'changed after planning')
    self.apply(self.outcome(images=(entry,)))
    self.assertEqual((self.deploy / entry.path).read_bytes(), b'fixed')

  def test_backslash_rejected_by_models_and_preparation(self):
    with self.assertRaisesRegex(ValueError, 'Unsafe image path'):
      ImagePlanEntry('ADD', 'images/a\\b.png', b'new')
    entry = ImagePlanEntry('ADD', 'images/valid.png', b'new')
    object.__setattr__(entry, 'path', 'images/a\\b.png')
    with patch.object(integration, '_apply_prepared') as apply:
      with self.assertRaises(LocalApplyError):
        integration.apply_publish(self.outcome(images=(entry,)))
    apply.assert_not_called()

  def test_non_ok_never_mutates(self):
    from checker.create_preflight import PreflightStatus
    outcome = self.outcome((self.create(),))
    for status in (PreflightStatus.ERROR, PreflightStatus.COLLISION):
      with patch.object(integration, '_apply_prepared') as apply:
        with self.assertRaises(LocalApplyError):
          integration.apply_publish(replace(outcome, preflight=replace(outcome.preflight, result=status)))
      apply.assert_not_called()

  def after_preparation(self, outcome, mutate):
    old = self.ledger_path.read_bytes()
    real = preparation.prepare_apply
    def prepare(value):
      prepared = real(value)
      mutate(prepared)
      return prepared
    with patch.object(integration, 'prepare_apply', side_effect=prepare):
      with self.assertRaises(LocalApplyError):
        integration.apply_publish(outcome)
    self.assertEqual(self.ledger_path.read_bytes(), old)

  def test_unexpected_article_create_and_recreate_targets(self):
    for operation in ('CREATE', 'RECREATE'):
      outcome = self.outcome((self.create(),)) if operation == 'CREATE' else self.mixed()
      selected = 0 if operation == 'CREATE' else 3
      self.after_preparation(outcome, lambda prepared:
        self.make_file(prepared.article_targets[selected].path, b'unrelated'))
      self.assertEqual((self.deploy / 'articles' / f'{outcome.plan.articles[selected].slug}.md').read_bytes(),
                       b'unrelated')
      (self.deploy / 'articles' / f'{outcome.plan.articles[selected].slug}.md').unlink()

  def make_file(self, path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

  def test_missing_or_directory_article_update_delete_rejected(self):
    for index in (1, 2):
      for kind in ('missing', 'directory'):
        outcome = self.mixed()
        def mutate(prepared):
          path = prepared.article_targets[index].path
          path.unlink()
          if kind == 'directory':
            path.mkdir()
        self.after_preparation(outcome, mutate)
        for target in (self.deploy / 'articles').iterdir():
          if target.is_dir():
            target.rmdir()
          else:
            target.unlink()

  def test_image_add_rejects_unexpected_regular_target(self):
    entry = ImagePlanEntry('ADD', 'images/new.png', b'planned')
    self.after_preparation(self.outcome(images=(entry,)), lambda prepared:
      prepared.image_writes[0].path.write_bytes(b'unrelated'))
    self.assertEqual((self.deploy / entry.path).read_bytes(), b'unrelated')

  def test_execution_rejects_symlinks_special_files_and_invalid_parents(self):
    for operation in ('ADD', 'UPDATE', 'DELETE'):
      for kind in ('symlink', 'fifo', 'directory', 'missing'):
        if operation == 'ADD' and kind == 'missing':
          continue
        with self.subTest(operation=operation, kind=kind):
          path = self.deploy / ('images/new.png' if operation == 'ADD' else 'images/asset.png')
          if operation != 'ADD':
            path.write_bytes(b'image')
          entry = ImagePlanEntry(operation, path.relative_to(self.deploy).as_posix(),
                                 None if operation == 'DELETE' else b'fixed')
          def mutate(prepared):
            if path.exists():
              path.unlink()
            if kind == 'symlink':
              path.symlink_to(self.root / 'images/asset.png')
            elif kind == 'fifo':
              os.mkfifo(path)
            elif kind == 'directory':
              path.mkdir()
          self.after_preparation(self.outcome(images=(entry,)), mutate)
          if path.is_dir():
            path.rmdir()
          elif path.exists() or path.is_symlink():
            path.unlink()
    for kind in ('symlink', 'fifo', 'file'):
      parent = self.deploy / 'images/parent'
      entry = ImagePlanEntry('ADD', 'images/parent/new.png', b'fixed')
      def mutate(prepared):
        if kind == 'symlink':
          parent.symlink_to(self.root / 'images', target_is_directory=True)
        elif kind == 'fifo':
          os.mkfifo(parent)
        else:
          parent.write_bytes(b'bad')
      self.after_preparation(self.outcome(images=(entry,)), mutate)
      parent.unlink()

  def test_nonempty_directory_added_after_preparation_is_not_removed(self):
    outcome = self.transition()
    self.after_preparation(outcome, lambda prepared:
      (prepared.image_directories_to_remove[0] / 'unexpected').write_bytes(b'keep'))
    self.assertTrue((self.deploy / 'images/replacement.png/nested/empty/unexpected').exists())
    self.assertFalse((self.deploy / 'images/replacement.png/nested/old.png').exists())

  def assert_failure(self, outcome, target, side_effect):
    old = self.ledger_path.read_bytes()
    with patch(target, autospec=True, side_effect=side_effect):
      with self.assertRaisesRegex(LocalApplyError, 'failed') as caught:
        integration.apply_publish(outcome)
    self.assertEqual(self.ledger_path.read_bytes(), old)
    self.assertEqual(json.loads(old)['pending_next_state'], {})
    return str(caught.exception)

  def test_first_article_write_failure_stops_without_pending(self):
    outcome = self.mixed()
    with patch.object(integration, 'atomic_replace_bytes', side_effect=OSError('first')) as write:
      with self.assertRaisesRegex(LocalApplyError, '0 preceding'):
        integration.apply_publish(outcome)
    write.assert_called_once()
    self.assertEqual(self.ledger_path.read_bytes(), json.dumps(self.ledger).encode())
    self.assertFalse((self.deploy / 'articles' / f'{SLUG}.md').exists())
    self.assertTrue((self.deploy / 'articles' / f'{DELETE_SLUG}.md').exists())

  def test_later_article_write_partial_changes_and_dirty_guard(self):
    outcome = self.mixed()
    real = integration.atomic_replace_bytes
    calls = []
    def write(path, content, **kwargs):
      calls.append(path)
      if len(calls) == 2:
        raise OSError('later article')
      return real(path, content, **kwargs)
    message = self.assert_failure(outcome, 'converter.apply_integration.atomic_replace_bytes', write)
    self.assertIn('1 preceding', message)
    self.assertEqual(len(calls), 2)
    self.assertEqual((self.deploy / 'articles' / f'{SLUG}.md').read_bytes(), b'candidate')
    self.assertTrue((self.deploy / 'articles' / f'{DELETE_SLUG}.md').exists())
    self.assertFalse((self.deploy / 'articles' / f'{NEW_SLUG}.md').exists())
    self.assertTrue(inspect_status(self.deploy))
    with self.assertRaisesRegex(GuardError, 'Dirty deploy'):
      inspect_inputs(self.root)

  def test_article_deletion_failure_stops_before_recreate(self):
    outcome = self.mixed()
    real = Path.unlink
    def unlink(path, *args, **kwargs):
      if path.name == f'{DELETE_SLUG}.md':
        raise OSError('article delete')
      return real(path, *args, **kwargs)
    self.assert_failure(outcome, 'pathlib.Path.unlink', unlink)
    self.assertTrue((self.deploy / 'articles' / f'{SLUG}.md').exists())
    self.assertTrue((self.deploy / 'articles' / f'{DELETE_SLUG}.md').exists())
    self.assertFalse((self.deploy / 'articles' / f'{NEW_SLUG}.md').exists())

  def mixed_with_images(self, images):
    outcome = self.mixed()
    return replace(outcome, plan=replace(outcome.plan, images=images))

  def test_image_delete_failure_stops_before_image_writes(self):
    outcome = self.mixed_with_images((ImagePlanEntry('DELETE', 'images/asset.png'),
                                     ImagePlanEntry('ADD', 'images/new.png', b'new')))
    real = Path.unlink
    def unlink(path, *args, **kwargs):
      if path == self.deploy / 'images/asset.png':
        raise OSError('image delete')
      return real(path, *args, **kwargs)
    self.assert_failure(outcome, 'pathlib.Path.unlink', unlink)
    self.assertTrue((self.deploy / 'images/asset.png').exists())
    self.assertFalse((self.deploy / 'images/new.png').exists())

  def test_directory_removal_failure_stops_before_writes(self):
    outcome = self.transition()
    outcome = self.mixed_with_images(outcome.plan.images)
    self.assert_failure(outcome, 'pathlib.Path.rmdir', OSError('directory removal'))
    self.assertFalse((self.deploy / 'images/replacement.png/nested/old.png').exists())
    self.assertTrue((self.deploy / 'images/replacement.png').is_dir())

  def test_parent_creation_failure_keeps_prior_mutations(self):
    outcome = self.mixed_with_images((ImagePlanEntry('ADD', 'images/new/deep/a.png', b'new'),))
    real = Path.mkdir
    def mkdir(path, *args, **kwargs):
      if path == self.deploy / 'images/new/deep':
        raise OSError('mkdir')
      return real(path, *args, **kwargs)
    self.assert_failure(outcome, 'pathlib.Path.mkdir', mkdir)
    self.assertTrue((self.deploy / 'images/new').is_dir())
    self.assertFalse((self.deploy / 'images/new/deep').exists())
    self.assertTrue((self.deploy / 'articles' / f'{NEW_SLUG}.md').exists())

  def test_image_add_and_update_failures_stop_later_operations(self):
    for operation in ('ADD', 'UPDATE'):
      with self.subTest(operation=operation):
        path = 'images/new.png' if operation == 'ADD' else 'images/asset.png'
        outcome = self.mixed_with_images((ImagePlanEntry(operation, path, b'fixed'),
                                         ImagePlanEntry('ADD', 'images/last.png', b'last')))
        real = integration.atomic_replace_bytes
        attempted = []
        def write(target, content, **kwargs):
          attempted.append(target)
          if target == self.deploy / path:
            raise OSError('image write')
          return real(target, content, **kwargs)
        self.assert_failure(outcome, 'converter.apply_integration.atomic_replace_bytes', write)
        self.assertEqual(attempted[-1], self.deploy / path)
        self.assertFalse((self.deploy / 'images/last.png').exists())
        if operation == 'UPDATE':
          self.assertEqual((self.deploy / path).read_bytes(), b'image')
        for artifact in (self.deploy / 'articles').iterdir():
          artifact.unlink()

  def test_atomic_replace_failure_keeps_old_artifact_and_cleans_temp(self):
    self.confirm()
    entry = self.create(article_id=ID, operation='UPDATE', release=4, candidate_content='updated')
    path = self.deploy / 'articles' / f'{SLUG}.md'
    old = path.read_bytes()
    self.assert_failure(self.outcome((entry,)), 'checker.atomic_write.os.replace', OSError('replace'))
    self.assertEqual(path.read_bytes(), old)
    self.assertEqual(tuple(path.parent.iterdir()), (path,))

  def ledger_outcome(self):
    return self.outcome((self.create(),), (ImagePlanEntry('UPDATE', 'images/asset.png', b'fixed'),))

  def test_ledger_temp_write_failure_does_not_commit(self):
    outcome = self.ledger_outcome()
    real = atomic_write.os.fdopen
    def fdopen(fd, *args, **kwargs):
      stream = real(fd, *args, **kwargs)
      class FailedWrite:
        def __enter__(self):
          return self
        def __exit__(self, *args):
          stream.close()
        def write(self, content):
          raise OSError('ledger temporary write')
      return FailedWrite() if (self.deploy / 'articles' / f'{SLUG}.md').exists() and (
        self.deploy / 'images/asset.png').read_bytes() == b'fixed' else stream
    self.assert_failure(outcome, 'checker.atomic_write.os.fdopen', fdopen)
    self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'fixed')
    self.assertEqual(list(self.management.glob('*.tmp')), [])

  def test_ledger_replace_failure_keeps_old_ledger_and_all_artifacts(self):
    outcome = self.ledger_outcome()
    real = atomic_write.os.replace
    def replace_file(src, dest):
      if dest == self.ledger_path:
        self.assertEqual((self.deploy / 'articles' / f'{SLUG}.md').read_bytes(), b'candidate')
        self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'fixed')
        raise OSError('ledger replace')
      return real(src, dest)
    self.assert_failure(outcome, 'checker.atomic_write.os.replace', replace_file)
    self.assertEqual(list(self.management.glob('*.tmp')), [])

  def test_exception_after_ledger_commit_reports_failure_with_complete_pending(self):
    outcome = self.ledger_outcome()
    real = integration.atomic_replace_bytes
    def write(path, content, **kwargs):
      result = real(path, content, **kwargs)
      if path == self.ledger_path:
        raise OSError('simulated exception after commit')
      return result
    with patch.object(integration, 'atomic_replace_bytes', side_effect=write):
      with self.assertRaisesRegex(LocalApplyError, 'after commit'):
        integration.apply_publish(outcome)
    self.assertEqual((self.deploy / 'articles' / f'{SLUG}.md').read_bytes(), b'candidate')
    self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'fixed')
    self.assertEqual(json.loads(self.ledger_path.read_bytes())['pending_next_state'][ID]['operation'], 'CREATE')

  def test_successful_article_pending_blocks_next_normal_conversion(self):
    self.apply(self.outcome((self.create(),)))
    self.commit(self.deploy)
    with self.assertRaisesRegex(GuardError, 'pending_next_state'):
      inspect_inputs(self.root)

  def test_successful_image_only_has_no_remaining_diff(self):
    (self.root / 'images/asset.png').write_bytes(b'changed')
    (self.root / 'images/new.png').write_bytes(b'new')
    self.commit(self.root)
    inputs = inspect_inputs(self.root)
    images = plan_images(inputs).images
    old = self.ledger_path.read_bytes()
    self.apply(self.outcome(images=images))
    self.assertEqual(self.ledger_path.read_bytes(), old)
    self.commit(self.deploy)
    self.assertEqual(plan_images(inspect_inputs(self.root)).images, ())

  def test_complete_operation_order_and_atomic_article_update(self):
    images = self.transition().plan.images + (
      ImagePlanEntry('DELETE', 'images/asset.png'),
      ImagePlanEntry('ADD', 'images/asset.png/new.png', b'new'))
    outcome = self.mixed_with_images(images)
    prepared = preparation.prepare_apply(outcome)
    events, replacements = [], []
    real_write, real_unlink, real_rmdir = integration.atomic_replace_bytes, Path.unlink, Path.rmdir
    real_replace = atomic_write.os.replace
    def write(path, content, **kwargs):
      events.append(('write', path))
      if path == self.ledger_path:
        for target in prepared.article_targets:
          if target.entry.operation == 'DELETE':
            self.assertFalse(target.path.exists())
          else:
            self.assertEqual(target.path.read_bytes(), target.entry.candidate_content.encode())
        for target in prepared.image_writes:
          self.assertEqual(target.path.read_bytes(), target.entry.content)
      return real_write(path, content, **kwargs)
    def unlink(path, *args, **kwargs):
      events.append(('delete', path))
      return real_unlink(path, *args, **kwargs)
    def rmdir(path):
      events.append(('rmdir', path))
      return real_rmdir(path)
    def replace_file(src, dest):
      self.assertEqual(Path(src).parent, dest.parent)
      replacements.append(dest)
      return real_replace(src, dest)
    with patch.object(integration, 'atomic_replace_bytes', side_effect=write), patch.object(
        Path, 'unlink', unlink), patch.object(Path, 'rmdir', rmdir), patch.object(
        atomic_write.os, 'replace', side_effect=replace_file):
      self.apply(outcome)
    expected = [('delete' if target.entry.operation == 'DELETE' else 'write', target.path)
                for target in prepared.article_targets]
    expected += [('delete', target.path) for target in prepared.image_deletes]
    expected += [('rmdir', path) for path in prepared.image_directories_to_remove]
    expected += [('write', target.path) for target in prepared.image_writes]
    expected += [('write', self.ledger_path)]
    self.assertEqual(events, expected)
    self.assertEqual(replacements, [path for operation, path in expected if operation == 'write'])

  def test_article_symlink_special_file_and_parent_rejected_at_execution(self):
    for kind in ('symlink', 'fifo', 'parent'):
      outcome = self.outcome((self.create(),))
      parent = self.deploy / 'articles'
      path = parent / f'{SLUG}.md'
      def mutate(prepared):
        if kind == 'parent':
          parent.symlink_to(self.root / 'articles', target_is_directory=True)
        else:
          parent.mkdir()
          if kind == 'symlink':
            path.symlink_to(self.root / 'missing')
          else:
            os.mkfifo(path)
      self.after_preparation(outcome, mutate)
      if kind == 'parent':
        parent.unlink()
      else:
        path.unlink()
        parent.rmdir()

  def test_directory_removal_rejects_changed_object(self):
    outcome = self.transition()
    empty = self.deploy / 'images/replacement.png/nested/empty'
    def mutate(prepared):
      empty.rmdir()
      empty.symlink_to(self.root / 'images', target_is_directory=True)
    self.after_preparation(outcome, mutate)
    self.assertTrue(empty.is_symlink())
    self.assertFalse((empty.parent / 'old.png').exists())

  def test_ledger_symlink_before_commit_is_rejected(self):
    outcome = self.ledger_outcome()
    old = self.ledger_path.read_bytes()
    other = self.root / 'external-ledger.json'
    other.write_bytes(old)
    real = integration.atomic_replace_bytes
    def write(path, content, **kwargs):
      result = real(path, content, **kwargs)
      if path == self.deploy / 'images/asset.png':
        self.ledger_path.unlink()
        self.ledger_path.symlink_to(other)
      return result
    with patch.object(integration, 'atomic_replace_bytes', side_effect=write):
      with self.assertRaisesRegex(LocalApplyError, 'Pending ledger replacement.*symlink'):
        integration.apply_publish(outcome)
    self.assertEqual(other.read_bytes(), old)
    self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'fixed')

  def test_mode_setting_failure_preserves_old_image_and_cleans_temp(self):
    self.confirm()
    outcome = self.outcome(images=(ImagePlanEntry('UPDATE', 'images/asset.png', b'fixed'),))
    self.assert_failure(outcome, 'checker.atomic_write.os.fchmod', OSError('mode setting'))
    self.assertEqual((self.deploy / 'images/asset.png').read_bytes(), b'image')
    self.assertEqual(list((self.deploy / 'images').glob('*.tmp')), [])


if __name__ == '__main__':
  unittest.main()
