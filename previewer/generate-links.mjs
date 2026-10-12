/** Build a read-only symlink view for Zenn preview; never modify authoring files. */
import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SLUG = /^[a-z0-9_-]{12,50}$/;
const META_KEY = /^<!--\s*#([A-Za-z][\w-]*)/;
const PREVIEW_VALUE = /^<!--\s*#(id|slug):([\s\S]*?)\s*-->$/;
const DECODER = new TextDecoder('utf-8', { fatal: true });

/** Normalize the UUID notation accepted by Python uuid.UUID to lowercase hyphenated form. */
export function normalizeArticleId(value) {
  if (typeof value !== 'string') throw new Error('Article ID must be a string');
  let hex = value.trim().replace(/^urn:uuid:/i, '').replace(/^\{(.*)\}$/, '$1');
  hex = hex.replaceAll('-', '');
  if (!/^[0-9a-fA-F]{32}$/.test(hex)) throw new Error(`Invalid article UUID: ${value}`);
  hex = hex.toLowerCase();
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * Extract #id and #slug from the leading management-comment region only.
 * Other management fields are ignored, including unknown future fields. Return null
 * when no #id is found. This does not validate publish eligibility or Front Matter;
 * invalid/duplicate #id or #slug values are rejected to protect preview link names.
 */
export function parsePreviewMetadata(markdown) {
  const opening = /^---(?:\r\n|\n)/.exec(markdown);
  if (!opening) return null;
  const contentStart = opening[0].length;
  const closing = /^---(?:\r\n|\n|$)/gm;
  closing.lastIndex = contentStart;
  const end = closing.exec(markdown);
  if (!end) return null;

  const values = new Map();
  let offset = closing.lastIndex;
  while (offset < markdown.length) {
    const space = /^[ \t\r\n]*/.exec(markdown.slice(offset))[0];
    const start = offset + space.length;
    const rest = markdown.slice(start);
    const key = META_KEY.exec(rest)?.[1];
    if (!key) break;
    const relevant = key === 'id' || key === 'slug';
    const closingComment = markdown.indexOf('-->', start + 4);
    if (closingComment < 0) {
      if (relevant) throw new Error(`Unterminated preview metadata comment: ${key}`);
      break;
    }
    if (relevant) {
      const comment = markdown.slice(start, closingComment + 3);
      const match = PREVIEW_VALUE.exec(comment);
      if (!match) throw new Error(`Invalid preview metadata comment: ${key}`);
      const value = match[2].trim();
      if (!value) throw new Error(`Empty preview metadata comment: ${key}`);
      if (values.has(key)) throw new Error(`Duplicate preview metadata comment: ${key}`);
      values.set(key, value);
    }
    offset = closingComment + 3;
  }
  if (!values.has('id')) return null;
  const articleId = normalizeArticleId(values.get('id'));
  const slug = values.get('slug') ?? `new-${articleId}`;
  if (!SLUG.test(slug)) throw new Error(`Invalid preview slug: ${slug}`);
  return { articleId, slug };
}

async function requireDirectory(root, label) {
  const info = await fs.lstat(root);
  if (!info.isDirectory() || info.isSymbolicLink())
    throw new Error(`${label} must be a real directory: ${root}`);
}

async function listArticles(directory) {
  const files = [];
  async function walk(dir) {
    const entries = await fs.readdir(dir, { withFileTypes: true });
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name, 'en'))) {
      const location = path.join(dir, entry.name);
      if (entry.isDirectory()) await walk(location);
      else if (entry.isFile()) {
        if (entry.name.endsWith('.md')) files.push(location);
      } else {
        throw new Error(`Unsafe article filesystem entry: ${location}`);
      }
    }
  }
  await walk(directory);
  return files;
}

/**
 * Build the preview-only link plan before touching /preview.
 * Checks file safety, #id/#slug values and collisions, not publisher metadata validity.
 * Returns filename/source pairs; duplicate normalized IDs and all slug collisions are errors.
 */
export async function planPreviewLinks(authoringRoot) {
  const sourceArticles = path.join(authoringRoot, 'articles');
  const sourceImages = path.join(authoringRoot, 'images');
  await requireDirectory(authoringRoot, 'Authoring root');
  await requireDirectory(sourceArticles, 'Authoring articles');
  await requireDirectory(sourceImages, 'Authoring images');

  const destinations = new Map();
  const identities = new Map();
  for (const file of await listArticles(sourceArticles)) {
    let info;
    try {
      const markdown = DECODER.decode(await fs.readFile(file));
      info = parsePreviewMetadata(markdown);
    } catch (error) {
      throw new Error(`Invalid source ${file}: ${error.message}`, { cause: error });
    }
    if (info === null) continue;
    if (identities.has(info.articleId)) {
      throw new Error(`Duplicate normalized article ID ${info.articleId}: ${identities.get(info.articleId)} and ${file}`);
    }
    const previous = destinations.get(info.slug);
    if (previous) {
      throw new Error(`Preview slug collision ${info.slug}: ${previous} and ${file}`);
    }
    identities.set(info.articleId, file);
    destinations.set(info.slug, file);
  }
  return { sourceImages, destinations };
}

async function checkExistingPreview(previewRoot, sourceArticles, sourceImages) {
  await requireDirectory(previewRoot, 'Preview root');
  const articles = path.join(previewRoot, 'articles');
  try {
    await requireDirectory(articles, 'Existing preview articles');
    for (const entry of await fs.readdir(articles, { withFileTypes: true })) {
      const p = path.join(articles, entry.name);
      if (!entry.isSymbolicLink() || !SLUG.test(entry.name.replace(/\.md$/, '')) || !entry.name.endsWith('.md'))
        throw new Error(`Refusing to remove unmanaged preview entry: ${p}`);
      const target = await fs.readlink(p);
      const rel = path.relative(sourceArticles, target);
      if (!path.isAbsolute(target) || !rel || rel.startsWith('..' + path.sep) || rel === '..' || path.isAbsolute(rel))
        throw new Error(`Refusing unexpected preview link target: ${p} -> ${target}`);
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const images = path.join(previewRoot, 'images');
  try {
    const st = await fs.lstat(images);
    if (!st.isSymbolicLink() || (await fs.readlink(images)) !== sourceImages)
      throw new Error(`Refusing to replace unmanaged preview images entry: ${images}`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
}

/**
 * Replace the generated article link directory at preview startup.
 * On any validation error, leave old generated links untouched and do not launch Zenn.
 * This function is for a stopped preview server; concurrent writers are unsupported.
 */
export async function generateLinks(authoringRoot, previewRoot) {
  if (!path.isAbsolute(authoringRoot) || !path.isAbsolute(previewRoot))
    throw new Error('Explicit absolute authoring and preview roots are required');
  const { sourceImages, destinations } = await planPreviewLinks(authoringRoot);
  const sourceArticles = path.join(authoringRoot, 'articles');
  await checkExistingPreview(previewRoot, sourceArticles, sourceImages);
  const current = path.join(previewRoot, 'articles');
  const stage = await fs.mkdtemp(path.join(previewRoot, '.articles-stage-'));
  const backup = path.join(previewRoot, `.articles-backup-${randomUUID()}`);
  let oldMoved = false;
  let installed = false;
  try {
    for (const [slug, source] of destinations) {
      await fs.symlink(source, path.join(stage, `${slug}.md`), 'file');
    }
    try {
      await fs.lstat(path.join(previewRoot, 'images'));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      await fs.symlink(sourceImages, path.join(previewRoot, 'images'), 'dir');
    }
    try {
      await fs.rename(current, backup);
      oldMoved = true;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await fs.rename(stage, current);
    installed = true;
  } catch (error) {
    if (oldMoved && !installed) {
      try { await fs.rename(backup, current); }
      catch (restoreError) {
        throw new Error(`Preview swap failed: ${error.message}; failed to restore prior links: ${restoreError.message}`);
      }
    }
    throw error;
  } finally {
    if (!installed) await fs.rm(stage, { recursive: true, force: true });
  }
  if (oldMoved) await fs.rm(backup, { recursive: true, force: true });
  return destinations.size;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  const authoringRoot = process.env.AUTHORING_ROOT ?? '/authoring';
  const previewRoot = process.env.PREVIEW_ROOT ?? '/preview';
  try {
    const count = await generateLinks(authoringRoot, previewRoot);
    const articles = path.join(previewRoot, 'articles');
    for (const name of (await fs.readdir(articles)).sort()) {
      const link = path.join(articles, name);
      console.log(`PREVIEW_LINK: ${link} -> ${await fs.readlink(link)}`);
    }
    const imagesLink = path.join(previewRoot, 'images');
    console.log(`PREVIEW_LINK: ${imagesLink} -> ${await fs.readlink(imagesLink)}`);
    console.log(`PREVIEW_LINKS_READY: ${count} articles`);
  } catch (error) {
    console.error(`PREVIEW_LINKS_ERROR: ${error.message}`);
    process.exitCode = 1;
  }
}
