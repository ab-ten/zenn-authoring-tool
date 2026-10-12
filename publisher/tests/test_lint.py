"""隔離した filesystem と記事 repository で lint の境界を検証します。"""

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from converter import lint
from converter.git_inspection import Worktrees, git as read_git
from test_entry_guards import fixture_git, snapshot


ID = '6a5c05e8-401a-4e2b-bc92-2c9aa61be305'
OTHER_ID = '3b18f058-ec22-4a6a-9e29-3098c234b113'
FRONT = '---\ntitle: Test\nemoji: ""\ntype: tech\ntopics: []\npublished: true\n---\n'
OBSERVATION = {key: 'time' for key in (
  'body_updated_at', 'source_repo_updated_at', 'published_at')}


def source(article_id=ID, state='hold', slug=None, release=None, override=None):
  comments = f'<!-- #id:{article_id} -->\n<!-- #state:{state} -->\n'
  for key, value in (('slug', slug), ('release', release), ('observation-override', override)):
    if value is not None:
      comments += f'<!-- #{key}:{value} -->\n'
  return FRONT + comments + 'Body\n'


class LintTests(unittest.TestCase):
  def setUp(self):
    self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
    (self.root / 'articles').mkdir()
    self.identity = self.enterContext(patch.object(lint, 'identify_worktrees',
      return_value=Worktrees(self.root, self.root / '_deploy')))
    for target in ('converter.entry_guards.inspect_inputs',
                   'converter.git_inspection.inspect_status',
                   'converter.git_inspection.inspect_index_flags',
                   'converter.filesystem_inputs.load_ledger',
                   'converter.filesystem_inputs.load_released_snapshots',
                   'converter.planning.plan_articles',
                   'converter.workflow.run_convert',
                   'converter.workflow.preflight_publish',
                   'converter.workflow.apply_publish',
                   'checker.zenn_http.fetch_article', 'urllib.request.urlopen'):
      mock = self.enterContext(patch(target, side_effect=AssertionError(target)))
      self.addCleanup(mock.assert_not_called)

  def write(self, text=None, name='example.md'):
    path = self.root / 'articles' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source() if text is None else text, encoding='utf-8')
    return path

  def run_lint(self, *articles):
    before = snapshot(self.root)
    try:
      return lint.lint_sources(self.root, all_articles=not articles, articles=articles)
    finally:
      self.assertEqual(snapshot(self.root), before)

  def codes(self, text):
    self.write(text)
    return tuple(item.code for item in self.run_lint().diagnostics)

  def test_hold_without_publish_fields_and_untracked_active(self):
    self.write()
    self.write(source(OTHER_ID, 'active'), 'nested/active.md')
    result = self.run_lint()
    self.assertEqual((result.scope, result.article_count, result.error_count), ('all', 2, 0))
    with self.assertRaises(FrozenInstanceError):
      result.scope = 'selected'

  def test_parser_errors_are_article_specific_and_do_not_stop_all(self):
    for bad in ('body', source().replace('type: tech', 'type: invalid'),
                source().replace('topics: []', 'topics: [1]'),
                source().replace('title: Test', 'title: ['),
                source().replace('<!-- #state:hold -->', '<!-- #state hold -->'),
                source().replace('<!-- #state:hold -->', ''),
                source().replace('Body', '<!-- #id:duplicate -->\nBody'),
                source().replace('Body', '<!-- #unknown:value -->\nBody'),
                source().replace('Body', '<!-- #release:1'),
                source().replace('published: true', 'published_at: 2026-02-30\npublished: true'),
                source().replace('published: true', 'published_at: 2026/01/01\npublished: true'),
                source(state='active').replace('published: true', 'published: false')):
      with self.subTest(bad=bad):
        self.write(bad, 'bad.md')
        self.write(source(), 'good.md')
        result = self.run_lint()
        self.assertEqual(result.article_count, 2)
        self.assertEqual([(d.path, d.code) for d in result.diagnostics],
                         [('articles/bad.md', 'SOURCE_PARSE')])

  def test_uuid_normalization(self):
    for value in (ID.upper(), ID.replace('-', ''), '{' + ID + '}', 'urn:uuid:' + ID):
      with self.subTest(value=value):
        self.assertEqual(self.codes(source(value)), ())
    self.assertEqual(self.codes(source('bad')), ('INVALID_UUID',))

  def test_independent_errors_accumulate(self):
    result = self.codes(source('bad', 'unknown', 'short', 0, '{}'))
    self.assertEqual(result, ('INVALID_OBSERVATION_OVERRIDE', 'INVALID_RELEASE',
                              'INVALID_SLUG', 'INVALID_STATE', 'INVALID_UUID'))

  def test_optional_local_representations(self):
    for value in ('short', 'A' * 12, 'a' * 51, 'invalid/slug!'):
      with self.subTest(slug=value):
        self.assertEqual(self.codes(source(slug=value)), ('INVALID_SLUG',))
    for value in ('0',):
      self.assertEqual(self.codes(source(release=value)), ('INVALID_RELEASE',))
    for value in ('-1', '+1', '1.0', 'true', 'one'):
      with self.subTest(release=value):
        self.assertEqual(self.codes(source(release=value)), ('SOURCE_PARSE',))
    for value in ('1', '0001', '999'):
      with self.subTest(release=value):
        self.assertEqual(self.codes(source(release=value, slug='a' * 12)), ())

  def test_override_uses_checker_local_schema_without_operation_restrictions(self):
    for value in ('{}', '[]', '1', '"string"',
                  json.dumps({**OBSERVATION, 'published_at': ''}),
                  json.dumps({**OBSERVATION, 'body_updated_at': 1})):
      with self.subTest(value=value):
        self.assertEqual(self.codes(source(override=value)), ('INVALID_OBSERVATION_OVERRIDE',))
    for value in ('null', 'NaN', '{'):
      with self.subTest(value=value):
        self.assertEqual(self.codes(source(override=value)), ('SOURCE_PARSE',))
    for state in ('active', 'hold', 'erased'):
      self.assertEqual(self.codes(source(state=state, override=json.dumps(
        {**OBSERVATION, 'extra': []}))), ())

  def test_future_calendar_date_is_local_validity_only(self):
    self.assertEqual(self.codes(source(state='active').replace(
      'published: true', 'published: true\npublished_at: 2099-01-01')), ())

  def test_duplicate_normalized_ids_report_all_paths_in_order(self):
    self.write(source(ID.upper()), 'z.md')
    self.write(source(ID.replace('-', ''), slug='short'), 'nested/a.md')
    result = self.run_lint()
    self.assertEqual([d.path for d in result.diagnostics],
                     ['articles/nested/a.md', 'articles/nested/a.md', 'articles/z.md'])
    duplicates = [d for d in result.diagnostics if d.code == 'DUPLICATE_UUID']
    self.assertEqual(len(duplicates), 2)
    for diagnostic in duplicates:
      self.assertIn(ID, diagnostic.message)
      self.assertIn('articles/nested/a.md, articles/z.md', diagnostic.message)
    self.assertEqual(result, self.run_lint())

  def test_targeted_does_not_parse_unrelated_or_check_duplicates(self):
    self.write()
    self.write(source(), 'duplicate.md')
    self.write('invalid', 'broken.md')
    with patch.object(lint, 'parse_source', wraps=lint.parse_source) as parser:
      result = self.run_lint('articles/example.md', 'articles/example.md')
    self.assertEqual((result.scope, result.article_count, result.error_count), ('selected', 1, 0))
    parser.assert_called_once_with(source())
    result = self.run_lint('articles/example.md', 'articles/duplicate.md')
    self.assertEqual(result.error_count, 0)

  def test_duplicate_slugs_are_not_rejected(self):
    self.write(source(slug='same-article-slug'))
    self.write(source(OTHER_ID, slug='same-article-slug'), 'second.md')
    self.assertEqual(self.run_lint().error_count, 0)

  def test_invalid_targets_and_missing_explicit_scope(self):
    self.write()
    for value in ('/articles/example.md', 'articles/../example.md', '../articles/example.md',
                  'articles//example.md', 'articles/./example.md', 'articles/no.md',
                  'articles/example.txt', 'images/example.md', 'articles/a\\b.md',
                  'articles/a\0.md', 'articles/example.md/'):
      with self.subTest(value=value), self.assertRaises(lint.LintError):
        self.run_lint(value)
    for kwargs in ({}, {'all_articles': True, 'articles': ('articles/example.md',)}):
      with self.assertRaises(lint.LintError):
        lint.lint_sources(self.root, **kwargs)
    with self.assertRaises(lint.LintError):
      lint.lint_sources(None, all_articles=True)

  def test_symlink_file_and_parent_rejected(self):
    path = self.write()
    (self.root / 'articles/link.md').symlink_to(path)
    (self.root / 'articles/linked').symlink_to(path.parent, target_is_directory=True)
    for targets in ((), ('articles/link.md',), ('articles/linked/example.md',)):
      with self.subTest(targets=targets), self.assertRaises(lint.LintError):
        self.run_lint(*targets)
    self.assertEqual(self.run_lint('articles/example.md').error_count, 0)

  def test_non_regular_file_rejected(self):
    (self.root / 'articles/directory.md').mkdir()
    with self.assertRaises(lint.LintError):
      self.run_lint('articles/directory.md')
    os.mkfifo(self.root / 'articles/fifo.md')
    for targets in ((), ('articles/fifo.md',)):
      with self.assertRaises(lint.LintError):
        lint.lint_sources(self.root, all_articles=not targets, articles=targets)

  def test_utf8_is_diagnostic_and_read_failure_is_infrastructure_error(self):
    path = self.write()
    path.write_bytes(b'\xff')
    self.write(source(), 'valid.md')
    result = self.run_lint()
    self.assertEqual(result.article_count, 2)
    self.assertEqual([d.code for d in result.diagnostics], ['INVALID_UTF8'])
    with patch.object(Path, 'read_bytes', side_effect=PermissionError('unreadable')):
      with self.assertRaisesRegex(lint.LintError, 'unreadable'):
        lint.lint_sources(self.root, all_articles=True)

  def test_enumeration_and_root_fail_closed(self):
    with patch.object(Path, 'iterdir', side_effect=PermissionError('cannot enumerate')):
      with self.assertRaisesRegex(lint.LintError, 'cannot enumerate'):
        lint.lint_sources(self.root, all_articles=True)
    (self.root / 'articles').rmdir()
    with self.assertRaises(lint.LintError):
      lint.lint_sources(self.root, all_articles=True)
    self.identity.side_effect = ValueError('invalid repository')
    with self.assertRaisesRegex(lint.LintError, 'invalid repository'):
      lint.lint_sources(self.root, all_articles=True)

  def test_unexpected_parser_failure_is_not_suppressed(self):
    self.write()
    with patch.object(lint, 'parse_source', side_effect=RuntimeError('unexpected')):
      with self.assertRaisesRegex(RuntimeError, 'unexpected'):
        self.run_lint()


class LintRepositoryTests(unittest.TestCase):
  def test_dirty_staged_untracked_and_deploy_state_are_read_only(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary) / 'article repo'
      root.mkdir()
      fixture_git(root, 'init', '-b', 'main')
      fixture_git(root, 'config', 'worktree.useRelativePaths', 'true')
      (root / 'articles').mkdir()
      article = root / 'articles/active.md'
      article.write_text(source(state='active'))
      (root / '.gitignore').write_text('/_deploy/\n')
      fixture_git(root, 'add', '.')
      fixture_git(root, 'commit', '-m', 'fixture')
      fixture_git(root, 'worktree', 'add', '--relative-paths', '-b', 'deploy', '_deploy')
      article.write_text(source(state='active') + 'Staged\n')
      fixture_git(root, 'add', 'articles/active.md')
      article.write_text(source(state='active') + 'Unstaged\n')
      (root / 'articles/untracked.md').write_text(source(OTHER_ID))
      management = root / '_deploy/.zenn-authoring'
      management.mkdir()
      (management / 'ledger.json').write_text('invalid ledger with pending state')
      (root / '_deploy/articles/active.md').write_text('Dirty deploy')
      before = snapshot(root)
      with patch('converter.git_inspection.git', wraps=read_git) as git:
        result = lint.lint_sources(root, all_articles=True)
      self.assertEqual((result.article_count, result.error_count), (2, 0))
      self.assertEqual(snapshot(root), before)
      self.assertTrue(git.call_args_list)
      self.assertTrue(all(call.args[1] in ('rev-parse', 'symbolic-ref')
                          for call in git.call_args_list))

  def test_non_repository_and_development_repository_rejected(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      with self.assertRaises(lint.LintError):
        lint.lint_sources(root, all_articles=True)
      (root / 'docs').mkdir()
      (root / 'docs/architecture.md').touch()
      (root / 'publisher/converter').mkdir(parents=True)
      (root / 'publisher/converter/source_parser.py').touch()
      with self.assertRaisesRegex(lint.LintError, 'Development repository'):
        lint.lint_sources(root, all_articles=True)
