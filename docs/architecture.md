# Architecture

## Scope

This document defines the architecture and operational behavior of the Zenn authoring tool.

The domain values and lifecycle semantics used here are defined in [`domain.md`](domain.md). This document focuses on representation, validation, publish planning, remote checks, state updates, recovery, and the division of responsibility between the authoring worktree, deploy worktree, containerized tools, the host, and Zenn.

User-facing goals and observable workflows are documented separately in `use-cases.md`. Design rationale is documented separately in `decisions.md`.

## Execution environment and responsibility boundary

The converter, state checker, preview environment, and their dependencies run inside Docker.

The container boundary limits direct access from large dependency trees such as Node.js or Python modules to the host filesystem. Only files and Git metadata required for Zenn authoring and deployment are exposed to the container.

The authoring worktree is mounted read-only.

The deploy worktree at `/_deploy` is mounted read-write so that the converter and state checker can apply generated article, image, and management changes.

Git metadata is exposed read-only:

- the authoring worktree `.git` is read-only;
- `/_deploy/.git` is read-only; and
- tools inside the container do not modify Git metadata.

Git inspection performed inside the container uses read-only Git operations.

In particular, Git status inspection uses:

`git --no-optional-locks status ...`

so that optional index refresh or other optional locking behavior is disabled.

Git history and metadata modifications are host-side responsibilities. The tools may modify ordinary files in the deploy worktree where specified, but the user performs and reviews operations such as:

- `git diff`;
- commit;
- push;
- Git restore or other recovery operations explicitly instructed by the tools; and
- worktree creation, repair, or other Git metadata changes.

The converter and state checker do not commit, push, restore Git history, or modify worktree metadata themselves.

## Repository and worktree layout

A single Git repository is used with separate worktrees for authoring and deployment.

The deploy worktree is located directly under the authoring worktree as:

`/_deploy`

`/_deploy` is a linked worktree for the `deploy` branch.

The authoring branch ignores `/_deploy/`.

The repository uses relative worktree links:

`worktree.useRelativePaths = true`

This allows the repository and linked deploy worktree to retain the same relative layout when the host and container use different absolute filesystem paths.

Both the host and container require Git 2.48 or later.

### Authoring branch

The authoring branch is `main` or `master`.

It contains the authoring sources and the authoritative `/images` tree used while writing.

The authoring worktree is allowed to contain unrelated or in-progress dirty files. Only inputs that participate in a publish operation are required to satisfy the converter's cleanliness rules.

### Deploy branch

The `deploy` branch is the deployment and management branch connected to Zenn.

It is checked out as the linked worktree:

`/_deploy`

inside the authoring worktree.

Using a relative worktree link allows both the host and container to access the same authoring/deploy relationship even when their absolute paths differ.

It is accessed through a separate worktree so that the authoring branch does not need to be switched when preparing a deployment.

Deployment changes and management-only changes are committed and pushed on this branch.

Management-only commits use `[skip ci]` so that they do not trigger a new Zenn deployment.

### Deploy worktree invariants

`/articles` and `/images` in the deploy worktree are generated or synchronized outputs managed by the converter and state checker.

The user does not directly edit these locations.

User operations on the deploy worktree are limited to:

- reviewing diffs;
- committing;
- pushing; and
- performing Git recovery operations explicitly instructed by the tools.

The converter requires the entire deploy worktree to be clean before beginning a new conversion. Staged, unstaged, or untracked differences cause an error before article generation, image synchronization, or creation of `pending_next_state`.

## Deploy worktree layout

### `/articles`

`/articles` contains the Markdown artifacts deployed to Zenn.

An active or candidate publication is represented as:

`/articles/<slug>.md`

The filename is based on `{slug}` and is independent of the authoring source filename.

### `/images`

`/images` contains image assets referenced by articles.

The deploy `/images` tree mirrors the authoritative authoring `/images` tree when the converter applies an image change.

Image constraints currently represented in the design are:

- each file is at most 3 MB;
- supported extensions are `.png`, `.jpg`, `.jpeg`, `.gif`, and `.webp`.

Image validation for size and extension is planned, but is not required in the first prototype.

### `/.zenn-authoring`

`/.zenn-authoring` stores authoring-tool management data on the deploy branch.

#### `released/`

`released/` stores the latest successfully released article artifact for managed articles.

Released snapshots are retained for erased articles.

Each released snapshot is stored as:

`/.zenn-authoring/released/<normalized-article-id>.md`

`normalized-article-id` is the lowercase hex-and-dash UUID representation of `{article-id}`.

Exactly one latest successful `{article-artifact}` is retained for each `{article-id}`.

#### `ledger.json`

`ledger.json` stores confirmed article state and unresolved publish candidates.

Its logical structure is defined below.

## Authoring source representation

The Zenn Front Matter remains at the beginning of the authoring Markdown file.

Authoring-tool management information follows the Front Matter as HTML comments at the beginning of the article body.

The supported management comments are:

- `<!-- #id:{article-id} -->`
- `<!-- #slug:{slug} -->`
- `<!-- #state:{article-state} -->`
- `<!-- #release:{release-number} -->`

`{article-id}` uses a UUID as its concrete string representation.

`{slug}` may be omitted before the first publish.

`{release-number}` may be omitted before the first publish.

The source `#state` declares the next intended `{article-state}`.

For an operation that publishes an `{article-artifact}`, source `#release` is treated as the candidate `{release-number}`.

The converter parses consecutive management comments immediately following the Front Matter.

When an article artifact is generated, all authoring-tool management comments are removed. The Zenn Front Matter and article body are otherwise preserved according to the normal rendering rules.

Management comment representation does not itself perform validation or transition classification. Those responsibilities belong to the converter and state checker.

## Ledger representation

### `articles`

`articles` records confirmed successful state keyed by normalized `{article-id}`.

For each managed article it stores:

- `slug`: the currently confirmed `{slug}`;
- `state`: the confirmed `{article-state}`, either `active` or `erased`;
- `successful release`: the latest `{release-number}` whose Zenn result has been confirmed successful;
- `successful Zenn observation`: the remote observation associated with the last confirmed successful own-user publication; and
- the released snapshot stored under `/.zenn-authoring/released/`.

The concrete successful Zenn observation contains:

- `body_updated_at`;
- `source_repo_updated_at`; and
- `published_at`.

For `CREATE`, `UPDATE`, and `RECREATE` success, the successful Zenn observation is updated to the current observation of the own-user publication.

For `DELETE` success, the previous successful Zenn observation is retained.

Observations of other users' publications are not stored as the article's successful Zenn observation.

### `pending_next_state`

`pending_next_state` records publish candidates whose remote result has not yet been confirmed.

The existence of any pending article blocks generation of a new publish candidate.

Each pending entry is keyed by `{article-id}` and stores:

- `operation`: the candidate `{operation}`;
- `slug`: the candidate `{slug}`;
- `state`: the candidate target `{article-state}`;
- `release`: the candidate `{release-number}`; and
- `baseline observation`: only for `UPDATE`.

The target state is:

- `active` for `CREATE`, `UPDATE`, and `RECREATE`;
- `erased` for `DELETE`.

An `UPDATE` pending entry stores the `{baseline-observation}` returned by successful preflight.

`CREATE`, `DELETE`, and `RECREATE` do not store a baseline observation.

`pending_next_state` is not the Zenn deploy status itself. It records the local unresolved publish transaction that must be reconciled with the remote state.

Pending entries are resolved only by:

- successful result confirmation;
- collision cleanup; or
- `discard-all-pendings`.

## Preview environment

Preview is independent from the deploy worktree and does not alter article state, release information, the ledger, or deploy artifacts.

### Purpose

The preview environment exposes the current authoring worktree to Zenn CLI so that in-progress content, including `hold` or dirty articles, can be previewed.

Preview success does not guarantee deploy success.

### Preview filesystem view

A temporary filesystem view is created inside the container according to the requirements of Zenn CLI.

For each previewed article:

`/preview/articles/<slug>.md`

is a symlink to the actual authoring Markdown file.

The article is not copied or converted for preview. Authoring management HTML comments therefore remain present during preview.

Only the symlink filename is adjusted to the slug-based filename expected by Zenn.

If the source has no declared `{slug}`, preview uses the synthetic slug:

`new-<normalized-article-id>`

This synthetic slug is used only for preview. It is not treated as the source `{slug}` and is not written back to the authoring file.
The article-id normalization is the same lowercase hex-and-dash UUID representation used for article-id comparison / ledger keys.

A source with no article ID is not treated as an article file for preview.

For images:

`/preview/images`

is a symlink to the authoring worktree `/images`.

Images are not copied into a separate preview tree.

### Zenn CLI

Zenn CLI is run from the preview directory inside the container using:

`zenn preview --host 0.0.0.0`

The container port is exposed to the host.

## Converter

The converter prepares a fixed publish plan and applies it to the deploy worktree only after all required local and remote checks succeed.

### Execution model

The converter assumes explicit, sequential execution by a single user.

The following are unsupported operations:

- running multiple conversions concurrently;
- modifying participating authoring sources while a conversion is running.

The converter does not implement locking or mid-run source-change detection for these cases.

Unexpected partial results or abnormal termination are detected and recovered through:

- the deploy worktree's dirty state; and
- user review of `git diff`.

### Git state inspection

The converter inspects Git state from inside the container using the read-only Git metadata exposed by the container configuration.

Git inspection must not modify the index, worktree metadata, refs, or repository history.

Commands that may perform optional locking use `--no-optional-locks` where applicable.

Git commands that modify repository or worktree metadata are not part of converter or state-checker behavior and remain host-side operations.

### Authoring worktree checks

The entire authoring worktree does not need to be clean.

Articles that participate in a publish operation must be committed and clean.

This applies to publish-target articles whose source state is:

- `active`; or
- `erased`.

`hold` articles may contain staged, unstaged, or untracked changes.

The authoring `/images` tree that will be synchronized to deploy must be committed and clean.

Dirty files outside managed article and image inputs are ignored.

For these checks, clean means that no staged or unstaged difference exists relative to `HEAD`.

### Deploy worktree check

At the beginning of conversion, the entire deploy worktree must be clean.

If staged, unstaged, or untracked differences exist:

- conversion fails;
- article artifact application does not begin;
- image mirror synchronization does not begin; and
- `pending_next_state` is not created.

### Per-article processing

`per article task` is a list of required processing responsibilities, not a required internal execution order.

For each managed source, the converter performs the required work including:

- parsing management comments;
- checking Zenn article Front Matter;
- determining and validating release information;
- validating `{article-id}`;
- validating `{slug}`;
- classifying article state and `{operation}`;
- generating the candidate `{article-artifact}` when needed; and
- creating a per-article publish-plan entry.

### Article ID handling

The converter obtains one `#id` from each managed source.

The concrete value is validated as a UUID.

For comparison and ledger lookup, the value is normalized to lowercase hexadecimal with dashes.

Ledger `articles` entries are looked up by `{article-id}`.

Source filename and `{slug}` are not used as the logical article identity.

### Article state and operation classification

The converter validates source `#state` as one of:

- `active`;
- `hold`;
- `erased`.

Any other value is an error.

If source state is `hold`, no publish transition is generated.

If there is no ledger confirmed state:

- source `active` -> `CREATE`;
- source `erased` -> no publish transition.

If ledger confirmed state is `active`:

- source `active` with candidate `{article-artifact}` different from the released snapshot -> `UPDATE`;
- source `active` with candidate `{article-artifact}` equal to the released snapshot -> no article operation;
- source `erased` -> `DELETE`.

If ledger confirmed state is `erased`:

- source `active` -> `RECREATE`;
- source `erased` -> no publish transition.

The converter performs this classification once. Later phases do not reclassify the operation.

### Front Matter validation

Required Zenn Front Matter elements must be present.

If source state is `active`, Front Matter must have `published: true`; otherwise conversion fails.

### Release and slug validation

Release and slug validation is operation-specific.

The detailed validation must preserve the operation selected by state classification rather than infer a new operation from release or slug values.

### Candidate artifact generation

Candidate generation takes the authoring Markdown as input.

It removes only the authoring-tool management HTML comments.

Zenn Front Matter and article body are otherwise preserved by the normal renderer.

Candidate generation itself does not write the artifact into the deploy worktree.

### Article global checks

After per-article processing, the converter performs cross-article checks.

Conversion fails if:

- normalized `{article-id}` is duplicated among sources;
- a ledger-managed `{article-id}` has no corresponding source;
- candidate `{slug}` values are duplicated among publish candidates.

The source filename is not part of identity matching.

`{article-id}` and `{slug}` are not combined into a composite identity.

### Image mirror diff

The converter computes the mirror difference between authoring `/images` and deploy `/images`.

The publish plan records whether image changes exist.

### Publish plan

A per-article publish-plan entry is created after local validation and before modifying the deploy worktree.

Each article entry is constructed from:

- source information;
- ledger information;
- validation results;
- the candidate `{article-artifact}` when required; and
- the `{operation}` selected by the state check.

Publish eligibility and `{operation}` are decided exactly once in this phase.

After all article entries are collected, image mirror differences are added to produce the final publish plan.

The final publish plan is immutable for the remainder of the conversion.

Exactly the same publish plan is passed to state-checker preflight and, if preflight succeeds, to deploy application.

### Preflight invocation

The converter invokes state-checker `preflight` with the finalized publish plan.

If preflight does not return `OK`, the converter does not apply the plan.

For `UPDATE`, successful preflight returns a `{baseline-observation}` that is retained for the pending candidate.

### Publish-plan application

The converter applies only the same plan that passed preflight.

The operation is not reclassified during apply.

For `CREATE`, `UPDATE`, and `RECREATE`, the plan's candidate `{article-artifact}` is written to:

`deploy /articles/<candidate-slug>.md`

For `DELETE`:

`deploy /articles/<confirmed-slug>.md`

is removed.

Image mirror differences are applied to deploy `/images`.

If the publish plan contains one or more article operations, `pending_next_state` is generated only after all deploy-worktree changes have succeeded.

Pending content is derived from the fixed publish plan.

For `UPDATE`, the pending entry also receives the `{baseline-observation}` returned by preflight.

If no article operation exists, such as an image-only change, no `pending_next_state` is generated.

## State checker

The state checker observes remote article state and reconciles local management state with confirmed remote outcomes.

The initial implementation is one-shot. For each article that requires observation, it performs one Zenn API observation and exits. The user reruns `check` when another observation is needed.

The state checker uses the Zenn article API through a small abstraction boundary so that API-specific behavior can be isolated if the API changes.

### Result model

The state checker uses these article results:

- `SUCCESS`: the expected remote state for the pending `{operation}` has been confirmed;
- `PENDING`: the expected remote state cannot yet be confirmed;
- `COLLISION`: a `CREATE` or `RECREATE` candidate `{slug}` collides with another user's publication;
- `UNKNOWN`: the remote response cannot be interpreted as a state covered by the specification;
- `ERROR`: remote observation or processing cannot be completed normally.

`SUCCESS` is a post-deploy result and is not returned by preflight.

### `preflight`

`preflight` is called automatically during conversion to verify that the finalized local publish plan is compatible with the current remote state.

It receives the publish plan as input.

It does not:

- decide which articles are publish targets;
- classify `{operation}`; or
- reclassify an operation.

It first checks `pending_next_state`. If unresolved pending state exists, conversion is not allowed to proceed.

For each article operation in the plan, preflight:

1. obtains ledger confirmed state by the plan's `{article-id}`;
2. validates that the plan's `{operation}` is compatible with the ledger premise;
3. checks the current remote state according to that operation.

If the plan contains no article operation, such as an image-only change, article remote transition checks are skipped and preflight returns `OK`.

Preflight results are:

- `OK`: conversion may continue;
- `COLLISION`: the candidate slug cannot be used;
- `ERROR`: ledger/remote consistency failed or the remote state could not be observed normally.

#### CREATE preflight

For a new article with no ledger record:

- API `404` -> `OK`;
- API `200` for another user's article -> `COLLISION`;
- other results -> `ERROR`.

#### UPDATE preflight

The ledger state and source state are both `active`.

Preflight obtains the current `{zenn-observation}`.

Any result other than API `200` for the user's own publication is an error.

For the own-user publication, the current observation is compared with the ledger successful Zenn observation.

If they differ:

- preflight returns `ERROR`;
- the condition is reported as remote drift.

If they match:

- preflight returns `OK`;
- the current observation is returned as `{baseline-observation}`.

#### DELETE preflight

The ledger state is `active` and source state is `erased`.

- API `404` -> `OK`, with a warning that the publication has already been deleted;
- API `200` for the user's own publication:
  - current observation == ledger successful Zenn observation -> `OK`;
  - current observation != ledger successful Zenn observation -> `ERROR` as remote drift.
- API `200` for another user's publication -> `OK`, with a warning that the old slug has already been reused.

Remote drift of the own-user publication is an error.

#### RECREATE preflight

The ledger state is `erased` and source state is `active`.

If the ledger slug differs from the source slug, preflight first checks the old ledger slug.

- API `404` -> `ERROR`; the previous confirmed slug is still available and must be used.
- API `200` for the user's own publication -> `ERROR`; the publication is not deleted or the remote state is unexpected.
- API `200` for another user's publication -> continue by checking the source slug.

The source slug is then checked:
- API `404` -> `OK`.
- API `200` for another user's publication -> `COLLISION`.
- other results -> `ERROR`.

### `check`

`check` normally runs after the deploy branch has been committed and pushed.

It is also used after recovery operations to re-evaluate whether management changes are safe to commit.

Release success is confirmed only by the state checker, not by successful `git push`.

#### Observation phase

Before applying any local terminal result, `check` observes every article currently in `pending_next_state`.

During the observation phase it does not modify:

- the ledger;
- released snapshots; or
- deploy artifacts.

After all required observations have been collected, terminal results are applied locally article by article.

#### CREATE result

- API `404` -> `PENDING`;
- API `200` for the user's own article -> `SUCCESS`;
- API `200` for another user's article -> `COLLISION`.

#### UPDATE result

The current `{zenn-observation}` is obtained.

For API `200` on the user's own publication:

- current observation different from pending `{baseline-observation}` -> `SUCCESS`;
- current observation equal to pending `{baseline-observation}` -> `PENDING`.

Other results follow the `ERROR` / `UNKNOWN` rules.

#### DELETE result

- API `404` -> `SUCCESS`;
- API `200` for another user's article -> `SUCCESS`;
- API `200` for the user's own article -> `PENDING`.

For `DELETE`, the Zenn publication must also be deleted from the Zenn Dashboard.

Dashboard deletion is not ordered relative to the deploy-side artifact removal:

- it may be performed before conversion/deployment; or
- it may be performed after a check returns PENDING because the user's publication still exists.

`DELETE` reaches semantic `SUCCESS` when check confirms that the previous own-user publication no longer exists.

#### RECREATE result

RECREATE result checking follows the new-article result model for the candidate slug.

### Applying `SUCCESS`

For `CREATE` success:

- create or finalize `articles[{article-id}]`;
- state = `active`;
- slug = candidate `{slug}`;
- successful release = candidate `{release-number}`;
- released snapshot = candidate `{article-artifact}`;
- successful Zenn observation = current observation of the user's own publication.

For `UPDATE` success:

- successful release = candidate `{release-number}`;
- released snapshot = candidate `{article-artifact}`;
- successful Zenn observation = current observation of the user's own publication.

For `DELETE` success:

- state = `erased`;
- retain the existing slug;
- retain successful release;
- retain released snapshot;
- retain successful Zenn observation.

For `RECREATE` success:

- state = `active`;
- slug = candidate `{slug}`;
- successful release = candidate `{release-number}`;
- released snapshot = candidate `{article-artifact}`;
- successful Zenn observation = current observation of the user's own publication.

After successful application, the corresponding `pending_next_state[{article-id}]` entry is removed.

### Applying `COLLISION`

`COLLISION` applies to `CREATE` and `RECREATE`.

The confirmed successful article state is not changed.

The checker:

- reads the candidate slug from `pending_next_state`;
- removes `deploy/articles/<candidate-slug>.md`;
- removes the article's pending entry;
- leaves the source unchanged;
- leaves cleanup changes dirty in the deploy worktree;
- instructs the user to review, commit, and push the cleanup using `[skip ci]`.

The user then changes source `#slug` to another candidate and starts a later publish workflow.

### `PENDING`, `UNKNOWN`, and `ERROR`

For these non-terminal outcomes:

- confirmed successful state is not advanced;
- the pending entry remains.

A later `check` may re-observe unresolved articles.

### Commit readiness

At the end of `check`, the state checker determines whether management changes may be committed.

If `pending_next_state` is non-empty:

- result is `NOT_READY`;
- commit is prohibited;
- management changes for already terminal articles remain in the worktree;
- only unresolved articles are observed by the next `check`.

If `pending_next_state` is empty, the checker validates confirmed state against deploy `/articles`.

For each confirmed `active` article:

- `deploy/articles/<ledger.slug>.md` must exist;
- the deploy artifact must equal the released snapshot.

For each confirmed `erased` article:

- `deploy/articles/<ledger.slug>.md` must not exist.

No ledger-unmanaged article artifact may remain in deploy `/articles`.

If all checks pass:

- result is `COMMIT_READY`;
- after reviewing the diff, the user may commit and push with `[skip ci]`.

If any check fails:

- result is `NOT_READY`;
- commit is prohibited;
- rollback or cleanup remains incomplete.

#### Command result

`check` reports commit readiness through both its command-line output and exit code.

`COMMIT_READY`:

- pending article count is zero;
- deploy state is consistent;
- management changes are ready to commit;
- the output instructs the user to use `[skip ci]`;
- exit code is `0`.

`NOT_READY`:

- unresolved pending articles remain, or the deploy state is inconsistent;
- management changes must not be committed;
- exit code is `1`.

Internal errors and other failures that prevent the readiness result from being determined use exit code `2`.

### `import`

`import` reconciles remote state that exists outside the normal pending publish flow.

The target article is selected by `{article-id}` or source filename.

Common guards require:

- ledger confirmed state is `active`;
- no `pending_next_state` exists;
- the deploy worktree is clean;
- current remote state can be obtained;
- the current remote publication belongs to the user;
- current remote `{slug}` equals the confirmed ledger `{slug}`.

#### Default import

Default `import` is for reconciling remote metadata drift when the user has confirmed that remote article content still matches the released snapshot.

It obtains the current `{zenn-observation}` and updates only the ledger successful Zenn observation.

It does not change:

- state;
- slug;
- successful release; or
- released snapshot.

If remote content differs from the released snapshot, or equality cannot be confirmed, import is not performed and returns an error.

#### `import --adopt-remote`

`--adopt-remote` explicitly adopts an article change that was made outside the authoring tool as a new successful release.

It requires explicit user action.

Preconditions include:

- no pending state;
- clean deploy worktree;
- target source committed and clean;
- ledger confirmed state `active`;
- ledger successful release = `N`;
- source state `active`;
- source slug equals confirmed ledger slug;
- source `#release` = `N+1`;
- source article artifact differs from released snapshot `N`;
- current remote is the user's own publication;
- the user has reconciled the source with current remote content.

The normal artifact renderer is used to generate the adopted article artifact from source Markdown.

`--adopt-remote` does not:

- run image mirror synchronization;
- run preflight; or
- create `pending_next_state`.

On successful adoption:

- `deploy/articles/<confirmed-slug>.md` becomes the generated article artifact;
- released snapshot becomes the generated article artifact;
- successful release becomes `N+1`;
- successful Zenn observation becomes the current observation;
- state, slug, and `{article-id}` are unchanged.

After adoption, `check` is used for commit-readiness validation. A `COMMIT_READY` result allows the management changes to be committed and pushed with `[skip ci]`.

### `discard-all-pendings`

`discard-all-pendings` prepares unresolved candidates for explicit user-driven rollback when `ERROR`, `UNKNOWN`, or another unresolved condition cannot be automatically finalized.

It requires:

- at least one pending article;
- the user has decided to discard all candidates after checking remote state;
- no later deploy commit exists while the candidates are pending;
- `HEAD` is the deploy commit containing the pending candidates.

#### Remote discard safety

Before discard, the user must confirm using Zenn Dashboard or equivalent evidence that the target deploy can no longer apply the candidates later.

A current remote observation alone is not sufficient evidence that the deploy is terminal.

If it cannot be established whether a candidate may still be applied later:

- discard is not performed;
- pending state is retained;
- the command returns an error.

Remote consistency expectations are operation-specific:

- `CREATE`: the candidate has not become the user's own publication;
- `UPDATE`: current observation remains consistent with baseline / previous successful state;
- `DELETE`: the previous successful publication has not been deleted;
- `RECREATE`: the candidate has not become the user's own publication.

#### Local discard operation

The command validates that the current deploy candidate still matches the pending record.

If any validation fails, pending state is not changed.

If all candidates pass validation:

- confirmed successful article state is not changed;
- released snapshots are not changed;
- `pending_next_state` is cleared;
- the state checker does not execute Git rollback itself;
- host-side restore commands are printed.

The recovery flow then uses host-side Git operations such as:

`git restore --source=HEAD^ --worktree -- articles/<candidate-slug>.md ...`

followed by:

- `git diff`;
- review of cleanup content;
- `state checker check`.

Immediately after `discard-all-pendings`, the deploy worktree is intentionally dirty:

- ledger pending state is empty;
- article rollback is still waiting for user action.

This dirty state prevents a new publish workflow from starting.

Recovery is complete only after the user performs restore, reviews the diff, commits, and pushes the recovery changes.

## Publish workflow

The normal article publish workflow is:

1. run converter `convert`;
2. review the deploy worktree diff;
3. commit the deploy branch, including `pending_next_state` when article operations exist;
4. push the deploy branch;
5. allow Zenn deployment to run;
6. run state checker `check`.

`check` evaluates remote state from `pending_next_state`.

Per article:

- `SUCCESS` advances confirmed state and removes the pending entry;
- `PENDING` keeps confirmed state unchanged and retains pending;
- `COLLISION` cleans up the failed candidate and removes its pending entry;
- `UNKNOWN` or `ERROR` keeps confirmed state unchanged and retains pending.

After `check`:

- `COMMIT_READY` -> review management/cleanup diff, then `[skip ci]` commit and push;
- `NOT_READY` -> do not commit; rerun `check` or use an appropriate recovery path such as `discard-all-pendings`.

## Image workflow

Images are intentionally independent from the article lifecycle.

The authoring `/images` tree is the authoritative image source.

Deploy `/images` is synchronized as a mirror.

Assets placed in authoring `/images` must therefore be safe to synchronize to Zenn regardless of article state.

Images do not have:

- article publish state;
- article release numbers;
- article ledger entries;
- released article snapshots; or
- image-specific pending state.

An image-only change:

- may modify deploy `/images`;
- does not increment article `{release-number}`;
- does not modify article ledger state;
- does not modify released article snapshots;
- does not create `pending_next_state`.

If an article Markdown image reference changes, the Markdown artifact changes and the normal article release rules apply.

Unused images are not removed merely because no active article currently references them. Removal is explicit through the authoritative authoring `/images` mirror.

Preview uses the authoring image tree directly through a symlink.

Article semantic results and image deployment results are independent.

Article state checker success does not prove image deployment success.

If a deployment contains both article and image changes, each result is handled independently. An image deployment result of `ERROR` does not alter the article semantic result determined by the state checker.

If the article result is `SUCCESS`, its confirmed article state may be committed even when the image deployment failed.

Image-only deployment success is confirmed through the Zenn Dashboard deployment result.

No fixed automatic recovery procedure is defined for image deployment errors. Depending on the failure cause, the user may rename an image, update references, remove an image, or otherwise correct the authoritative authoring inputs and rerun the normal publish workflow.

## External Zenn interfaces

### GitHub repository integration

The deploy branch is connected to Zenn's GitHub repository integration.

A normal push to the configured deploy branch triggers Zenn synchronization.

Management-only commits include `[skip ci]` so that Zenn deployment is skipped.

### Zenn CLI

Zenn CLI is used for local preview inside the container.

### Zenn article API

The state checker uses the non-public endpoint:

`https://zenn.dev/api/articles/{slug}`

The current design treats:

- `404` as no publication for that slug;
- `200` as a publication being present.

The response is used to distinguish the user's own publication from another user's publication and to construct `{zenn-observation}`.

Relevant response information currently includes:

- article `published_at`;
- article `body_updated_at`;
- article `source_repo_updated_at`;
- user identity such as `id` and `username`.

The exact change behavior of the update-related metadata has not been fully verified and must be validated empirically.

API-dependent checks should be isolated behind small functions so that a future API change does not require broad changes to the state checker.

## Supporting utilities

### Slug-generation utility

A supporting utility may generate a proposed slug from an article using a form such as:

`<fixed-prefix>-<summary-words-or-phrase>-<random-suffix>`

The user copies the resulting slug into the source.

Random suffixes are used to reduce collision probability. If a `CREATE` or `RECREATE` candidate collides, a new candidate slug can be generated.

## Known limitations and backlog

### UPDATE attribution limitation

While an `UPDATE` candidate is pending, an out-of-band modification to the same remote publication may change the remote observation.

Because the current UPDATE success check uses a difference from `{baseline-observation}` as evidence of update reflection, such an out-of-band modification may be mistaken for the candidate deployment.

This is a known limitation of the current design.

### Repeated check polling

The initial state checker is one-shot.

A possible future enhancement is bounded repeated observation with intervals such as:

- 15 seconds;
- +15 seconds;
- +30 seconds;
- +1 minute;
- +2 minutes;
- +2 minutes;
- +4 minutes;

for a total of approximately 10 minutes, with interruption by `Ctrl+C`.

This behavior is backlog and is not part of the initial version.
