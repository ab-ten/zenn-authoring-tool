# Use Cases

## Scope

This document describes the user-visible workflows supported by the Zenn authoring tool.

It focuses on why and when each workflow is used, the expected user actions, observable results, acceptance criteria, and failure or guard conditions.

Detailed converter behavior, state-checker algorithms, ledger representation, publish-plan construction, remote observation rules, and recovery mechanics are defined in `architecture.md`. Domain values and lifecycle semantics are defined in `domain.md`.

## US-00 Initialize a Zenn authoring repository

### Purpose

Create the authoring/deploy worktree structure, establish the repository's Zenn account identity, initialize empty management state, and leave the repository ready for the normal publish workflow.

### Initial state

- host Git is version 2.48 or later;
- the target authoring directory exists;
- the Zenn username to manage is known;
- required GitHub/Zenn repository-integration information is available.

### Flow

1. Prepare the authoring Git repository on the host.

   If the directory is not yet a Git repository:

   `git init -b <authoring-branch>`

   If it is already a Git repository, use the existing authoring branch.

2. On the authoring branch:
   - add `/_deploy/` to `.gitignore`;
   - create and commit any required initial authoring files;
   - make the authoring worktree clean.

3. Configure relative worktree links:

   `git config worktree.useRelativePaths true`

4. Create the deploy linked worktree:

   `git worktree add --orphan -b deploy _deploy`

5. Prepare:

   `/_deploy/.zenn-authoring/`

6. Run:

   `state checker init <zenn-username>`

7. The checker:
   - resolves the username through the Zenn user API;
   - records the numeric `{zenn-user-id}`;
   - creates a canonical empty `ledger.json`;
   - creates `config.json`.

8. Review the deploy-worktree diff.

9. Commit the initial management files using `[skip ci]`.

10. Push the deploy branch.

### Acceptance Criteria

- `/_deploy` is a linked worktree for the `deploy` branch;
- worktree links use relative paths;
- the authoring worktree is clean;
- the deploy worktree is clean after the initialization commit;
- `/.zenn-authoring/config.json` is Git-managed;
- `/.zenn-authoring/ledger.json` is Git-managed;
- ledger `articles` is empty;
- ledger `pending_next_state` is empty;
- `{zenn-user-id}` has been resolved and stored;
- own-user publication checks use the configured numeric `{zenn-user-id}`;
- normal converter / preflight / check workflows may begin.

### Failure and Recovery Cases

- Zenn username does not resolve:
  - initialization fails;
  - config is not created.
- user API response is invalid:
  - initialization fails;
  - config is not created.
- deploy worktree contains unrelated dirty changes:
  - initialization fails before modifying management state.
- initialization stops after creation of the empty ledger but before config creation:
  - rerunning `init` may resume when the empty ledger is the only init-produced dirty change.
- config is absent but a non-empty ledger exists:
  - initialization fails;
  - repository state must be inspected.
- config exists but ledger is missing:
  - initialization fails;
  - ledger is not regenerated automatically;
  - restore from Git is required.
- config or ledger is invalid after initialization:
  - initialization does not overwrite it automatically;
  - restore or inspect the Git-managed state.
- the repository is already initialized with the same `{zenn-user-id}`:
  - report already initialized / no-op.
- the repository is initialized with a different `{zenn-user-id}`:
  - initialization fails;
  - automatic account replacement is not supported in the MVP.

### End state

- authoring and deploy worktrees are established;
- the deploy branch contains the initial management state;
- configured `{zenn-user-id}` is the canonical own-user identity;
- `ledger.json` is in canonical empty state;
- both worktrees are clean;
- the repository is ready for normal authoring and publish workflows.

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
- The `{article-id}` must be unique among managed source articles.

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
- duplicate `{article-id}`;
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
- It has slug `S` and source release `N`.
- The ledger records article identity `I`, slug `S`, and successful release `N`.
- Released snapshot `N` exists.
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
- A normal `check` result of `PENDING`, `COLLISION`, `UNKNOWN`, or `ERROR` by itself does not advance the confirmed successful state.
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
2. if local image validation fails, stop without modifying the deploy worktree;
3. review the deploy diff;
4. do not push if the diff contains unintended image additions, changes, or removals;
5. commit and push the deploy branch;
6. follow US-09 to inspect deployment results.

### Acceptance Criteria

- New images, replacements, and explicit deletions use the same mirror workflow.
- Preview and deployed articles use the same `/images/...` reference path.
- No preview-only image copies are required.
- Authoring `/images` is authoritative.
- Deploy `/images` mirrors authoring `/images`.
- Unused images are allowed and are not automatically removed.
- Images referenced only by erased articles are not automatically removed.
- Replacing image bytes at the same path does not increment article release.
- Image-only changes do not change article ledger state or released snapshots.
- Changing an image reference in article Markdown is an article-artifact change and follows the normal article release rules.
- Any asset placed under authoring `/images` must be safe to synchronize to Zenn.
- Private, secret, or otherwise non-deployable assets must not be stored there.
- Conversion fails before deploy-worktree modification if any mirror input has an unsupported extension, exceeds the Phase 0 local size limit, is not a normal Git blob, or is a filesystem special file.
- Lack of a Markdown reference does not by itself make an image invalid.
- Phase 0 does not require the converter to detect missing image references, invalid relative image references, or deletion of an image still referenced by an article.

### Failure Cases

Examples include:

- dirty or untracked authoring `/images` at deployment time;
- unintended image deletion in the deploy diff;
- an image mirror input that is a Git symlink or gitlink;
- a filesystem special file under the image mirror input;
- an unsupported or non-lowercase image extension;
- an image larger than the `3_000_000`-byte local limit;
- an invalid Markdown image reference path;
- deleting an image still referenced by a published article;
- Zenn-side image deployment failure.

Invalid image entries, unsupported extensions, and oversized images cause converter `ERROR` before the deploy worktree is modified.

Invalid Markdown reference paths and deletion of still-referenced images are not automatically detected by the Phase 0 converter. They are guarded by Zenn CLI preview and user review.

No fixed automatic recovery is defined for Zenn-side image deployment errors. The user corrects the authoritative authoring inputs according to the failure and runs the normal workflow again.

### End state on SUCCESS

- authoring `/images` contains the intended asset set;
- deploy `/images` is synchronized to it;
- article release changes only when the Markdown artifact changes;
- image-only changes leave article release, ledger, and released snapshots unchanged.
- Zenn reflects the successfully deployed image additions, replacements, and deletions.

### Known limitation / validation task

If an image deployment fails transiently after authoring `/images` has already been mirrored to deploy `/images`, no local image difference may remain to trigger the same image update again.

The retry behavior is not yet defined.

Phase 0 must verify whether pushing an empty commit to the deploy branch causes Zenn to reprocess unchanged image content.

- If it does, an empty host-side commit may be used as the manual retry mechanism.
- If it does not, recovery uses a normal workflow change such as changing the image path, and a force-image-sync mechanism may be considered later if needed.

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

For each pending article, `check` determines one of the following semantic results:

- `SUCCESS`: expected article state is confirmed and confirmed management state is advanced;
- `PENDING`: previous successful state remains and the candidate stays unresolved;
- `COLLISION`: the failed CREATE/RECREATE candidate is cleaned up without advancing confirmed management state;
- `UNKNOWN` or `ERROR`: previous successful state remains and the candidate remains unresolved.

If an article remains `PENDING`, normally wait as appropriate and run `check` again without triggering another deployment.

For an eligible pending `UPDATE`, the user may instead use manual confirmation when both of the following have been verified:

- the target deployment completed successfully in the Zenn Dashboard; and
- the current remote content equals the candidate `{article-artifact}`.

In that case, run `confirm-pending-update <article-id>` to establish the UPDATE as `SUCCESS`.

After `confirm-pending-update`, run `check` again to re-evaluate overall `COMMIT_READY` / `NOT_READY`.

After per-article results have been applied locally, `check` determines global commit readiness as either `COMMIT_READY` or `NOT_READY`.

A per-article `SUCCESS` does not by itself permit a management commit. Management or cleanup changes may be committed only when the overall check result is `COMMIT_READY`.

If the check reaches `COMMIT_READY`, review the management or cleanup diff and commit/push it with `[skip ci]`.

### Flow for image changes

Inspect the relevant Zenn Dashboard deployment entry:

- deployment success -> image deployment `SUCCESS`;
- deployment still queued or running -> `PENDING`;
- deployment failure -> image deployment `ERROR`.

After replacing image content at the same path, the visible image may take some time to reflect the new content even after deployment succeeds.

### Acceptance Criteria

- The checker is one-shot and does not long-poll.
- Only article `SUCCESS` advances confirmed article management state.
- A normal `check` result of `PENDING`, `COLLISION`, `UNKNOWN`, or `ERROR` by itself does not advance confirmed article management state.
- Successful `git push` is not treated as Zenn deployment success.
- Article API success is not treated as proof of image deployment success.
- Image-only deployment does not change article release, ledger state, or released snapshots.
- While an unresolved article deployment exists, a new deploy-triggering commit is not pushed.
- Management commits after `COMMIT_READY` use `[skip ci]`.
- Converter and checker do not modify the authoring source.
- A per-article `SUCCESS` may update local confirmed management state while the overall check remains `NOT_READY` because another article is unresolved.
- Management and cleanup changes are committed only when the overall check result is `COMMIT_READY`.

### Failure and Recovery Cases

- `PENDING`: preserve the previous successful state and normally rerun the one-shot check later. For an eligible pending `UPDATE`, manual confirmation through `confirm-pending-update` may be used after the required Dashboard and remote-content checks, followed by another `check`.
- `COLLISION`: preserve confirmed management state, commit the cleanup when ready, then return to slug resolution.
- `UNKNOWN` / `ERROR`: preserve pending and successful state, inspect and resolve the cause, rerun `check`, or use `discard-all-pendings` as appropriate.
- Image `ERROR`: diagnose using the Zenn Dashboard and correct the authoring inputs; article semantic state remains independent.

### End states

**Article SUCCESS**

- the expected remote state for that article has been confirmed;
- confirmed management state for that article has been updated locally;
- the corresponding pending entry has been removed;
- whether those local management changes may be committed is determined by the overall commit-readiness result.

**Image-only SUCCESS**

- Zenn deployment success has been confirmed;
- article release, ledger, and released snapshots are unchanged.

**Article PENDING / UNKNOWN / ERROR**

- the previous successful ledger state and released snapshot for each unresolved article remain unchanged;
- the corresponding pending entry remains;
- no new deployment is started while unresolved article state remains.

**Article COLLISION**

- confirmed ledger state and the released snapshot for that article remain unchanged;
- the failed candidate cleanup is complete;
- the corresponding pending entry has been removed;
- whether cleanup may be committed is determined by the overall commit-readiness result.

**COMMIT_READY**

- no unresolved article pending state remains;
- deploy state is consistent with confirmed management state;
- management and cleanup changes may be reviewed, committed, and pushed with `[skip ci]`.

**NOT_READY**

- unresolved pending state remains or deploy state is inconsistent;
- management and cleanup changes must not be committed;
- local changes for already-terminal articles remain in the deploy worktree until a later check reaches `COMMIT_READY`.

## US-10 Reconcile an emergency Zenn-side edit

### Purpose

Reconcile an intentional emergency edit made directly through the Zenn Web UI or another out-of-band mechanism when the normal authoring workflow was unavailable.

The already-established remote change is not imported directly into confirmed ledger state. Instead, the authoring source is reconciled with the intended remote change and the change is confirmed as a new successful release through the normal `UPDATE` publish workflow.

Accepting remote drift must not advance confirmed successful state before the normal publish workflow succeeds.

### Initial state

- source state is `active`;
- the target article has ledger confirmed state `active`;
- successful release is `N`;
- the current own-user Zenn publication has changed from the successful Zenn observation recorded for release `N`;
- no `pending_next_state` exists; and
- the user has confirmed that the emergency remote change is intentional.

### Flow

1. Inspect the current remote article content.
   - Individual Markdown output or content export may be used.
   - If the exported content is insufficient to reconstruct the intended source changes, inspect the Zenn Web UI and use comparison tools such as `git diff` to reconcile the content manually.

2. Reconcile the intended remote changes into the authoring source.
   - Apply only the remote changes that are intended to be preserved.
   - Keep the existing `{article-id}`.
   - Keep the existing `{slug}`.
   - Change source release from `N` to exactly `N+1`.

3. Commit the reconciled authoring source and make the participating source clean.

4. Run the normal publish workflow once without an observation override.
   - The converter classifies the article as an `UPDATE` candidate.
   - UPDATE preflight observes the current remote publication.
   - Because the current remote observation differs from the ledger successful Zenn observation and no `{observation-override}` is present, preflight returns `ERROR` for remote drift.
   - The checker reports:
     - the ledger successful Zenn observation;
     - the current remote observation; and
     - the current observation in the JSON representation suitable for `#observation-override`.
   - The candidate is not applied to the deploy worktree.
   - No `pending_next_state` is created.

5. Review the reported current remote observation.
   - Confirm that it represents the remote state whose intended changes were reconciled into the authoring source.
   - If it does, add that exact value to the source as `{observation-override}`.
   - Commit the authoring source and make it clean again.

6. Run the normal publish workflow again.
   - UPDATE preflight requires the current remote observation to exactly match `{observation-override}`.
   - If they match and the observation still differs from the ledger successful Zenn observation, preflight succeeds.
   - The current remote observation becomes the UPDATE `{baseline-observation}`.
   - The candidate is deployed through the normal UPDATE workflow.
   - `pending_next_state` records the UPDATE candidate and its `{baseline-observation}`.
   - Review the deploy diff, commit the deploy branch, and push it normally.

7. Run `state checker check`.

   If the UPDATE result is `SUCCESS`:
   - confirm release `N+1` using the normal UPDATE SUCCESS processing.

   If the UPDATE result remains `PENDING`:
   - normally, another one-shot `check` may be run later without starting a new deployment;
   - if manual confirmation is appropriate, the user must first verify:
     - that the target deployment completed successfully in the Zenn Dashboard; and
     - that the current remote content equals the candidate `{article-artifact}`;
   - then run `state checker confirm-pending-update <article-id>`;
   - the pending UPDATE is confirmed using the same terminal-result local apply as normal UPDATE SUCCESS.

   If the result is `UNKNOWN` or `ERROR`:
   - do not advance successful state;
   - retain the pending candidate; and
   - follow the normal recovery procedure.

8. After release `N+1` has been confirmed successful:
   - remove `{observation-override}` from the authoring source;
   - keep source release at `N+1`;
   - do not otherwise change the reconciled article content;
   - commit the override cleanup and make the authoring source clean.

9. Run `state checker check` again to confirm overall commit readiness.

10. When the result is `COMMIT_READY`:
    - review the deploy worktree management changes;
    - commit and push them with `[skip ci]`.

### Acceptance Criteria

- An intentional Zenn Web UI or other out-of-band edit can be brought back under authoring management.
- The remote change is confirmed as a new successful release through the normal `UPDATE` publish workflow.
- Accepting remote drift through `{observation-override}` alone does not change confirmed successful ledger state.
- `{observation-override}` authorizes only the specific remote observation explicitly reviewed by the user.
- Publishing does not proceed when the current remote observation differs from `{observation-override}`.
- The current remote observation from successful UPDATE preflight becomes the pending UPDATE `{baseline-observation}`.
- If normal `check` can observe UPDATE success, release `N+1` is confirmed through the normal SUCCESS path.
- If normal `check` remains `PENDING`, `confirm-pending-update` may establish SUCCESS only after the user has verified both deployment completion and remote-content equality.
- On successful confirmation:
  - ledger successful release becomes `N+1`;
  - released snapshot becomes the candidate `{article-artifact}`;
  - successful Zenn observation becomes the current remote observation used when SUCCESS is confirmed;
  - the article is removed from `pending_next_state`.
- `{article-id}` and `{slug}` are preserved.
- `{observation-override}` is removed from the source after successful release confirmation.
- Adding or removing `{observation-override}` does not itself increment the article release number.
- Management changes are committed only after overall `COMMIT_READY`.
- The management commit uses `[skip ci]` and does not trigger another Zenn deployment.

### Examples

#### Emergency typo correction

Suppose release `5` is the confirmed successful release and the authoring tool is temporarily unavailable.

1. Correct the typo through the Zenn Web UI.
2. After the authoring environment is available again, inspect the current Zenn article and apply the intended correction to the authoring source.
3. Change source release to `6` and commit it.
4. Run the normal publish workflow.
5. UPDATE preflight detects remote drift and reports the current remote observation.
6. Confirm that the reported observation represents the emergency edit.
7. Add that observation as `{observation-override}` and commit the source.
8. Run the normal publish workflow again.
9. The UPDATE candidate is deployed and recorded as pending.
10. Run `check`.
    - If it returns `SUCCESS`, release `6` is confirmed normally.
    - If it remains `PENDING`, verify successful deployment in the Zenn Dashboard and verify that remote content equals the candidate artifact, then use `confirm-pending-update`.
11. Remove `{observation-override}` from the source and commit the cleanup.
12. Confirm `COMMIT_READY` with `check`.
13. Commit and push the management changes with `[skip ci]`.

#### Exported Markdown differs from the authoring source

If individual Markdown output or exported content differs from the authoring representation:

- do not replace the authoring source blindly with the exported Markdown;
- compare the export, current source, and Zenn Web UI as necessary;
- manually apply only the intended remote changes; and
- continue with the normal US-10 workflow after reconciliation is complete.

#### Replace the emergency edit with different content later

If the emergency change should later be replaced rather than retained permanently:

1. first confirm the emergency remote change as release `N+1` through this workflow;
2. remove `{observation-override}`;
3. edit the authoring source normally; and
4. publish the replacement as release `N+2` through the ordinary UPDATE workflow.

The emergency edit is therefore represented as a real successful release rather than being silently overwritten in management history.

### Failure and Guard Cases

The workflow must not proceed when, for example:

- `pending_next_state` already exists; resolve the existing pending workflow before starting another publish workflow;
- the remote article belongs to another user;
- source `{slug}` differs from the ledger confirmed `{slug}`;
- source release is not exactly `N+1`;
- the user has not sufficiently reconciled the intended remote content into the authoring source;
- `{observation-override}` does not equal the current remote observation;
- an unexpected remote change is discovered while reviewing the observation;
- an old `{observation-override}` remains after its successful release has already been confirmed;
- an UPDATE remains `PENDING` but the user cannot confirm successful completion of the target deployment in the Zenn Dashboard; or
- an UPDATE remains `PENDING` but the user cannot confirm that current remote content equals the candidate `{article-artifact}`.

If the remote observation changes after `{observation-override}` is created, preflight fails rather than authorizing the newer remote state.

If the user cannot establish that the remote change is intentional and correctly reconciled, the override must not be added and publishing must not proceed.

`confirm-pending-update` must not be used unless both of its user-verification conditions are satisfied.

### Unverified behavior

The following remain intentionally unverified and may require changes to the reconciliation procedure after practical testing:

- whether Zenn individual Markdown output or content export can fully reconstruct the authoring source;
- differences between Web UI edits and exported Markdown;
- whether all Front Matter fields survive export as expected;
- handling of images or image references introduced through the Web UI;
- whether the Zenn observation changes after a GitHub deployment when the candidate article content is already identical to the current remote content.

If practical validation reveals gaps, the reconciliation procedure should be revised without weakening the requirement that remote drift be explicitly reviewed before publication.

### End state on SUCCESS

- the authoring source is semantically aligned with the intended Zenn-side emergency change;
- source release and ledger successful release are both `N+1`;
- no `{observation-override}` remains in the source;
- released snapshot equals the release `N+1` candidate `{article-artifact}`;
- successful Zenn observation is the current remote observation from successful release confirmation;
- deploy `/articles/<slug>.md` equals the released snapshot;
- `pending_next_state` is empty;
- the deploy worktree is clean after the `[skip ci]` management commit; and
- the authoring source is clean after the observation-override cleanup commit.
