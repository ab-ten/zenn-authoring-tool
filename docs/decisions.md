# Design Decisions

## Scope

This document records the rationale behind major design choices in the Zenn authoring tool.

It does not redefine domain semantics or operational behavior. Normative definitions and workflows remain in:

- [`domain.md`](domain.md) for domain values and lifecycle semantics;
- [`architecture.md`](architecture.md) for representation, validation, publish planning, state checking, and recovery; and
- [`use-cases.md`](use-cases.md) for user-facing workflows and observable outcomes.

The purpose of this document is to preserve why the current design was chosen, especially where simpler-looking alternatives would weaken safety, recovery, or practical usability.

## Separate authoring and deploy branches with Git worktrees

### Decision

Use one Git repository with:

- `main` or `master` as the authoring branch; and
- `deploy` as the Zenn-connected deployment and management branch.

The deploy branch is checked out as a linked worktree at `/_deploy` directly under the authoring worktree.

The authoring branch ignores `/_deploy/`.

Use relative worktree links with:

`worktree.useRelativePaths = true`

so that the same repository/worktree relationship can be used from both the host and container even when their absolute filesystem paths differ.

The host and container require Git 2.48 or later.

### Rationale

Authoring commits should not automatically become Zenn deployment inputs. Drafts and in-progress work need to be commit-able without causing unnecessary deployment.

Using worktrees also allows the authoring branch and deploy branch to be accessed at the same time without repeatedly switching the checked-out branch.

Keeping the deploy worktree at a stable relative location also allows the host and container to observe the same repository layout without embedding host-specific absolute worktree paths into Git metadata.

### Consequences

The deploy branch is treated as a generated and managed representation of the authoring state rather than as the primary editing surface.

Deployment requires an explicit conversion, diff review, commit, and push.

See [`architecture.md`](architecture.md#repository-and-worktree-layout).

## Run authoring tools inside Docker with a narrow trust boundary

### Decision

Run the converter, state checker, preview environment, and their dependencies inside Docker.

Expose only files required for Zenn authoring and deployment to the container.

### Rationale

The toolchain depends on large Node.js and Python dependency trees. Keeping those dependencies inside the container limits their direct access to the host filesystem and narrows the set of host files exposed to the toolchain.

The container does not need unrestricted access to unrelated host files.

### Consequences

The authoring worktree is exposed read-only to the container.

The deploy worktree at `/_deploy` is exposed read-write because converter and state-checker operations must update generated and management files there.

Git metadata for both worktrees is exposed read-only.

The container may inspect Git state but does not modify Git metadata.

Operations that require broader host control remain outside the container.

See [`architecture.md`](architecture.md#execution-environment-and-responsibility-boundary).

## Keep Git history operations on the host

### Decision

Do not let the converter or state checker perform Git history operations such as commit, push, or history rollback.

The user performs those operations on the host after reviewing the tool-produced worktree changes or recovery instructions.

Git commands executed inside the container are limited to read-only inspection. Git status inspection uses `--no-optional-locks`, and operations that modify refs, indexes, worktree metadata, or repository history remain host-side responsibilities.

### Rationale

Keeping Git history operations on the host preserves an explicit review boundary between tool-produced worktree changes and repository history changes.

It also keeps recovery inspectable: the tool can prepare or describe required worktree changes without itself performing history rollback.


### Consequences

The normal workflow deliberately includes user-visible `git diff`, commit, and push steps.

Recovery commands such as restore operations are presented for host-side execution rather than being performed automatically inside the container.

See [`architecture.md`](architecture.md#execution-environment-and-responsibility-boundary).

## Use an explicit sequential, make-like conversion model

### Decision

Assume explicit, sequential converter execution by a single user.

Concurrent conversions and modification of participating authoring sources while conversion is running are unsupported operations.

Do not add converter-side locking or mid-run source-change detection for the current design.

### Rationale

The converter is a local generation tool, not a concurrent service.

For the intended single-user workflow, the deploy worktree's dirty state and explicit `git diff` review provide a simpler recovery boundary for interrupted or unexpected conversions.

Adding concurrency control would increase implementation complexity without addressing a supported workflow.

### Consequences

Users must not run multiple conversions concurrently or edit participating sources during conversion.

Unexpected partial results are detected and recovered through the deploy worktree state and Git review.

See [`architecture.md`](architecture.md#execution-model).

## Classify each article operation once

### Decision

Determine each article's `{operation}` once from source state, ledger confirmed state, and comparison of candidate article content with the content of the released snapshot during converter planning.

Later validation, preflight, and apply phases must not reclassify that operation.

### Rationale

Release checks, slug checks, remote preflight, and deploy application must all refer to the same intended transition.

Reclassifying later could make validation or remote checks describe a different transition from the one eventually applied.

### Consequences

Operation-specific checks validate the already selected operation rather than inferring another one.

The state checker receives the operation as part of the publish plan.

See [`architecture.md`](architecture.md#article-state-and-operation-classification).

## Finalize one immutable publish plan before preflight and apply

### Decision

Build the complete publish plan before modifying the deploy worktree.

After article entries and image mirror differences have been collected, treat the final publish plan as immutable for the remainder of conversion.

Pass exactly the same publish plan to state-checker preflight and to deploy application.

### Rationale

The remote checks and the local changes must describe the same candidate deployment.

Using one fixed plan prevents preflight from approving one set of operations while apply performs another.

### Consequences

If preflight fails, the plan is not applied.

`pending_next_state` is derived from the same fixed plan after deploy-worktree changes succeed.

See [`architecture.md`](architecture.md#publish-plan).

## Treat deploy `/articles` and `/images` as tool-managed outputs

### Decision

Treat deploy `/articles` and deploy `/images` as generated or synchronized outputs.

Users do not directly edit them.

### Rationale

The deploy worktree needs a clear ownership boundary so that converter output, state-checker cleanup, diff review, and recovery can be interpreted reliably.

Direct manual edits would blur whether a difference came from the authoring source, the converter, recovery, or an unintended local change.

### Consequences

Authoritative article edits occur in the authoring source.

Authoritative image edits occur in authoring `/images`.

User interaction with generated deploy content is limited to diff review, Git history operations, and explicitly instructed recovery operations.

Because direct editing of deploy artifacts is unsupported, the state checker does not perform a separate content-equality validation against Git HEAD. Operation-specific filesystem existence checks and commit-readiness validation remain in effect.

See [`architecture.md`](architecture.md#deploy-worktree-invariants).

## Keep image lifecycle independent from article lifecycle

### Decision

Synchronize authoring `/images` to deploy `/images` as a mirror without giving images article-style state, release numbers, ledger entries, released snapshots, or pending state.

Evaluate image deployment results independently from article semantic results.

### Rationale

Image assets do not need the article publication lifecycle to satisfy the current workflow.

For example, replacing bytes at the same image path should not create a new article release when the article Markdown itself is unchanged.

A mirror model keeps image handling simple while allowing preview and deployed articles to use the same `/images/...` paths.

### Consequences

Image-only changes do not change article release or ledger state and do not create `pending_next_state`.

Unused images are not automatically removed. Deletion is explicit through the authoritative authoring image tree.

If an article Markdown image-reference path changes, `{article-content}` changes and the normal article release rules apply.

Image deployment failure has no fixed automatic recovery procedure.

When the failure can be addressed by changing authoritative authoring inputs, the user may correct those inputs and rerun the normal publish workflow.

Retry behavior after a transient Zenn-side image deployment failure with no remaining authoring/deploy image diff remains undefined pending Phase 0 validation.

See [`architecture.md`](architecture.md#image-workflow).

## Use non-public Zenn APIs behind a small abstraction

### Decision

Use the non-public Zenn article endpoint for publication existence, ownership, collision, and observation checks.

Use the non-public Zenn user endpoint only during repository initialization to resolve a supplied username to the numeric Zenn user ID.

Keep API-dependent interpretation behind small, isolated functions.

### Rationale

The authoring tool needs to distinguish:

- no publication for a slug;
- the configured user's own publication;
- another user's publication; and
- changes to an existing remote publication.

A username is convenient user input during initial setup, but publication ownership requires a stable canonical comparison value.

The Zenn responses expose numeric `user.id`, so initialization resolves the username once and persists that numeric identity for later ownership checks.

Repeated username lookup during normal operation would unnecessarily increase dependence on the non-public user endpoint.

The interfaces are non-public and their exact behavior may change, so broad coupling to response formats would make maintenance unnecessarily expensive.

### Consequences

The design accepts dependency on undocumented and potentially changing Zenn interfaces.

The users endpoint is required only for `state checker init`.

Normal ownership checks compare the article response `user.id` with the configured `{zenn-user-id}`.

Username and display name are not used as canonical owner identity.

API-dependent behavior must be empirically verified, and changes should remain isolated to the API interpretation layer.

See [`architecture.md`](architecture.md#non-public-zenn-apis).

## Persist repository owner identity separately from article state

### Decision

Store repository-level Zenn owner identity in `/.zenn-authoring/config.json`, separate from `ledger.json`.

Create the configuration explicitly through `state checker init`.

Store:

- numeric `{zenn-user-id}` as the canonical owner identity; and
- the resolved username only as diagnostic information.

Do not modify `config.json` during normal article state transitions.

Create the initial empty `ledger.json` during the same initialization workflow, but keep it as a separate file that remains mutable under normal state transitions.

### Rationale

Owner identity and article publication state have different lifecycles.

The configured account identity is expected to remain stable after repository setup, while `ledger.json` changes whenever successful state or pending state changes.

Keeping owner identity inside the frequently rewritten ledger would unnecessarily expose identity configuration to corruption or accidental replacement during normal state updates.

A separate initialization-only configuration also makes accidental account switching explicit rather than allowing it to occur as a side effect of ledger mutation.

### Consequences

`config.json` and `ledger.json` have distinct roles:

- `config.json` identifies which Zenn account this repository manages;
- `ledger.json` records article successful and unresolved state.

A valid config with a missing or damaged ledger is treated as recovery-required state, not as a fresh repository.

The checker does not recreate an empty ledger automatically in that situation.

Changing the configured Zenn account is not supported automatically in the MVP.

The initialization workflow creates the empty ledger first and the config second so that an interruption before config creation can be recognized and safely resumed.

Both files are Git-managed, and initial creation is committed with `[skip ci]`.

## Defer scheduled publication from the Phase 0 MVP

### Decision

Do not support scheduled publication through a future Front Matter `published_at` in the Phase 0 MVP.

For `CREATE` and `RECREATE`, a future `published_at` causes converter validation to fail before deployment.

Past and current `published_at` values remain supported for establishing the initial publication date.

Do not change the existing `pending_next_state` model merely to support scheduled publication.

### Rationale

The current `pending_next_state` model represents an article transition already applied to the deploy worktree whose remote result remains unresolved.

A scheduled article introduces a different state: deployment may already have completed even though the publication is intentionally not yet expected to appear as a normal remote publication.

Using normal `pending_next_state` for that interval could keep the global deployment serialization active until the scheduled time, potentially blocking unrelated article publication for hours or days.

The behavior of the non-public Zenn article API for a scheduled but not-yet-public article has also not yet been verified.

Adding scheduled publication before that behavior is known would therefore require assumptions about remote observability and would broaden the pending-state model without evidence that the additional complexity is necessary.

### Consequences

The Phase 0 converter rejects future `published_at` values for `CREATE` and `RECREATE`.

Existing `pending_next_state`, preflight, check, and terminal-result semantics remain unchanged.

A future implementation may introduce a separate `future_publication_pending` representation and promote due candidates into normal `pending_next_state` only when the normal pending set is empty.

That future design is not yet normative.

Before scheduled publication is implemented, the article API behavior must be tested before and after the scheduled publication time.

See [`architecture.md`](architecture.md#scheduled-publication).

## Keep the initial state checker one-shot

### Decision

Have `state checker check` perform one remote observation per relevant article and then exit.

If another observation is needed, the user reruns `check`.

### Rationale

A one-shot checker keeps the initial implementation simple and predictable and avoids turning the checker into a long-running polling process.

The user remains in control of when another remote observation is performed.

### Consequences

A result may remain `PENDING` across multiple explicit invocations.

Bounded repeated polling may be considered later, but it is not part of the initial behavior.

See [`architecture.md`](architecture.md#state-checker).

## Serialize unresolved article deployments with `pending_next_state`

### Decision

Store unresolved article transitions in `pending_next_state` after their changes have been applied to the deploy worktree.

At converter entry, after worktree-state checks, fail with `ERROR` if any pending article transition exists.

While pending state exists, do not begin:

- candidate article content generation;
- image validation;
- image mirror-difference calculation; or
- publish-plan generation.

State-checker preflight retains its pending-state check as a defensive invariant check, not as the primary serialization boundary.

Image-only changes do not create pending article transitions.

### Rationale

A pending article transition represents a deploy-worktree change whose remote result has not yet been established as successful.

Allowing another publish workflow to begin while an earlier article transition remains unresolved would make it unsafe to attribute remote changes to a particular deployment.

For UPDATE, this is especially important because success classification relies on comparison with the fixed `{baseline-observation}` captured during preflight.

Checking pending state at converter entry prevents the construction of new publish candidates while an earlier workflow remains unresolved.

This is workflow-level serialization, not a concurrency lock. Concurrent converter executions remain unsupported.

### Consequences

The converter must reject a new conversion whenever `pending_next_state` is non-empty, including conversions that would otherwise contain only image changes.

Pending article transitions must be resolved through:

- normal `SUCCESS`, which advances successful state and removes the pending entry;
- `confirm-pending-update`, which confirms an eligible UPDATE using explicit user verification;
- `COLLISION`, which removes the failed deploy artifact without advancing confirmed successful state; or
- `discard-all-pendings`, which abandons all unresolved article transitions under the required safety conditions.

`PENDING`, `UNKNOWN`, and `ERROR` retain the pending entry.

Preflight cannot be used as an alternative path around the converter-entry guard.

See [`architecture.md`](architecture.md#pending-transition-guard).

## Recover interrupted terminal-result application by idempotent retry

### Decision

Make terminal-result local application idempotent and recover interruption by retrying the same terminal-result processing.

Do not introduce additional persistent intermediate states such as `applying` or `recovery`.

Treat removal of `pending_next_state[{article-id}]` as the article-level commit point.

Update `ledger.json` and released snapshots by atomic replacement rather than in-place modification.

For `CREATE`, `UPDATE`, and `RECREATE` SUCCESS:

1. Confirm that the corresponding deploy artifact exists. If absent, return `ERROR` and retain the pending entry.
2. Atomically replace the released snapshot with the content of the deploy artifact. If it already contains the same content, treat this step as a no-op.
3. Atomically replace `ledger.json` with the successful-state changes and pending removal together.

For `DELETE` SUCCESS:

1. Confirm that the corresponding deploy artifact is absent. If present, return `ERROR` and retain the pending entry.
2. Preserve the released snapshot.
3. Atomically replace `ledger.json` with the confirmed-state change and pending removal together.

For `COLLISION`, remove the failed deploy artifact before removing the pending entry. If the artifact is already absent, treat cleanup as a no-op.

Preserve confirmed successful state and the released snapshot during COLLISION cleanup.

### Rationale

The existing pending entry already provides a durable indication that terminal processing has not committed for that article.

As long as the pending entry remains, `check` can observe the remote result again and retry the same local apply.

Making snapshot replacement and collision cleanup idempotent allows interruption after a partial local update to be repaired without introducing another persistent transaction phase.

Adding separate `applying` or `recovery` states would expand the persistent state machine and create additional intermediate states requiring their own recovery rules.

Atomic replacement prevents interruption during a management-file or snapshot write from leaving a partially written file.

Deploy artifacts are tool-managed outputs. Direct user editing is unsupported, so the checker does not introduce a separate Git HEAD content-equality validation for terminal-result application.

### Consequences

A released snapshot already containing the target deploy artifact content is treated as a no-op during retry.

A COLLISION cleanup whose target deploy artifact is already absent is also treated as a no-op.

A missing deploy artifact required for CREATE, UPDATE, or RECREATE SUCCESS causes `ERROR` while preserving the pending entry.

A remaining deploy artifact during DELETE SUCCESS also causes `ERROR` while preserving the pending entry.

If processing stops after a released snapshot has been updated but before the ledger replacement commits, the pending entry remains and a later `check` retries the terminal apply.

Once the ledger replacement removes `pending_next_state[{article-id}]`, that article is no longer a terminal-apply target on later checks.

Recovery therefore depends on idempotent operations, operation-specific local existence checks, atomic file replacement, and the existing pending commit point.

See [`architecture.md`](architecture.md#terminal-result-local-apply).

## Allow explicit manual confirmation only for ambiguous pending UPDATEs

### Decision

Provide `confirm-pending-update <article-id>` as a maintenance operation for an UPDATE that remains `PENDING` because normal observation cannot establish that the candidate deployment changed the remote observation.

Allow manual confirmation only when:

- the pending operation is `UPDATE`;
- normal UPDATE result classification still returns `PENDING`;
- the user has verified in the Zenn Dashboard that the target deployment completed successfully; and
- the user has verified that the current remote article content equals the content of the corresponding deploy artifact.

Treat successful manual confirmation as the same semantic `SUCCESS` as a normally observed UPDATE success.

Reuse the normal UPDATE SUCCESS terminal-result local apply rather than defining a separate state transition or successful-state representation.

### Rationale

An UPDATE may legitimately deploy without producing an observable change that distinguishes the post-deploy `{zenn-observation}` from its `{baseline-observation}`.

Leaving such an UPDATE transition permanently `PENDING` would make recovery impossible even when the user can independently verify both the deployment result and the resulting article content.

At the same time, automatically treating an unchanged observation as success would weaken the safety boundary because the checker has no evidence that the pending UPDATE was reflected remotely.

Manual confirmation makes that uncertainty explicit and places the unverifiable content-equality decision at a deliberate user trust boundary.

Reusing normal UPDATE SUCCESS processing avoids creating a second kind of successful UPDATE state.

### Consequences

`confirm-pending-update` is not a general override for `UNKNOWN`, `ERROR`, or other operations.

If a fresh observation already classifies the UPDATE as `SUCCESS`, the maintenance command is unnecessary and normal `check` is used instead.

If the result is `UNKNOWN` or `ERROR`, manual confirmation is prohibited.

Successful confirmation updates the same successful release, released snapshot, successful Zenn observation, and pending state that normal UPDATE SUCCESS would update.

The common idempotent terminal-result recovery rules also apply if manual confirmation is interrupted.

After manual confirmation, `check` is run again so that overall `COMMIT_READY` / `NOT_READY` is determined across all articles.

See [`architecture.md`](architecture.md#confirm-pending-update-article-id).

## Use `[skip ci]` for management-only commits

### Decision

Commit and push management-only or cleanup changes on the deploy branch using `[skip ci]`.

### Rationale

Those commits persist local management state after a deployment result has already been determined. They must not trigger another Zenn deployment.

### Consequences

Normal deploy-triggering commits do not use this marker.

Management and cleanup commits after `COMMIT_READY` do use it.

See [`architecture.md`](architecture.md#deploy-branch).

## Reconcile intentional remote drift through one-shot authorization and the normal UPDATE workflow

### Decision

Do not import an intentional out-of-band remote edit directly into confirmed ledger state.

Instead, reconcile the intended remote content into the authoring source, declare the next release, and use the normal UPDATE publish workflow to establish that content as a new successful release.

When UPDATE preflight detects remote drift, require the user to explicitly authorize the reviewed current remote state with a one-shot `{observation-override}`.

The override contains exactly one `{zenn-observation}` and is valid only when the current remote observation exactly matches that value.

A successful override-assisted preflight uses the current remote observation as the UPDATE `{baseline-observation}` and proceeds through the ordinary UPDATE pending, deployment, and result-confirmation workflow.

Remove the override from the authoring source after the corresponding successful release has been confirmed.

### Rationale

An intentional Zenn-side edit contains two distinct concerns:

- the remote content must be reconciled back into the authoritative authoring source; and
- the remote drift that would normally block UPDATE preflight must be explicitly reviewed and authorized.

Updating ledger successful state directly from the existing remote publication would bypass the normal release workflow and make successful state describe content that had not passed through candidate article content generation, deploy artifact materialization, the pending article transition, and successful-result confirmation.

A one-shot observation authorization solves the narrower problem: it allows preflight to accept the specific remote state the user has reviewed without treating that authorization itself as successful publication state.

Reusing the normal UPDATE workflow keeps one definition of successful UPDATE regardless of whether the baseline remote state came from the previous managed publication or from an explicitly reviewed emergency remote edit.

Exact equality between the current remote observation and `{observation-override}` also prevents an authorization from silently covering an additional remote change that occurred after the user reviewed the original drift.

### Consequences

`{observation-override}` does not update:

- ledger confirmed state;
- the released snapshot;
- the successful release; or
- the successful Zenn observation.

The override is planning and preflight input only. It is not stored in `pending_next_state`.

After successful preflight, only the resulting `{baseline-observation}` is carried into the normal pending UPDATE.

The override applies only to an UPDATE candidate declaring the next release and is not retained as historical state or carried into later releases.

If the current remote observation no longer equals the override, preflight fails and the newer remote state must be reviewed explicitly.

If the current observation already equals the ledger successful Zenn observation, the override is stale or unnecessary and preflight fails rather than silently accepting it.

The resulting UPDATE is confirmed through normal `check` when possible. If it remains eligible `PENDING`, `confirm-pending-update` may be used under its separate user-verification rules.

After successful release confirmation, the source override is removed without incrementing the release number.

This design intentionally replaces a separate remote-adoption/import transaction with explicit authorization followed by the ordinary UPDATE lifecycle.

See [`architecture.md`](architecture.md#update-preflight) and [`use-cases.md`](use-cases.md#us-10-reconcile-an-emergency-zenn-side-edit).

## Validate image deployability without introducing image reference lifecycle

### Decision

In Phase 0, validate the complete authoritative authoring `/images` tree before image mirror synchronization.

Validate only the local properties required to keep obviously invalid image inputs from reaching Zenn deployment:

- supported lowercase extension;
- conservative local file-size limit;
- normal Git blob entry; and
- absence of filesystem special-file inputs.

Do not introduce Markdown reference-graph analysis, image ownership, unused-image rejection, image decoding, MIME inspection, or dimension inspection in Phase 0.

### Rationale

The purpose of Phase 0 image validation is to prevent known invalid deployment inputs from reaching Zenn while preserving the existing simple mirror model.

Zenn documents concrete constraints for repository image size and supported extensions. Enforcing those constraints locally removes predictable deployment failures without creating an article-to-image lifecycle model.

Reference-graph analysis would expand converter responsibility into Markdown interpretation and image lifecycle management. It is not required to preserve the current invariant that authoring `/images` is an authoritative mirror whose assets may exist independently of article state.

### Consequences

Every mirror input is validated, including unchanged images.

An invalid image input prevents deploy-worktree application and creation of new pending article state for that conversion.

Unused images remain valid.

Missing image references, invalid relative references, and deletion of images still referenced by published content remain preview and user-review responsibilities in Phase 0.

Image-content validation may be considered later if operational evidence shows that extension and size validation are insufficient.

See [`architecture.md`](architecture.md#image-validation).
