# Use Cases

## Scope

This document describes the user-visible workflows supported by the Zenn authoring tool.

It focuses on why and when each workflow is used, the expected user actions, observable results, acceptance criteria, and failure or guard conditions.

Detailed converter behavior, state-checker algorithms, ledger representation, publish-plan construction, remote observation rules, and recovery mechanics are defined in `architecture.md`. Domain values and lifecycle semantics are defined in `domain.md`.

## US-01 Write a new article

### Purpose

Start writing a new Zenn article using a human-readable authoring filename that does not need to match the Zenn slug.

### Initial state

No managed source article exists yet.

### Flow

1. Create a new Markdown file in the authoring worktree.
2. Put the Zenn Front Matter at the beginning of the file.
3. Put the authoring-tool management comments immediately after the Front Matter.
4. Assign an `{article-id}`.
5. Use `hold` as the normal initial `{article-state}`. `active` is also permitted when the article is already ready for publication.
6. A `{slug}` and `{release-number}` may remain absent until first-publish preparation.
7. The article may be previewed using US-02 before it is committed or published.

### Acceptance Criteria

- The source filename does not need to match the Zenn slug.
- A newly created article may be untracked or otherwise uncommitted before first publish.
- `{slug}` and `{release-number}` may be omitted until first-publish preparation.
- The deploy worktree is not changed by merely creating the source article.
- No ledger entry or released snapshot is created.
- The article can be previewed immediately after creation.

### Invalid Cases

The article is not a valid managed source when required authoring information is invalid, including cases such as:

- missing `{article-id}`;
- duplicate `{article-id}`;
- malformed Front Matter; or
- malformed management comments.

### End state

Normally the source article exists in the authoring tree with:

- an `{article-id}`;
- `state: hold`;
- no required first-publish side effects in the deploy worktree, ledger, or released snapshots.

The article is ready for continued editing and preview.

## US-02 Preview an article in progress

### Purpose

Inspect how the current authoring worktree content is rendered by Zenn without publishing it.

### Initial state

The article may be:

- `hold`;
- dirty;
- renamed within the authoring tree; or
- not yet prepared for first publish.

### Flow

1. Start the preview container.
2. Open the Zenn CLI preview.
3. Inspect the article and any referenced images.

### Acceptance Criteria

- Preview does not modify the deploy worktree.
- Preview displays the current source content directly.
- Authoring management HTML comments are not visibly rendered as article content.
- Images under the authoring `/images` tree are available through the normal `/images/...` paths.
- Preview does not change article state, release, ledger state, or released snapshots.

### Failure Cases

Preview may fail because of conditions such as:

- a broken preview symlink; or
- malformed Zenn Front Matter.

### End state

No managed state changes as a result of preview.

## US-03 Publish a new article for the first time

### Purpose

Publish a managed source article to Zenn for the first time and establish release `1` as the first successful managed release.

### Initial state

- A managed source article exists.
- The source has an `{article-id}`.
- The article is not yet published on Zenn.
- No ledger entry exists for the `{article-id}`.
- No released snapshot exists for the article.
- No confirmed deploy article exists for the article.

### Publish preparation

Before starting the publish workflow:

1. assign a `{slug}`;
2. set `state: active`;
3. set `release: 1`;
4. set the Zenn Front Matter to `published: true`;
5. commit the source article.

The deploy worktree must be clean when the publish workflow begins.

### Flow

1. Run the converter.
2. Review the deploy worktree diff.
3. Commit the deploy branch.
4. Push the deploy branch from the host.
5. Allow Zenn deployment to run.
6. Follow US-09 to confirm the result.

### Acceptance Criteria

- The authoring source is not modified by the converter or state checker.
- The deploy article is named `<slug>.md`.
- The deploy article does not contain authoring management comments.
- The source filename and deploy filename remain independent.
- A validation or preflight failure does not establish the candidate as a successful release.
- The user can inspect the deploy diff before pushing it.
- Successful `git push` alone does not make release `1` successful.
- Failed or unresolved deployment does not advance the ledger or released snapshot.
- Release `1` is established as successful only after the article result is confirmed as `SUCCESS`.

### Failure and Guard Cases

Examples include:

- missing slug;
- missing release;
- release other than `1`;
- `published` not equal to `true`;
- source article not committed and clean;
- converter or preflight failure;
- unexpected deploy diff;
- Git push failure;
- Zenn deploy failure;
- slug collision; or
- unresolved state-checker result.

For a slug collision, the failed candidate can be cleaned up and a different candidate slug used in a later workflow.

### End state on SUCCESS

- source state is `active`;
- source release is `1`;
- deploy `/articles/<slug>.md` contains the release `1` artifact;
- ledger successful release is `1`;
- released snapshot is release `1`;
- the Zenn publication exists under the selected slug.

## US-04 Revise a published article

### Purpose

Edit the source for a currently published article without immediately changing the currently successful publication.

### Initial state

- The source article exists with identity `I`.
- It is currently `active`.
- It has slug `S` and successful release `N`.
- Ledger state and released snapshot for release `N` exist.
- `deploy/articles/S.md` represents release `N`.
- Zenn currently exposes the release `N` publication.

### Flow

1. Change source state from `active` to `hold`.
2. Edit the source body and/or Front Matter.
3. Rename the source file if useful.
4. Keep slug `S`.
5. Keep release `N` while the revision remains work in progress.
6. Preview the dirty or committed working version using US-02 as needed.

### Acceptance Criteria

- Revision work normally remains in `state: hold`.
- The source may remain dirty while editing.
- Multiple authoring commits may be made while the article is on hold.
- Renaming the source file does not change article identity.
- The successful remote publication remains release `N` until another publish workflow succeeds.

### Guard Cases

Changing or removing the release number without entering an actual publish preparation is invalid.

When the revised article is prepared for publication, US-05 defines the required release change.

### End state

Normally:

- source state is `hold`;
- release remains `N`;
- source content may differ from successful release `N`;
- the article is ready for US-05.

If the revision is abandoned, the source can be restored to the successful release `N` content and returned to `active` with release `N`.

## US-05 Publish a revised article

### Purpose

Publish revised source content as the next article release and advance the successful release from `N` to `N+1`.

### Initial state

- A source article exists with identity `I`.
- Source state is `hold`.
- Slug is `S`.
- Source release is `N`.
- Ledger successful release is `N`.
- Released snapshot `N` exists.
- The artifact generated from the revised source differs from released snapshot `N`.
- Zenn currently exposes release `N`.

### Publish preparation

1. Change source state from `hold` to `active`.
2. Change release from `N` to exactly `N+1`.
3. Confirm `published: true`.
4. Commit the source article.

The deploy worktree must be clean when the publish workflow begins.

### Flow

1. Run the converter.
2. Review the deploy diff as needed.
3. Commit the deploy branch.
4. Push from the host.
5. Follow US-09 and run the one-shot state check as needed.

### Acceptance Criteria

- The candidate release is exactly `N+1`.
- The candidate artifact differs from released snapshot `N`.
- Successful push alone does not advance the successful release.
- Only `SUCCESS` advances the ledger successful release to `N+1`.
- Only `SUCCESS` replaces the released snapshot with the candidate artifact.
- `PENDING`, `COLLISION`, `UNKNOWN`, and `ERROR` do not advance the previous successful state.
- Converter and state checker do not modify the authoring source.

### Guard Cases

The workflow must reject inconsistent release or artifact combinations, including:

- modified artifact with source release still equal to `N`;
- source release greater than `N+1`;
- source release lower than `N`;
- release `N+1` while the candidate artifact still equals snapshot `N`;
- `published` not equal to `true`;
- dirty deploy worktree; or
- inconsistent ledger or released snapshot.

### End state on SUCCESS

- source state is `active`;
- source release is `N+1`;
- deploy `/articles/S.md` contains the `N+1` artifact;
- ledger successful release is `N+1`;
- released snapshot is `N+1`;
- Zenn exposes the updated article under the same slug.

## US-06 Delete an article from Zenn

### Purpose

Remove a publication from Zenn without deleting its source article or managed history, and ensure that later normal deployment does not recreate it unintentionally.

### Initial state

Normally:

- source article exists and is `active`;
- article identity is `I`;
- slug is `S`;
- release is `N`;
- ledger successful release is `N`;
- released snapshot `N` exists;
- `deploy/articles/S.md` exists;
- the user's Zenn publication `S` exists.

### Delete preparation

1. Change source state from `active` to `erased`.
2. Keep release `N`.
3. Commit the source article.

### Flow

1. Run the normal converter workflow for the delete transition.
2. Review the deploy diff, which removes `deploy/articles/S.md`.
3. Commit and push the deploy branch.
4. Delete publication `S` from the Zenn Dashboard.
5. Follow US-09 until the delete result is confirmed as `SUCCESS`.

Dashboard deletion is not ordered relative to deploy-side artifact removal. It may be performed before deployment or after a check reports that the user's publication still exists.

### Alternative Flow: Dashboard deletion first

If the user deletes the publication from the Zenn Dashboard before running the publish workflow:

1. ledger confirmed state remains `active`;
2. change source state to `erased`;
3. run the normal delete publish workflow;
4. preflight may observe the already-deleted publication and continue with a warning;
5. a later check confirms the absence of the previous own-user publication and returns `SUCCESS`.

### Acceptance Criteria

- The source article is retained.
- Release remains `N`.
- Successful release history remains `N`.
- Released snapshot `N` is retained.
- `deploy/articles/S.md` no longer exists.
- The previous own-user publication no longer exists on Zenn.
- A later normal deployment does not recreate the erased article.

### Guard Cases

Examples include:

- changing the release number as part of deletion;
- unexpected remote drift; or
- a dirty deploy worktree.

### End state on SUCCESS

- source state is `erased`;
- source retains identity, slug, and release `N`;
- ledger confirmed state is `erased`;
- successful release and released snapshot remain `N`;
- the successful Zenn observation retains the last value from before deletion;
- deploy article `S` is absent;
- the user's Zenn publication `S` is absent.

## US-07 Republish an erased article

### Purpose

Publish a previously erased managed article again while preserving its logical `{article-id}`.

### Initial state

- article identity is `I`;
- ledger state is `erased`;
- previous confirmed slug is `S_old`;
- successful release is `N`;
- released snapshot `N` exists;
- no deploy article for `S_old` exists;
- the user's publication at `S_old` no longer exists.

### Revision patterns

Two normal patterns are supported:

- **No content change:** change source state from `erased` directly to `active`.
- **Content change:** move from `erased` to `hold`, revise the article, then change to `active`.

### Publish preparation

- state is `active`;
- `published: true`;
- if the generated artifact equals snapshot `N`, release remains `N`;
- if the artifact differs from snapshot `N`, release becomes `N+1`;
- commit the source.

### Slug behavior

- If the old slug is still available, the old slug is used.
- A different slug is allowed only when the old slug has been reused by another user.
- A candidate slug collision may require another slug before retrying.

### Flow

1. Run the converter and RECREATE preflight.
2. Review the diff as needed.
3. Commit and push the deploy branch.
4. Follow US-09.

### Acceptance Criteria

- The same `{article-id}` is retained.
- The old slug is reused when still available.
- A new slug is accepted only when the previous slug has been taken by another user.
- Unchanged artifact keeps release `N`.
- Changed artifact uses release `N+1`.
- Push alone does not make the ledger active.
- Ledger state becomes `active` only after `SUCCESS`.

### Guard Cases

Examples include:

- artifact changed while release remains `N`;
- artifact unchanged while release is set to `N+1`; or
- dirty deploy worktree.

### End state on SUCCESS

- source state is `active`;
- article identity remains `I`;
- current slug is the successfully used slug;
- successful release is `N` or `N+1` according to artifact equality;
- deploy article exists under the current slug;
- ledger state is `active`;
- successful Zenn observation describes the republished own-user publication.

## US-08 Add or change images

### Purpose

Add, change, or explicitly remove assets under authoring `/images` so that preview and Zenn deployment use the same article image paths, without introducing article-style lifecycle management for images.

### Initial state

- authoring `/images` exists;
- preview exposes it through `/preview/images`;
- deploy `/images` is the Zenn deployment mirror;
- image state is independent from article state, release, ledger entries, and released snapshots.

### Editing and preview

1. Add, replace, or explicitly remove images under authoring `/images`.
2. Reference repository images from Markdown using `/images/...`.
3. Preview uses the authoring image tree directly.
4. Dirty or untracked images are permitted while editing and previewing.

### Deploy preparation and flow

Before deployment, authoring `/images` changes must be committed and clean.

Then:

1. run the converter;
2. review the deploy diff;
3. do not push if the diff contains unintended image additions, changes, or removals;
4. commit and push the deploy branch;
5. follow US-09 to inspect deployment results.

### Acceptance Criteria

- New images, replacements, and explicit deletions use the same mirror workflow.
- Preview and deployed articles use the same `/images/...` reference path.
- No preview-only image copies are required.
- Authoring `/images` is authoritative.
- Deploy `/images` mirrors authoring `/images`.
- Unused images are not automatically removed.
- Images referenced only by erased articles are not automatically removed.
- Replacing image bytes at the same path does not increment article release.
- Image-only changes do not change article ledger state or released snapshots.
- Changing an image reference in article Markdown is an article-artifact change and follows the normal article release rules.
- Any asset placed under authoring `/images` must be safe to synchronize to Zenn.
- Private, secret, or otherwise non-deployable assets must not be stored there.

### Failure Cases

Examples include:

- dirty or untracked authoring `/images` at deployment time;
- unintended image deletion in the deploy diff;
- invalid image reference paths;
- unsupported image extension;
- image larger than the supported Zenn limit;
- deleting an image still referenced by a published article;
- Zenn-side image deployment failure.

No fixed automatic recovery is defined for image deployment errors. The user corrects the authoritative authoring inputs according to the failure and runs the normal workflow again.

### End state on SUCCESS

- authoring `/images` contains the intended asset set;
- deploy `/images` is synchronized to it;
- article release changes only when the Markdown artifact changes;
- image-only changes leave article release, ledger, and released snapshots unchanged.

## US-09 Confirm deployment results

### Purpose

After pushing the deploy branch, determine what actually became effective on Zenn and advance management state only for article operations whose semantic result is confirmed successful.

Article result confirmation and image deployment confirmation are separate.

### Initial state

- the target deploy commit has been pushed;
- Zenn deployment is running, queued, or has completed;
- confirmed ledger state and released snapshots still represent the previous successful state;
- article candidates, when present, are not yet successful;
- no separate unresolved deployment transaction should be introduced.

### Success model

For article changes:

- Zenn Dashboard deploy success alone is not article semantic `SUCCESS`;
- state checker `check` determines article semantic result.

For image changes:

- image deployment is confirmed from the Zenn Dashboard deployment result;
- article API success is not proof of image deployment success.

For mixed article-and-image deployment:

- article and image results are evaluated independently;
- image `ERROR` does not negate an article `SUCCESS`;
- article `SUCCESS` does not imply image `SUCCESS`.

### Flow for article changes

Run state checker `check` as a one-shot operation.

Possible results are:

- `SUCCESS`: expected article state is confirmed and confirmed management state is advanced;
- `PENDING`: previous successful state remains and the candidate stays unresolved;
- `COLLISION`: the failed CREATE/RECREATE candidate is cleaned up without advancing confirmed state;
- `UNKNOWN` or `ERROR`: previous successful state remains and the candidate remains unresolved.

If `check` returns `PENDING`, wait as appropriate and run `check` again. Do not trigger another deployment in the meantime.

If the check reaches `COMMIT_READY`, review the management or cleanup diff and commit/push it with `[skip ci]`.

### Flow for image changes

Inspect the relevant Zenn Dashboard deployment entry:

- deployment success -> image deployment `SUCCESS`;
- deployment still queued or running -> `PENDING`;
- deployment failure -> image deployment `ERROR`.

### Acceptance Criteria

- The checker is one-shot and does not long-poll.
- Only article `SUCCESS` advances confirmed article management state.
- `PENDING`, `COLLISION`, `UNKNOWN`, and `ERROR` do not advance article successful state.
- Successful `git push` is not treated as Zenn deployment success.
- Article API success is not treated as proof of image deployment success.
- Image-only deployment does not change article release, ledger state, or released snapshots.
- While an unresolved article deployment exists, a new deploy-triggering commit is not pushed.
- Management commits after article result handling use `[skip ci]`.
- Converter and checker do not modify the authoring source.

### Failure and Recovery Cases

- `PENDING`: preserve previous successful state and rerun the one-shot check later.
- `COLLISION`: preserve confirmed state, commit the cleanup when ready, then return to slug resolution.
- `UNKNOWN` / `ERROR`: preserve pending and successful state, inspect the cause, and use check, reconcile, or `discard-all-pendings` as appropriate.
- Image `ERROR`: diagnose using the Zenn Dashboard and correct the authoring inputs; article semantic state remains independent.

### End states

**Article SUCCESS**

- expected remote article state has been confirmed;
- confirmed management state has been updated according to the operation;
- management changes are committed and pushed with `[skip ci]`.

**Image-only SUCCESS**

- Zenn deployment success has been confirmed;
- article release, ledger, and released snapshots are unchanged.

**PENDING / UNKNOWN / ERROR**

- previous successful ledger and snapshot remain;
- unresolved pending state remains;
- no new deployment is started until the issue is resolved.

**COLLISION**

- confirmed ledger and released snapshot remain unchanged;
- failed candidate cleanup is complete;
- the corresponding pending entry has been removed;
- after cleanup management commit, slug resolution can proceed.

## US-10 Adopt an emergency Zenn-side edit

### Purpose

Reconcile an intentional emergency edit made directly through the Zenn Web UI or another out-of-band mechanism when the normal authoring workflow was unavailable.

The already-established remote article change is adopted as a new successful release.

### Initial state

- the target article has confirmed state `active`;
- successful release is `N`;
- the user's Zenn publication differs from released snapshot `N`;
- no `pending_next_state` exists;
- the user has confirmed that the remote change is intentional.

### Flow

1. Obtain the current remote article content.
   - Individual Markdown output or content export may be used.
   - If export is insufficient, use Web UI inspection and `git diff` to reconcile manually.
2. Apply only the intended remote changes to the authoring source.
3. Set source release to `N+1`.
4. Commit the source and make it clean.
5. Run `state checker import --adopt-remote`.
6. Run `state checker check`.
7. If the result is `COMMIT_READY`, review the deploy management changes.
8. Commit and push those management changes with `[skip ci]`.

### Acceptance Criteria

- Intentional Web UI or other remote edits can be brought back under authoring management.
- A real remote article-content change is not treated merely as observation metadata drift.
- The adopted article artifact becomes a new release.
- Released snapshot becomes the adopted article artifact.
- Successful Zenn observation becomes the current remote observation.
- Slug and article identity remain unchanged.
- No `pending_next_state` is generated.
- Confirmed ledger, released snapshot, and deploy artifact are consistent after adoption.
- The management commit does not trigger another deployment.

### Failure Cases

Adoption must not proceed when, for example:

- `pending_next_state` exists;
- the remote article belongs to another user;
- source slug differs from the confirmed ledger slug;
- source release is not `N+1`;
- the user has not sufficiently reconciled the remote content;
- an unexpected remote change is discovered.

Unexpected remote content must be reconciled manually rather than automatically adopted.

### Unverified behavior

The following remain intentionally unverified and may require changes to the reconcile procedure after practical testing:

- whether Zenn individual Markdown output or content export can fully reconstruct the authoring source;
- differences between Web UI edits and exported Markdown;
- whether all Front Matter fields survive as expected;
- handling of images or image references introduced from the Web UI.

### End state on SUCCESS

- authoring source is semantically aligned with the adopted Zenn article;
- source release and ledger successful release are both `N+1`;
- released snapshot is the adopted article artifact;
- successful Zenn observation is the current remote observation;
- deploy `/articles/<slug>.md` equals the released snapshot;
- `pending_next_state` is empty;
- deploy worktree is clean after the management commit.
