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

`/images` contains image assets available for Zenn deployment.

The authoring `/images` tree is authoritative. The deploy `/images` tree mirrors it when the converter applies an image change.

Phase 0 validates every file that participates in the authoritative image mirror, not only files that differ from the current deploy tree.

The Phase 0 local image constraints are:

- the accepted extensions are exactly `.png`, `.jpg`, `.jpeg`, `.gif`, and `.webp`;
- the accepted extension spellings are lowercase only;
- each file must be at most `3_000_000` bytes;
- directory structure below `/images` is unrestricted;
- no additional filename-character restriction is imposed;
- each mirrored Git entry must be a normal blob entry;
- Git symlinks and gitlinks are rejected; and
- filesystem special files are rejected.

The `3_000_000`-byte limit is a conservative local interpretation of Zenn's documented 3 MB image limit.

### `/.zenn-authoring`

`/.zenn-authoring` stores authoring-tool management data on the deploy branch.

#### `released/`

`released/` stores the latest successfully released article artifact for managed articles.

Released snapshots are retained for erased articles.

Each released snapshot is stored as:

`/.zenn-authoring/released/<normalized-article-id>.md`

`normalized-article-id` is the lowercase hex-and-dash UUID representation of `{article-id}`.

Exactly one latest successful `{article-artifact}` is retained for each `{article-id}`.

### Released snapshot invariant

For every ledger-managed article with successful release `N`, exactly one released snapshot exists and represents the `{article-artifact}` of successful release `N`.

This invariant applies whether the ledger confirmed state is `active` or `erased`.

A successful `DELETE`:

- does not create a new `{release-number}`;
- does not modify the released snapshot; and
- leaves the snapshot representing the last successfully published `{article-artifact}` from before deletion.

Therefore, for an `erased` article, the retained released snapshot remains the artifact baseline for a later `RECREATE`.

When `RECREATE` is classified:

- if the candidate `{article-artifact}` equals the retained released snapshot for successful release `N`, the candidate uses release `N`;
- if it differs, the candidate uses release `N+1`.

On successful `RECREATE`, the released snapshot is atomically replaced with the candidate `{article-artifact}`. If it already equals the candidate artifact, the replacement is an idempotent no-op.

#### `ledger.json`

`ledger.json` stores confirmed article state and unresolved publish candidates.

It is created explicitly by `state checker init` before the repository enters the normal publish workflow.

The canonical empty ledger is:

```json
{
  "articles": {},
  "pending_next_state": {}
}
```

After initialization, `ledger.json` is updated by normal state transitions.

Its logical structure is defined below.

#### `config.json`

`config.json` stores repository-level Zenn account identity configuration.

It is created by `state checker init` and is not modified by normal publish or state-transition processing after initialization.

Its logical content includes:

```json
{
  "zenn_user": {
    "id": 12345,
    "username": "example-user"
  }
}
```

`zenn_user.id` is the configured `{zenn-user-id}` and is the canonical value used for own-user publication checks.

`zenn_user.username` records the username resolved during initialization for diagnostics and human-readable output. It is not used for owner identity comparison.

## Management-file and released-snapshot update method

Management JSON files and released snapshots MUST NOT be updated in place.

The complete replacement content is written to a temporary file in the same directory and atomically replaces the destination.

This rule applies to:

- `ledger.json`;
- `config.json` when it is created by `state checker init`; and
- released snapshots.

For normal operation after initialization, `config.json` is read-only from the tool's perspective.

## Authoring source representation

The Zenn Front Matter remains at the beginning of the authoring Markdown file.

Authoring-tool management information follows the Front Matter as HTML comments at the beginning of the article body.

The supported management comments are:

- `<!-- #id:{article-id} -->`
- `<!-- #slug:{slug} -->`
- `<!-- #state:{article-state} -->`
- `<!-- #release:{release-number} -->`
- `<!-- #observation-override:{JSON} -->`

`{article-id}` uses a UUID as its concrete string representation.

`{slug}` may be omitted before the first publish.

`{release-number}` may be omitted before the first publish.

The source `#state` declares the next intended `{article-state}`.

For an operation that publishes an `{article-artifact}`, source `#release` is treated as the candidate `{release-number}`.

`#observation-override` is optional and may appear zero or one time. Its JSON value is the concrete representation of one `{zenn-observation}`. Multiple observation-override comments are invalid.

The converter parses consecutive management comments immediately following the Front Matter.

When an article artifact is generated, all authoring-tool management comments are removed. The Zenn Front Matter and article body are otherwise preserved according to the normal rendering rules.

Management comment representation does not itself perform validation or transition classification. Those responsibilities belong to the converter and state checker.

### Observation override validation

The converter parses the optional `#observation-override` management comment as an `{observation-override}`.

If no override is present, no additional validation is required.

If an override is present:

- its JSON value must be a valid concrete `{zenn-observation}` representation;
- the already-classified `{operation}` must be `UPDATE`;
- otherwise conversion fails.

Observation-override validation does not classify or reclassify the article operation.

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

The existence of any pending article prevents a new deployable publish candidate from being authorized or applied.

Temporary in-memory candidate artifacts and publish-plan entries may still be constructed. Preflight enforces the pending guard before any publish plan is applied to the deploy worktree.

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
- `confirm-pending-update` for an eligible pending `UPDATE`;
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

Before publish-input cleanliness is evaluated, the converter performs a minimal parse of the authoring-tool management comments sufficient to obtain source `#state`.

This minimal parse:

- is used only to determine the applicable authoring-input cleanliness rule;
- does not classify `{operation}`; and
- does not replace the full per-article management-comment parsing and validation performed later.

For source state `active` or `erased`, the source article is a publish input and must be committed and clean.

Specifically:

- the source path must be tracked in `HEAD`;
- there must be no staged difference from `HEAD`; and
- there must be no unstaged difference from the index.

If any of these conditions is not satisfied, conversion fails before publish planning or deploy-worktree modification.

For source state `hold`:

- the source path may be tracked or untracked;
- staged differences are allowed; and
- unstaged differences are allowed.

A `hold` article is therefore not required to represent committed publish input.

The authoring `/images` tree is authoritative for the deploy image mirror.

Every file that would participate in the `/images` mirror must:

- be tracked in `HEAD`;
- have no staged difference from `HEAD`; and
- have no unstaged difference from the index.

Untracked or ignored files under authoring `/images` are not valid mirror inputs and cause conversion to fail rather than being copied to deploy.

Dirty files outside managed article inputs and authoring `/images` are ignored.

The converter performs these authoring-input checks once near the beginning of conversion.

Concurrent converter execution and authoring-source modification while conversion is running are unsupported operations. The converter does not perform a second source-cleanliness check immediately before apply.

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

#### `published_at`

`published_at` is optional.

When present, it must be parseable in one of the formats accepted by Zenn:

- `YYYY-MM-DD`;
- `YYYY-MM-DD HH:MM`.

`published_at` is interpreted in JST.

A date-only value (`YYYY-MM-DD`) is interpreted as `00:00 JST`.

Comparison of `published_at` values uses their parsed semantic datetime values rather than their source-text representation.

For `CREATE`:

- absent -> OK;
- past or current datetime -> OK and is treated as the initial publication date on Zenn;
- future datetime -> `ERROR` in the Phase 0 MVP.

For `RECREATE`, the same rules as `CREATE` apply because the operation creates a new Zenn publication.

For `UPDATE`:

- compare source `published_at` with the value in the released snapshot;
- if both represent the same semantic value, validation succeeds;
- adding, removing, or changing the value is an `ERROR`.

The UPDATE restriction reflects that Zenn does not permit an already-established publication date to be changed.

For an `active` article for which no publish operation was generated:

- source `published_at` must have the same semantic value as the released snapshot;
- otherwise conversion fails.

For `DELETE`, `published_at` transition validation is not performed.

For source state `hold`, `published_at` transition validation is not performed.

Scheduled publication using a future `published_at` is not supported in the Phase 0 MVP. Its future design is described under Known limitations and backlog.

### Release and slug validation

Release and slug validation is performed after `{operation}` has been classified.

Validation must not reclassify the operation.

#### Release validation

For `CREATE`:

- source `#release` must be `1`;
- otherwise conversion fails.

For an article whose ledger successful release is `N`, `UPDATE` / `RECREATE` validation uses candidate-artifact equality:

- if candidate `{article-artifact}` equals released snapshot `N`, source `#release` must be `N`;
- if candidate `{article-artifact}` differs from released snapshot `N`, source `#release` must be exactly `N+1`;
- otherwise conversion fails.

For `DELETE`:

- source `#release` must equal ledger successful release `N`;
- `{article-artifact}` differences are not used for DELETE release validation;
- otherwise conversion fails.

For source state `hold`, release transition validation is not performed.

For an `active` article for which no publish operation was generated:

- ledger confirmed state is `active`;
- candidate `{article-artifact}` equals the released snapshot for successful release `N`;
- source `#release` must remain `N`;
- otherwise conversion fails.

#### Slug validation

Every publish candidate must have a `{slug}`.

The `{slug}` must satisfy the domain constraints for allowed characters and length.

A candidate `{slug}` must not equal the ledger confirmed `{slug}` of a different `{article-id}`.

For `CREATE`:

- no confirmed slug exists for the article;
- remote availability of the candidate slug is checked by state-checker preflight.

For `UPDATE`:

- source `{slug}` must equal ledger confirmed `{slug}`;
- otherwise conversion fails.

For `DELETE`:

- source `{slug}` must equal ledger confirmed `{slug}`;
- otherwise conversion fails.

For `RECREATE`:

- if source `{slug}` equals ledger confirmed `{slug}`, that slug is used as the recreate candidate;
- if source `{slug}` differs from ledger confirmed `{slug}`, local validation permits the candidate;
- whether that slug change is allowed is determined by state-checker RECREATE preflight from the remote state of the previous confirmed slug.

For source state `hold`, slug transition validation is not performed.

For an `active` article for which no publish operation was generated:

- ledger confirmed state is `active`;
- source `{slug}` must equal ledger confirmed `{slug}`;
- otherwise conversion fails.

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

### Image validation

After per-article processing and article-global checks, and before computing the image mirror difference, the converter validates the complete authoring `/images` mirror input.

For each mirror input, the converter validates:

- that the Git entry is a normal blob rather than a symlink or gitlink;
- that the filesystem object is not a special file;
- that the extension is one of the supported lowercase extensions; and
- that the file size does not exceed `3_000_000` bytes.

If any image validation fails, conversion stops before image mirror-difference calculation, publish-plan finalization, state-checker preflight, or deploy-worktree application.

In particular:

- image mirror synchronization does not begin;
- deploy application does not begin; and
- no `pending_next_state` is generated.

The converter does not require an image to be referenced by Markdown.

Markdown image-reference validity, including missing images and references that do not use `/images/...`, is outside Phase 0 converter validation and is checked through Zenn CLI preview and user review.

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

For an `UPDATE` entry, if the source contains an `{observation-override}`, the same value is retained in the per-article publish-plan entry.

The `{observation-override}` is planning/preflight input only. It is not written to `pending_next_state`.

After successful UPDATE preflight, only the returned `{baseline-observation}` is stored in the pending entry.

### Preflight invocation

The converter invokes state-checker `preflight` with the finalized publish plan.

If preflight does not return `OK`, the converter does not apply the plan.

For `UPDATE`, successful preflight returns a `{baseline-observation}` that is retained for the pending candidate.

Temporary candidate artifacts and publish-plan entries may be constructed while unresolved pending state exists.

These are in-memory planning data only and do not become a deployable candidate unless preflight succeeds.

`preflight` enforces the pending guard before any publish plan is applied to the deploy worktree.

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

### Configuration and owner identity

The state checker reads repository account configuration from:

`/_deploy/.zenn-authoring/config.json`

The configured `zenn_user.id` is the repository's `{zenn-user-id}`.

For an observed Zenn article:

- `article.user.id == configured {zenn-user-id}` means an own-user publication;
- a different `article.user.id` means another user's publication;
- missing or invalid `article.user.id` is an `ERROR` and is treated as a possible API-schema change.

The checker does not fall back to username comparison when numeric owner identity cannot be established.

Any state-checker operation that performs remote publication observation loads `config.json` read-only before owner-sensitive interpretation.

If:

- `config.json` is missing;
- the JSON cannot be parsed;
- `zenn_user.id` is missing; or
- `zenn_user.id` is not a valid numeric ID,

the operation fails with `ERROR` and does not perform owner classification.

When configuration is missing, the user is instructed to run:

`state checker init <zenn-username>`


### `init <zenn-username>`

`init` establishes the repository's Zenn account identity and initial management state.

It uses:

`GET https://zenn.dev/api/users/<zenn-username>`

to resolve the supplied username.

The endpoint is used only by initialization. Normal preflight and check operations use the already configured numeric `{zenn-user-id}`.

Before initialization proceeds, the deploy-worktree state is inspected.

A clean deploy worktree is accepted normally.

A dirty deploy worktree is accepted only when all of the following are true:

- `config.json` does not exist;
- the only init-produced management change is a canonical empty `ledger.json`; and
- that state is recognized as an interrupted initial setup.

Any other dirty deploy-worktree state causes `init` to fail without making further changes.

#### Local management-state classification

`init` classifies the existing `config.json` / `ledger.json` combination before modifying management state.

If neither file exists:

- perform normal first-time initialization.

If `config.json` is absent and `ledger.json` exists:

- invalid or unparsable ledger:
  - return `ERROR`;
  - do not overwrite it automatically.
- canonical empty ledger:
  - treat it as an interrupted initial setup;
  - resolve the requested username again;
  - continue by creating `config.json`.
- ledger containing article or pending state:
  - return `ERROR`;
  - do not create configuration automatically;
  - require inspection of repository state.

If `config.json` exists but `ledger.json` does not:

- return `ERROR`;
- do not regenerate an empty ledger;
- instruct the user to restore the ledger from Git.

If `config.json` exists and `ledger.json` is invalid:

- return `ERROR`;
- do not overwrite the ledger;
- instruct the user to restore it from Git.

If both files exist and the ledger is valid:

- invalid or unparsable config:
  - return `ERROR`;
  - do not overwrite it automatically;
  - instruct the user to restore it from Git.
- valid config:
  - resolve the requested username;
  - if its `{zenn-user-id}` equals the configured ID, report already initialized / no-op;
  - if it differs, return `ERROR`;
  - automatic account replacement is not supported in the MVP.

#### Fresh initialization

For a valid user lookup:

1. obtain `user.id` as `{zenn-user-id}`;
2. obtain `user.username` as diagnostic information;
3. atomically create the canonical empty `ledger.json`;
4. atomically create `config.json`;
5. treat successful `config.json` creation as the initialization completion point.

The ledger is deliberately created before the config.

If processing stops after the empty ledger is created but before config creation, rerunning `init` can recognize and resume that interrupted initial setup.

Once a valid config exists, a missing or damaged ledger is no longer interpreted as first-time initialization.

#### Initialization completion

`init` leaves its generated management-file changes in the deploy worktree.

The state checker does not commit or push them.

The user:

1. reviews the deploy-worktree diff;
2. commits the management files using `[skip ci]`;
3. pushes the deploy branch.

Normal publish workflows begin only after the deploy worktree is clean again.

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

If the publish-plan entry contains an `{observation-override}`:

- current observation != override:
  - return `ERROR`;
  - report that the remote publication may have changed again after the override was created;
  - report both the override and current observation.
- current observation == override:
  - compare the current observation with the ledger successful Zenn observation;
  - if they are equal, return `ERROR` because no remote drift exists and the override is unnecessary or stale;
  - if they differ, return `OK` and use the current observation as `{baseline-observation}`.

If the publish-plan entry does not contain an `{observation-override}`:

- current observation == ledger successful Zenn observation:
  - return `OK`;
  - use the current observation as `{baseline-observation}`.
- otherwise:
  - return `ERROR` as remote drift;
  - report the ledger successful Zenn observation;
  - report the current observation;
  - report the current observation's JSON representation suitable for `#observation-override`.

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

### Terminal-result local apply

Terminal-result local apply applies a confirmed `SUCCESS` or `COLLISION` to the local deploy worktree, article by article, in the order specified below.

Terminal-result local application is idempotent.

No additional persistent intermediate state such as `applying` or `recovery` is introduced.

Removal of `pending_next_state[{article-id}]` is the article-level commit point. It occurs in the same ledger atomic replacement as the corresponding confirmed-state changes, after any required snapshot replacement or candidate cleanup has completed.

If processing is interrupted while the pending entry still exists, a later `check` re-evaluates the same remote result and retries the same terminal-result local apply.

A local write that already equals its target content is treated as a no-op.

Local content that is neither the expected pre-apply state nor the target state is an error.

Once the pending entry has been removed, that article is no longer a terminal-apply target on later checks.

### Applying `SUCCESS`

For `CREATE`, `UPDATE`, and `RECREATE` success, apply the following steps in order:

1. Obtain the candidate `{article-artifact}` from `deploy/articles/<candidate-slug>.md`.
2. Atomically replace the released snapshot with the candidate `{article-artifact}`. If the snapshot already equals the candidate artifact, treat this step as a no-op and continue.
3. Atomically replace the ledger, applying all of the following changes together:
   - create or finalize `articles[{article-id}]` for `CREATE`;
   - confirmed state = `active`;
   - slug = candidate `{slug}` for `CREATE` and `RECREATE`; retain the confirmed slug for `UPDATE`;
   - successful release = candidate `{release-number}`;
   - successful Zenn observation = current observation of the user's own publication;
   - remove `pending_next_state[{article-id}]` (the article-level commit point).

The pending entry remains until snapshot replacement has completed and the ledger atomic replacement commits the article result.

For `DELETE` success, do not modify the released snapshot. Atomically replace the ledger, applying all of the following changes together:

- confirmed state = `erased`;
- retain the existing slug;
- retain successful release;
- retain released snapshot;
- retain successful Zenn observation;
- remove `pending_next_state[{article-id}]` (the article-level commit point).

### Applying `COLLISION`

`COLLISION` applies to `CREATE` and `RECREATE`.

The confirmed successful article state is not changed.

The checker applies the following steps in order:

1. Read the candidate slug from `pending_next_state` and remove `deploy/articles/<candidate-slug>.md`. If the candidate deploy artifact is already absent, treat cleanup as a no-op and continue. Unexpected artifact content is an error under the terminal-result local apply rules.
2. Atomically replace the ledger, leaving confirmed successful article state unchanged and removing `pending_next_state[{article-id}]` together (the article-level commit point).

The pending entry remains until candidate deploy artifact cleanup has completed and the ledger atomic replacement commits the collision result.

The checker leaves the source unchanged, leaves cleanup changes dirty in the deploy worktree, and instructs the user to review, commit, and push the cleanup using `[skip ci]`.

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

If `pending_next_state` is empty, the checker validates ledger confirmed state against deploy `/articles`.

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

### `confirm-pending-update <article-id>`

`confirm-pending-update` is a maintenance command for an UPDATE whose deployment cannot be confirmed automatically because its normal check result remains `PENDING`.

It is available only when:

- `pending_next_state[{article-id}]` exists; and
- the pending operation is `UPDATE`.

The command re-observes the article using the same observation and UPDATE result-classification logic as normal `check`.

If the result is:

- `PENDING`: manual confirmation may proceed;
- `SUCCESS`: manual confirmation is unnecessary; the command returns an error and instructs the user to use normal `check`;
- `UNKNOWN` or `ERROR`: manual confirmation is prohibited.

Manual confirmation uses the user as a trust boundary. Before confirming, the user must have verified:

- in the Zenn Dashboard, that the target deployment completed successfully; and
- that the current remote content equals the candidate `{article-artifact}`.

The state checker does not independently verify remote content equality.

After confirmation, the pending UPDATE is treated as `SUCCESS`.

The command uses exactly the same terminal-result local apply as normal UPDATE success.

Ledger state update and removal of the pending entry occur in the same atomic `ledger.json` replacement.

If interrupted while the pending entry still exists, the command may be safely retried according to the common terminal-result recovery rules.

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

- `SUCCESS` advances ledger confirmed state and removes the pending entry;
- `PENDING` keeps ledger confirmed state unchanged and retains pending;
- `COLLISION` cleans up the failed candidate and removes its pending entry;
- `UNKNOWN` or `ERROR` keeps ledger confirmed state unchanged and retains pending.

After `check`:

`COMMIT_READY` -> review management/cleanup diff, then `[skip ci]` commit and push;

`NOT_READY` -> do not commit. Resolve unresolved pending state by:

- rerunning `check` when further remote propagation may complete;
- for an eligible UPDATE that remains `PENDING`, using `confirm-pending-update <article-id>` and then rerunning `check`; or
- when appropriate, using `discard-all-pendings`.

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

Before mirror synchronization, the complete authoritative image tree is subject to the Phase 0 image validation rules defined above. This validation concerns deployability of image files, not article-reference ownership or image lifecycle.

Preview uses the authoring image tree directly through a symlink.

Article semantic results and image deployment results are independent.

Article state checker success does not prove image deployment success.

If a deployment contains both article and image changes, each result is handled independently. An image deployment result of `ERROR` does not alter the article semantic result determined by the state checker.

If the article result is `SUCCESS`, its confirmed management state may be updated locally even when the image deployment failed.

Whether those local management changes may be committed still depends on the overall `COMMIT_READY` result.

Image-only deployment success is confirmed through the Zenn Dashboard deployment result.

No fixed automatic recovery procedure is defined for image deployment errors. Depending on the failure cause, the user may rename an image, update references, remove an image, or otherwise correct the authoritative authoring inputs and rerun the normal publish workflow.

Retry behavior after a transient Zenn-side image deployment failure with no remaining authoring/deploy image diff is intentionally undefined pending Phase 0 validation.

## External Zenn interfaces

### GitHub repository integration

The deploy branch is connected to Zenn's GitHub repository integration.

A normal push to the configured deploy branch triggers Zenn synchronization.

Management-only commits include `[skip ci]` so that Zenn deployment is skipped.

### Zenn CLI

Zenn CLI is used for local preview inside the container.

### Non-public Zenn APIs

The state checker depends on a small set of non-public Zenn endpoints.

These interfaces are undocumented and may change, so API-dependent interpretation is isolated behind small functions.

#### Article endpoint

`https://zenn.dev/api/articles/{slug}`

The current design treats:

- `404` as no publication for that slug;
- `200` as a publication being present.

Relevant response information includes:

- article `published_at`;
- article `body_updated_at`;
- article `source_repo_updated_at`;
- article `user.id`; and
- article `user.username`.

`user.id` is compared with the configured `{zenn-user-id}` to distinguish an own-user publication from another user's publication.

`user.username` is diagnostic information and is not the canonical ownership criterion.

The update behavior of observation-related metadata has not been fully verified and must be validated empirically.

#### User endpoint

`https://zenn.dev/api/users/{username}`

This endpoint is used only by `state checker init`.

For a valid user, the expected response provides user information including:

- `id`;
- `username`; and
- `name`.

Initialization stores:

- `id` as `{zenn-user-id}`; and
- `username` as diagnostic configuration.

A missing user (`404`) or an invalid response causes initialization to fail without creating `config.json`.

Normal state-checker operation does not repeatedly resolve the username through this endpoint.

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

### Scheduled publication

Scheduled publication through a future `published_at` is not supported in the Phase 0 MVP.

A future implementation should avoid overloading the existing `pending_next_state` semantics.

The current design direction is to consider a separate `future_publication_pending` state for candidates that have been deployed but whose scheduled publication time has not yet arrived.

The intended properties of that future design are:

- `future_publication_pending` is separate from normal `pending_next_state`;
- while an article is in future-pending state, another publish for the same `{article-id}` or `{slug}` is not allowed in the initial design;
- other articles may still be published;
- after the scheduled time has passed, `state checker check` may promote the candidate into `pending_next_state`;
- promotion is performed by one atomic `ledger.json` replacement;
- promotion occurs only when normal `pending_next_state` is empty;
- if normal `pending_next_state` is not empty, promotion is deferred, the future-pending entry remains unchanged, and a later `check` retries the decision.

Whether a scheduled publication should instead permit replacement of the still-unpublished candidate remains an open design question.

Before this feature is specified for implementation, the Zenn article API must be tested before and after scheduled publication to determine how a reserved but not-yet-public article is represented remotely.

The scheduled time becoming due means only that remote result observation may begin. It does not itself establish publication success.
