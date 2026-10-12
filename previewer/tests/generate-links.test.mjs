import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  normalizeArticleId, parsePreviewMetadata, planPreviewLinks, generateLinks,
} from '../generate-links.mjs';

const ID1 = '6a5c05e8-401a-4e2b-bc92-2c9aa61be305';
const ID2 = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const FRONT = '---\ntitle: Example\nemoji: "🧪"\ntype: tech\ntopics: [nodejs]\npublished: false\n---\n';

let workspace, authoring, preview;

function markdown(id, slug, body='Body') {
  return FRONT + `<!-- #id:${id} -->\n<!-- #state:hold -->\n` +
    (slug === undefined ? '' : `<!-- #slug:${slug} -->\n`) + body;
}

async function article(name, content) {
  const target = path.join(authoring, 'articles', name);
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, content);
  return target;
}

async function generatedNames() {
  return (await fs.readdir(path.join(preview, 'articles'))).sort();
}

beforeEach(async () => {
  workspace = await fs.mkdtemp(path.join(os.tmpdir(), 'previewer-test-'));
  authoring = path.join(workspace, 'source');
  preview = path.join(workspace, 'preview');
  await fs.mkdir(path.join(authoring, 'articles'), { recursive: true });
  await fs.mkdir(path.join(authoring, 'images'));
  await fs.mkdir(path.join(preview, 'articles'), { recursive: true });
  await fs.mkdir(path.join(preview, 'books'));
});

afterEach(async () => {
  await fs.rm(workspace, { recursive: true, force: true });
});

test('management metadata: normalized UUID, synthetic slug and comments only in leading block', () => {
  assert.equal(normalizeArticleId(ID1.toUpperCase().replaceAll('-', '')), ID1);
  assert.equal(normalizeArticleId(`{${ID1}}`), ID1);
  assert.deepEqual(parsePreviewMetadata(markdown(ID1.toUpperCase(), undefined)), {
    articleId: ID1, slug: `new-${ID1}`,
  });
  const inline = FRONT + ` <!-- #id:${ID1} --> <!-- #state:hold --> <!-- #slug:valid-preview-slug -->\n`;
  assert.equal(parsePreviewMetadata(inline).slug, 'valid-preview-slug');
  const multi = FRONT + `<!--\n #id:${ID1}\n-->\n<!-- #state:hold -->\n`;
  assert.equal(parsePreviewMetadata(multi).slug, `new-${ID1}`);
  assert.equal(parsePreviewMetadata(FRONT + `Body\n<!-- #id:${ID1} -->`), null);
  assert.equal(parsePreviewMetadata(FRONT + 'Body only'), null);
});

test('normal slug, synthetic slug, nested sources, images link, and no destructive source changes', async () => {
  const first = await article('drafts/test-article.md', markdown(ID1, 'valid-preview-slug'));
  const second = await article('nested/my-draft.md', markdown(ID2, undefined));
  await article('unmanaged.md', FRONT + 'No management comments');
  await fs.writeFile(path.join(authoring, 'images', 'picture.png'), Buffer.from([1, 2, 3]));
  const before = await Promise.all([fs.readFile(first), fs.readFile(second)]);
  assert.equal(await generateLinks(authoring, preview), 2);
  assert.deepEqual(await generatedNames(), [`new-${ID2}.md`, 'valid-preview-slug.md']);
  assert.equal(await fs.readlink(path.join(preview, 'articles', 'valid-preview-slug.md')), first);
  assert.equal(await fs.readlink(path.join(preview, 'articles', `new-${ID2}.md`)), second);
  assert.equal(await fs.readlink(path.join(preview, 'images')), path.join(authoring, 'images'));
  assert.deepEqual(await Promise.all([fs.readFile(first), fs.readFile(second)]), before);
  assert.deepEqual(await fs.readFile(path.join(preview, 'images', 'picture.png')), Buffer.from([1,2,3]));
  assert.deepEqual((await fs.readdir(preview)).sort(), ['articles', 'books', 'images']);
});

test('repeat generation replaces stale links on slug changes and source moves', async () => {
  const first = await article('a.md', markdown(ID1, 'valid-preview-slug'));
  await generateLinks(authoring, preview);
  const moved = path.join(authoring, 'articles', 'sub', 'a.md');
  await fs.mkdir(path.dirname(moved), { recursive: true });
  await fs.rename(first, moved);
  await fs.writeFile(moved, markdown(ID1, 'changed-preview-slug'));
  await generateLinks(authoring, preview);
  assert.deepEqual(await generatedNames(), ['changed-preview-slug.md']);
  assert.equal(await fs.readlink(path.join(preview, 'articles', 'changed-preview-slug.md')), moved);
  assert.deepEqual((await fs.readdir(preview)).sort(), ['articles', 'books', 'images']);
});

test('duplicate explicit slug, duplicate normalized ID, synthetic/explicit collision all reject', async () => {
  for (const kind of ['slug', 'id', 'synthetic']) {
    const first = await article('first.md', markdown(ID1, kind === 'synthetic' ? undefined : 'valid-preview-slug'));
    const second = await article('second.md', markdown(
      kind === 'id' ? ID1.toUpperCase() : ID2,
      kind === 'synthetic' ? `new-${ID1}` : kind === 'slug' ? 'valid-preview-slug' : 'another-valid-slug'));
    await assert.rejects(generateLinks(authoring, preview), /Duplicate normalized article ID|Preview slug collision/);
    assert.deepEqual(await generatedNames(), []);
    await fs.unlink(first);
    await fs.unlink(second);
  }
});

test('after successful generation, collisions do not remove previous links', async () => {
  await article('a.md', markdown(ID1, 'valid-preview-slug'));
  await generateLinks(authoring, preview);
  await article('b.md', markdown(ID2, 'valid-preview-slug'));
  await assert.rejects(generateLinks(authoring, preview), /Preview slug collision/);
  assert.deepEqual(await generatedNames(), ['valid-preview-slug.md']);
});

test('invalid UUID, slug, state, and malformed/duplicate management comments reject', async () => {
  const samples = [
    markdown('not-a-uuid', undefined),
    markdown(ID1, '../traversal'),
    markdown(ID1, 'short'),
    FRONT + `<!-- #id:${ID1} -->\nBody`,
    FRONT + `<!-- #id:${ID1} -->\n<!-- #state:hold -->\n<!-- #slug:duplicate-slug -->\n<!-- #slug:duplicate-slug -->`,
    FRONT + `<!-- #id:${ID1} -->\n<!-- #state:hold -->\n<!-- #unrecognized:x -->`,
    FRONT + `<!-- #id:${ID1} -->\n<!-- #state:hold -->\n<!-- #slug: -->`,
  ];
  for (const content of samples) {
    const file = await article('broken.md', content);
    await assert.rejects(planPreviewLinks(authoring));
    await fs.unlink(file);
  }
});

test('unsafe source filesystem entries and missing images root reject', async () => {
  const outside = path.join(workspace, 'outside.md');
  await fs.writeFile(outside, markdown(ID1, 'valid-preview-slug'));
  await fs.symlink(outside, path.join(authoring, 'articles', 'link.md'));
  await assert.rejects(generateLinks(authoring, preview), /Unsafe article filesystem entry/);
  await fs.unlink(path.join(authoring, 'articles', 'link.md'));
  await fs.rm(path.join(authoring, 'images'), { recursive: true });
  await assert.rejects(generateLinks(authoring, preview), /ENOENT/);
});

test('preexisting unmanaged preview paths are never overwritten', async () => {
  await article('a.md', markdown(ID1, 'valid-preview-slug'));
  const other = path.join(preview, 'articles', 'notes.txt');
  await fs.writeFile(other, 'DO NOT DELETE');
  await assert.rejects(generateLinks(authoring, preview), /unmanaged preview entry/);
  assert.equal(await fs.readFile(other, 'utf8'), 'DO NOT DELETE');
  await fs.unlink(other);
  await fs.mkdir(path.join(preview, 'images'));
  await assert.rejects(generateLinks(authoring, preview), /unmanaged preview images entry/);
});

test('preview root and article root cannot be symlinks', async () => {
  await article('a.md', markdown(ID1, undefined));
  await fs.rename(path.join(preview, 'articles'), path.join(preview, 'old-articles'));
  await fs.symlink(path.join(preview, 'old-articles'), path.join(preview, 'articles'), 'dir');
  await assert.rejects(generateLinks(authoring, preview), /real directory/);
});


test('entrypoint generator exits nonzero on collision before any preview changes', async () => {
  await article('first.md', markdown(ID1, 'valid-preview-slug'));
  await article('second.md', markdown(ID2, 'valid-preview-slug'));
  const program = fileURLToPath(new URL('../generate-links.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [program], {
    env: { ...process.env, AUTHORING_ROOT: authoring, PREVIEW_ROOT: preview },
    encoding: 'utf8',
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /PREVIEW_LINKS_ERROR: Preview slug collision/);
  assert.deepEqual(await generatedNames(), []);
});

test('relative roots are refused to prevent CWD fallback', async () => {
  await assert.rejects(generateLinks('.', preview), /Explicit absolute/);
  await assert.rejects(generateLinks(authoring, 'preview'), /Explicit absolute/);
});
