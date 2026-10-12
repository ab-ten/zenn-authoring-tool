# Previewer — symlink entrypoint MVP

The image runs Node.js 24.21.0 and Zenn CLI 0.5.6, separate from the Python `publisher` image.

## Build and isolated Node tests

From the **development repository root**:

```sh
make build-previewer
docker run --rm --entrypoint node zenn-authoring-previewer --test /opt/previewer/tests/generate-links.test.mjs
```

The tests create disposable directories under the container's `/tmp`. The publisher's `make test` and `make build` commands are unaffected.

## Starting the preview

Prepare an **authoring repository** with `articles/` and `images/` directories (`images/` may be empty). Do not mount the development repository, the deploy worktree, or `_deploy` as `/authoring`.

From Windows PowerShell or cmd.exe, replace `C:/path/to/my-article-repo` with the **absolute path** of that authoring repository:

```sh
docker run --init --rm -it --publish 127.0.0.1:8000:8000 --mount "type=bind,source=C:/path/to/my-article-repo,target=/authoring,readonly" zenn-authoring-previewer
```

Open http://localhost:8000 . Use Ctrl-C to exit; `--init` is necessary for reliable interactive signal handling. Use a unique local port if 8000 is already in use.

For named-container restart tests, **omit** `--rm`:

```sh
docker run --init -d --name zenn-preview --publish 127.0.0.1:8000:8000 --mount "type=bind,source=C:/path/to/my-article-repo,target=/authoring,readonly" zenn-authoring-previewer
docker logs zenn-preview
docker restart zenn-preview
docker stop zenn-preview
docker rm zenn-preview
```

A container restart re-runs the entrypoint and rebuilds the symlink set from the current authoring tree. The source is read-only, the generated `/preview` view is writable, and Zenn runs with `--no-watch`. Refresh the browser after content edits or restart the preview container after an article is added, deleted, renamed, or changes slug. Image reload requires a browser refresh. Windows bind-mount inotify is **not** relied upon.

## Link-generation contract

At startup:

1. Scan `/authoring/articles` recursively for regular `.md` files. Symlinks and special filesystem objects anywhere under `articles` are rejected.
2. Read only the management comment block immediately following Zenn Front Matter. Ignore `.md` files without an `#id` in that block; do not inspect management-looking text in the article body.
3. Validate and normalize the UUID. For each managed Markdown file, map `#slug` to `/preview/articles/<slug>.md`, or generate `new-<normalized-uuid>` when no slug is declared. Existing `#state` must be active, erased, or hold. Duplicate IDs, duplicate preview slugs, and malformed managed metadata are errors.
4. Finish all source validation **before** switching the generated `/preview/articles` link directory. Never delete arbitrary user files from `/preview/articles`; pre-existing unmanaged objects cause an error.
5. Ensure `/preview/images` is a symlink to `/authoring/images` (the source images directory must exist, even if empty).
6. Only after link generation succeeds, launch `zenn preview --host 0.0.0.0 --no-watch`.

A failed validation exits nonzero without launching Zenn CLI. The source articles and images, deploy tree, ledger, and released snapshots are never written to by link generation. Only `/preview` is modified.

The metadata extractor is deliberately smaller than Converter's `source_parser.py`. It **does not** validate YAML content, release progression, observation overrides, or publish eligibility. Zenn CLI performs its own Front Matter parsing; Converter owns publication-specific validation. Keep the preview extractor's management-block grammar and UUID/slug interpretation aligned with the frozen specification and Converter tests.

**MVP limits:** Single preview writer; no file watcher/polling; no concurrent source tree rename during link generation. This packaging does not implement Windows/Docker Desktop end-to-end testing, image-only publishing, or preview lifecycle automation beyond re-running the entrypoint at startup.
